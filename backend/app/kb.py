"""RAG knowledge base — verified incident patterns, searchable by similarity.

Mirrors analytics.py's backend selection: PostgreSQL + pgvector when DATABASE_URL
is set (and the `vector` extension is enabled on that instance), SQLite fallback
for local dev with cosine similarity computed in Python instead of in SQL.

Embeddings go through the same OpenAI-compatible client the rest of the app uses
(settings.openai_api_key / settings.openai_base_url) regardless of which provider
LLM_PROVIDER picks for chat/analysis — Anthropic has no embeddings endpoint, and in
this deployment OPENAI_BASE_URL already points at the Maersk Vibe proxy, so this
needs no new credential beyond what llm.py already uses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import settings
from .models import KBEntry, KBMatch

log = logging.getLogger("kb")

_USE_POSTGRES = bool(settings.database_url)
_pg_available = False  # set True only after successful init, same pattern as analytics.py
_pg_pool = None


# ─── Embeddings (shared by both backends) ────────────────────────────────────

_embed_client = None
_embed_client_kind: Optional[str] = None  # "openai" | "azure" | "none"


def embeddings_enabled() -> bool:
    return bool(settings.openai_api_key)


def _get_embed_client():
    global _embed_client, _embed_client_kind
    if _embed_client_kind is not None:
        return _embed_client
    if not settings.openai_api_key:
        _embed_client_kind = "none"
        return None
    if settings.is_azure_openai:
        from openai import AsyncAzureOpenAI
        _embed_client = AsyncAzureOpenAI(
            api_key=settings.openai_api_key,
            azure_endpoint=settings.openai_base_url,
            api_version=settings.openai_api_version,
        )
        _embed_client_kind = "azure"
    else:
        from openai import AsyncOpenAI
        _embed_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        _embed_client_kind = "openai"
    return _embed_client


_LEX_DIM = 96
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _lexical_embed(text: str) -> list:
    """Hash-bag vector so mock/demo RAG works without an embeddings API key."""
    vec = [0.0] * _LEX_DIM
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) < 3:
            continue
        idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % _LEX_DIM
        vec[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


async def embed_text(text: str) -> Optional[list]:
    """Returns an embedding vector, or None if no embeddings client is configured.

    In mock mode we fall back to a local lexical vector so L1 RAG can be demoed
    without OpenAI/Azure credentials.
    """
    client = _get_embed_client()
    if client is not None:
        resp = await client.embeddings.create(model=settings.embedding_model, input=(text or "")[:8000])
        return list(resp.data[0].embedding)
    if settings.use_mock:
        return _lexical_embed(text)
    return None


def _cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _vector_literal(embedding: list) -> str:
    """pgvector's text input format: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


# ─── PostgreSQL + pgvector backend ───────────────────────────────────────────

async def _get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    import asyncpg
    url = settings.database_url
    if url.endswith("?ssl=true") or url.endswith("&ssl=true"):
        url = url.rsplit("?ssl", 1)[0].rsplit("&ssl", 1)[0]
        _pg_pool = await asyncpg.create_pool(
            url, ssl="require", min_size=1, max_size=5, command_timeout=10, timeout=10,
        )
    else:
        _pg_pool = await asyncpg.create_pool(
            url, min_size=1, max_size=5, command_timeout=10, timeout=10,
        )
    return _pg_pool


async def _init_pg(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_entries (
                id                  TEXT PRIMARY KEY,
                fingerprint         TEXT NOT NULL DEFAULT '',
                service             TEXT NOT NULL DEFAULT '',
                pattern_text        TEXT NOT NULL,
                embedding           VECTOR(1536),
                root_cause          TEXT NOT NULL,
                fix_summary         TEXT NOT NULL,
                servicenow_incident TEXT DEFAULT '',
                verified_by         TEXT NOT NULL DEFAULT '',
                verified_at         DOUBLE PRECISION NOT NULL
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_fingerprint ON kb_entries(fingerprint)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_embedding ON kb_entries "
            "USING hnsw (embedding vector_cosine_ops)"
        )


async def _pg_upsert(entry: KBEntry, embedding: list) -> str:
    pool = await _get_pg_pool()
    entry_id = entry.id or str(uuid.uuid4())
    vec_literal = _vector_literal(embedding)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kb_entries
                (id, fingerprint, service, pattern_text, embedding, root_cause,
                 fix_summary, servicenow_incident, verified_by, verified_at)
            VALUES ($1,$2,$3,$4,$5::vector,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO UPDATE SET
                pattern_text = EXCLUDED.pattern_text,
                embedding = EXCLUDED.embedding,
                root_cause = EXCLUDED.root_cause,
                fix_summary = EXCLUDED.fix_summary,
                servicenow_incident = EXCLUDED.servicenow_incident,
                verified_by = EXCLUDED.verified_by,
                verified_at = EXCLUDED.verified_at
            """,
            entry_id, entry.fingerprint, entry.service, entry.pattern_text,
            vec_literal, entry.root_cause, entry.fix_summary,
            entry.servicenow_incident, entry.verified_by, entry.verified_at or time.time(),
        )
    return entry_id


async def _pg_search(embedding: list, service: str, top_k: int) -> list:
    pool = await _get_pg_pool()
    vec_literal = _vector_literal(embedding)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, fingerprint, service, pattern_text, root_cause, fix_summary,
                   servicenow_incident, verified_by, verified_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM kb_entries
            WHERE ($2 = '' OR service = $2)
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            vec_literal, service, top_k,
        )
    out = []
    for r in rows:
        entry = KBEntry(
            id=r["id"], fingerprint=r["fingerprint"], service=r["service"],
            pattern_text=r["pattern_text"], root_cause=r["root_cause"],
            fix_summary=r["fix_summary"], servicenow_incident=r["servicenow_incident"] or "",
            verified_by=r["verified_by"], verified_at=r["verified_at"],
        )
        out.append(KBMatch(entry=entry, confidence=max(0.0, min(1.0, float(r["similarity"])))))
    return out


# ─── SQLite backend (local dev fallback — no pgvector needed) ───────────────

_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent / "data"))
_KB_DB_PATH = _DATA_DIR / "kb.db"
_sqlite_conn = None


def _get_sqlite():
    global _sqlite_conn
    if _sqlite_conn is not None:
        return _sqlite_conn
    import sqlite3
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_KB_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kb_entries (
            id                  TEXT PRIMARY KEY,
            fingerprint         TEXT NOT NULL DEFAULT '',
            service             TEXT NOT NULL DEFAULT '',
            pattern_text        TEXT NOT NULL,
            embedding           TEXT NOT NULL,
            root_cause          TEXT NOT NULL,
            fix_summary         TEXT NOT NULL,
            servicenow_incident TEXT DEFAULT '',
            verified_by         TEXT NOT NULL DEFAULT '',
            verified_at         REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kb_fingerprint ON kb_entries(fingerprint);
        """
    )
    conn.commit()
    _sqlite_conn = conn
    return conn


def _sqlite_upsert(entry: KBEntry, embedding: list) -> str:
    conn = _get_sqlite()
    entry_id = entry.id or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO kb_entries (id, fingerprint, service, pattern_text, embedding,
            root_cause, fix_summary, servicenow_incident, verified_by, verified_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            pattern_text=excluded.pattern_text, embedding=excluded.embedding,
            root_cause=excluded.root_cause, fix_summary=excluded.fix_summary,
            servicenow_incident=excluded.servicenow_incident,
            verified_by=excluded.verified_by, verified_at=excluded.verified_at
        """,
        (entry_id, entry.fingerprint, entry.service, entry.pattern_text,
         json.dumps(embedding), entry.root_cause, entry.fix_summary,
         entry.servicenow_incident, entry.verified_by, entry.verified_at or time.time()),
    )
    conn.commit()
    return entry_id


def _sqlite_search(embedding: list, service: str, top_k: int) -> list:
    conn = _get_sqlite()
    if service:
        rows = conn.execute("SELECT * FROM kb_entries WHERE service = ?", (service,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kb_entries").fetchall()
    scored = []
    for r in rows:
        vec = json.loads(r["embedding"])
        scored.append((_cosine_similarity(embedding, vec), r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for sim, r in scored[:top_k]:
        entry = KBEntry(
            id=r["id"], fingerprint=r["fingerprint"], service=r["service"],
            pattern_text=r["pattern_text"], root_cause=r["root_cause"],
            fix_summary=r["fix_summary"], servicenow_incident=r["servicenow_incident"] or "",
            verified_by=r["verified_by"], verified_at=r["verified_at"],
        )
        out.append(KBMatch(entry=entry, confidence=max(0.0, min(1.0, sim))))
    return out


# ─── Public API (called by l1_agent.py and the verification/ingest paths) ───

async def init_kb() -> None:
    """Called once at startup, same shape as analytics.init_analytics()."""
    global _pg_available
    if _USE_POSTGRES:
        try:
            pool = await _get_pg_pool()
            await _init_pg(pool)
            _pg_available = True
            log.info("kb: connected to PostgreSQL + pgvector ✅")
        except Exception as exc:
            _pg_available = False
            log.warning("kb: PostgreSQL/pgvector unavailable (%s) — falling back to SQLite", exc)
            _get_sqlite()
            log.info("kb: using SQLite at %s (fallback)", _KB_DB_PATH)
    else:
        _get_sqlite()
        log.info("kb: using SQLite at %s", _KB_DB_PATH)
    if settings.use_mock:
        await seed_mock_kb()


async def upsert_entry(entry: KBEntry) -> Optional[str]:
    """Embeds and stores a verified entry. Returns the entry id, or None if embeddings
    aren't configured (the caller should treat that as a soft failure, not an error)."""
    embedding = await embed_text(entry.pattern_text)
    if embedding is None:
        log.warning("kb.upsert_entry: no embeddings client configured — entry not stored")
        return None
    if _USE_POSTGRES and _pg_available:
        return await _pg_upsert(entry, embedding)
    return _sqlite_upsert(entry, embedding)


async def search(pattern_text: str, service: str = "", top_k: int = 3) -> list:
    """Up to top_k KBMatch objects, best match first. Empty list if embeddings aren't
    configured or nothing is stored yet — callers should treat that as "no L1 match"."""
    embedding = await embed_text(pattern_text)
    if embedding is None:
        return []
    if _USE_POSTGRES and _pg_available:
        matches = await _pg_search(embedding, service, top_k)
    else:
        matches = _sqlite_search(embedding, service, top_k)
    return matches


_SEED_ENTRIES = [
    KBEntry(
        id="seed-tms-ack",
        service="",
        pattern_text=(
            "Bookings remain pending after submission. Booking remains pending after Send to TMS "
            "was requested. One of two transport orders has not received a TMS acknowledgement. "
            "SEND_TO_TMS started. TMS acknowledgement still pending for transport order."
        ),
        root_cause="The booking request was accepted and SEND_TO_TMS started, but a transport order has no acknowledgement, so the workflow cannot complete.",
        fix_summary="Check the outbound TMS topic and acknowledgement consumer. Replay the missing acknowledgement when the outbound message exists; otherwise perform one idempotent Send-to-TMS retrigger.",
        servicenow_incident="INC0098421",
        verified_by="seed",
        verified_at=1.0,
    ),
    KBEntry(
        id="seed-s3-403",
        service="",
        pattern_text=(
            "Documents unavailable for completed shipment. Customer cannot download documents. "
            "S3Exception Access Denied Status Code 403. Document lookup failed for container."
        ),
        root_cause="S3 is returning HTTP 403 for the document lookup, matching an expired or mis-scoped workload identity.",
        fix_summary="Validate the current workload identity and S3 bucket policy, then refresh the configured credential reference. Keep permissions limited to the document bucket and retry the lookup.",
        servicenow_incident="INC0098417",
        verified_by="seed",
        verified_at=1.0,
    ),
    KBEntry(
        id="seed-consumer-lag",
        service="",
        pattern_text=(
            "Billing events delayed by consumer lag. Billing events for invoice are delayed while "
            "Kafka consumer lag is elevated. Consumer group telikos-billing lag partition."
        ),
        root_cause="The billing consumer group is healthy but processing more slowly than the incoming event rate, matching the consumer-lag runbook.",
        fix_summary="Check for a stuck partition and downstream throttling, then scale the consumer within the configured partition limit and monitor lag until it drains.",
        servicenow_incident="INC0098344",
        verified_by="seed",
        verified_at=1.0,
    ),
]


async def seed_mock_kb() -> None:
    """Load demo runbooks so L1 RAG has something to hit in mock mode."""
    for entry in _SEED_ENTRIES:
        entry_id = await upsert_entry(entry)
        if entry_id:
            log.info("kb: seeded mock entry %s", entry_id)
