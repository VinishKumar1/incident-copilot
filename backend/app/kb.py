"""RAG knowledge base — verified incident patterns, searchable by similarity.

Mirrors analytics.py's backend selection: PostgreSQL + pgvector when DATABASE_URL
is set (and the `vector` extension is enabled on that instance), SQLite fallback
for local dev with cosine similarity computed in Python instead of in SQL.

Embeddings use a LOCAL sentence-transformers model (nomic-embed-text-v1.5,
settings.use_local_embeddings=True) — runs on-device, free, no API key, no rate
limits. If the local model is disabled or fails to load, retrieval falls back to
lexical (keyword) search — no remote embedding API is used.
"""
from __future__ import annotations

import hashlib
import asyncio
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
from .models import KBDocumentChunk, KBDocumentMatch, KBEntry, KBMatch

log = logging.getLogger("kb")

_USE_POSTGRES = bool(settings.database_url)
_pg_available = False  # set True only after successful init, same pattern as analytics.py
_pg_pool = None


# ─── Embeddings (shared by both backends) ────────────────────────────────────

_local_embed_model = None          # lazily-loaded SentenceTransformer instance
_local_embed_model_kind: Optional[str] = None  # "loaded" | "unavailable"


def embeddings_enabled() -> bool:
    # Local sentence-transformers model only — no API key/credential required.
    return bool(settings.use_local_embeddings)


def _get_local_embed_model():
    """Lazily loads the local sentence-transformers model (downloaded from Hugging
    Face on first use, then cached under ~/.cache/huggingface). Returns None if the
    package/model can't be loaded, so callers fall back to remote/lexical retrieval."""
    global _local_embed_model, _local_embed_model_kind
    if _local_embed_model_kind is not None:
        return _local_embed_model
    try:
        from sentence_transformers import SentenceTransformer

        log.info("kb: loading local embedding model %s (first run downloads it) ...", settings.local_embedding_model)
        _local_embed_model = SentenceTransformer(settings.local_embedding_model, trust_remote_code=True)
        _local_embed_model_kind = "loaded"
        log.info("kb: local embedding model ready")
    except Exception as exc:
        log.warning("kb: local embedding model unavailable (%s) — falling back to remote/lexical", exc)
        _local_embed_model = None
        _local_embed_model_kind = "unavailable"
    return _local_embed_model


async def embed_text(text: str, kind: str = "document") -> Optional[list]:
    """Returns an embedding vector using the local sentence-transformers model, or
    None if that model is disabled/unavailable — callers then fall back to lexical
    keyword retrieval instead of failing the request. No external API key is used.

    kind: "query" for search queries, "document" for stored KB entries/chunks — per
    the Nomic model card, these get different "search_query:"/"search_document:"
    prefixes.
    """
    if not settings.use_local_embeddings:
        return None
    return await _embed_text_local(text, kind)


async def _embed_text_local(text: str, kind: str) -> Optional[list]:
    """Runs the local sentence-transformers model in a worker thread (encode() is
    CPU-bound/synchronous) so it doesn't block the event loop."""
    model = _get_local_embed_model()
    if model is None:
        return None
    prefix = "search_query: " if kind == "query" else "search_document: "
    prefixed = prefix + (text or "")[:8000]
    try:
        vector = await asyncio.to_thread(model.encode, prefixed)
        return [float(x) for x in vector]
    except Exception as exc:
        log.warning("kb.embed_text: local embedding failed (%s) — falling back to lexical search", exc)
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


# ─── Keyless fallback (Plan B): keyword-overlap retrieval when no embeddings API ───
# Without OPENAI_API_KEY, embeddings return None; instead of dropping the data we
# store it with an empty vector and retrieve via token overlap. Real embeddings take
# over automatically as soon as the key is configured — no code change needed.

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be been by for from has have in is it its not of on or that "
    "the this to was were will with".split()
)


def _tokens(text: str) -> set:
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOPWORDS}


def _keyword_score(query_tokens: set, text: str) -> float:
    """Overlap coefficient: |shared tokens| / min(|query|, |entry|) tokens.

    Real incident text (pattern_text) is long and noisy — full description plus
    several correlated log lines — while KB entries are short, curated patterns.
    Dividing by the query's token count (as a naive "fraction of query matched"
    would) makes even a perfect thematic match score near zero, since the query
    has far more unique tokens (booking IDs, timestamps, ...) than the entry could
    ever contain. Normalizing by the *smaller* set instead means a short KB entry
    whose distinctive terms are all present in the (longer) query scores highly.
    """
    entry_tokens = _tokens(text)
    if not query_tokens or not entry_tokens:
        return 0.0
    smaller = min(len(query_tokens), len(entry_tokens))
    return len(query_tokens & entry_tokens) / smaller


def chunk_text(text: str, max_chars: int = 1200) -> list:
    """Split raw text into retrieval-friendly chunks: paragraphs are packed greedily
    up to max_chars; an oversized paragraph is split on sentence boundaries. Empty
    input yields []."""
    chunks: list = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for para in (p.strip() for p in re.split(r"\n\s*\n", text or "")):
        if not para:
            continue
        if len(para) > max_chars:
            _flush()
            cur = ""
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                if len(cur) + len(sentence) + 1 > max_chars:
                    if cur:
                        chunks.append(cur.strip())
                    cur = sentence[:max_chars]
                else:
                    cur = f"{cur} {sentence}".strip()
            if cur:
                chunks.append(cur.strip())
            continue
        if len(buf) + len(para) + 2 > max_chars:
            _flush()
        buf = f"{buf}\n\n{para}".strip()
    _flush()
    return chunks


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
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_documents (
                id          TEXT PRIMARY KEY,
                doc_id      TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL DEFAULT '',
                service     TEXT NOT NULL DEFAULT '',
                chunk_index INTEGER NOT NULL DEFAULT 0,
                chunk_text  TEXT NOT NULL,
                embedding   VECTOR(1536),
                source      TEXT NOT NULL DEFAULT '',
                created_at  DOUBLE PRECISION NOT NULL
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_documents_doc ON kb_documents(doc_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_documents_embedding ON kb_documents "
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


async def _pg_ingest_documents(doc_id: str, title: str, service: str, source: str,
                               chunks: list) -> int:
    """Embeds and stores document chunks in pgvector. Returns the number stored."""
    pool = await _get_pg_pool()
    stored = 0
    async with pool.acquire() as conn:
        for index, chunk in enumerate(chunks):
            embedding = await embed_text(chunk)
            if embedding is None:
                log.warning("kb.ingest_document: no embeddings client — chunk %d skipped", index)
                continue
            await conn.execute(
                """
                INSERT INTO kb_documents
                    (id, doc_id, title, service, chunk_index, chunk_text, embedding, source, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8,$9)
                """,
                str(uuid.uuid4()), doc_id, title, service, index, chunk,
                _vector_literal(embedding), source, time.time(),
            )
            stored += 1
    return stored


async def _pg_search_documents(embedding: list, service: str, top_k: int) -> list:
    pool = await _get_pg_pool()
    vec_literal = _vector_literal(embedding)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, doc_id, title, service, chunk_index, chunk_text, source,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM kb_documents
            WHERE ($2 = '' OR service = $2)
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            vec_literal, service, top_k,
        )
    out = []
    for r in rows:
        chunk = KBDocumentChunk(
            id=r["id"], doc_id=r["doc_id"], title=r["title"], service=r["service"],
            chunk_index=r["chunk_index"], chunk_text=r["chunk_text"], source=r["source"] or "",
        )
        out.append(KBDocumentMatch(chunk=chunk, confidence=max(0.0, min(1.0, float(r["similarity"])))))
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
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          TEXT PRIMARY KEY,
            doc_id      TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL DEFAULT '',
            service     TEXT NOT NULL DEFAULT '',
            chunk_index INTEGER NOT NULL DEFAULT 0,
            chunk_text  TEXT NOT NULL,
            embedding   TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kb_documents_doc ON kb_documents(doc_id);
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


async def _sqlite_ingest_documents(doc_id: str, title: str, service: str, source: str,
                                   chunks: list) -> int:
    conn = _get_sqlite()
    stored = 0
    for index, chunk in enumerate(chunks):
        embedding = await embed_text(chunk)
        if embedding is None:
            # Plan B: no embeddings client — keep the chunk for keyword retrieval.
            log.info("kb: no embeddings client — chunk %d stored for keyword retrieval only", index)
        conn.execute(
            """
            INSERT INTO kb_documents (id, doc_id, title, service, chunk_index,
                chunk_text, embedding, source, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (str(uuid.uuid4()), doc_id, title, service, index, chunk,
             json.dumps(embedding or []), source, time.time()),
        )
        stored += 1
    conn.commit()
    return stored


async def _sqlite_search_documents(embedding: list, service: str, top_k: int) -> list:
    conn = _get_sqlite()
    if service:
        rows = conn.execute("SELECT * FROM kb_documents WHERE service = ?", (service,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kb_documents").fetchall()
    scored = []
    for r in rows:
        vec = json.loads(r["embedding"])
        scored.append((_cosine_similarity(embedding, vec), r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for sim, r in scored[:top_k]:
        chunk = KBDocumentChunk(
            id=r["id"], doc_id=r["doc_id"], title=r["title"], service=r["service"],
            chunk_index=r["chunk_index"], chunk_text=r["chunk_text"], source=r["source"] or "",
        )
        out.append(KBDocumentMatch(chunk=chunk, confidence=max(0.0, min(1.0, sim))))
    return out


def _lexical_search_documents(pattern_text: str, service: str, top_k: int) -> list:
    """Plan B retrieval over kb_documents when embeddings aren't configured."""
    conn = _get_sqlite()
    if service:
        rows = conn.execute("SELECT * FROM kb_documents WHERE service = ?", (service,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kb_documents").fetchall()
    query_tokens = _tokens(pattern_text)
    scored = [(_keyword_score(query_tokens, r["chunk_text"]), r) for r in rows]
    scored = [pair for pair in scored if pair[0] > 0.0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["chunk_index"]))
    out = []
    for score, r in scored[:top_k]:
        chunk = KBDocumentChunk(
            id=r["id"], doc_id=r["doc_id"], title=r["title"], service=r["service"],
            chunk_index=r["chunk_index"], chunk_text=r["chunk_text"], source=r["source"] or "",
        )
        out.append(KBDocumentMatch(chunk=chunk, confidence=round(min(1.0, score), 3)))
    return out


def _lexical_search_entries(pattern_text: str, service: str, top_k: int) -> list:
    """Plan B retrieval over kb_entries (verified resolutions) when embeddings aren't
    configured. Lets L1 still return an approved resolution in keyless demo mode."""
    conn = _get_sqlite()
    if service:
        # service="" entries (e.g. human-approved resolutions from /approve) are
        # service-agnostic — they must stay visible to scoped searches too.
        rows = conn.execute(
            "SELECT * FROM kb_entries WHERE service = ? OR service = ''", (service,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kb_entries").fetchall()
    query_tokens = _tokens(pattern_text)
    scored = [(_keyword_score(query_tokens, r["pattern_text"]), r) for r in rows]
    scored = [pair for pair in scored if pair[0] > 0.0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["verified_at"]))
    out = []
    for score, r in scored[:top_k]:
        entry = KBEntry(
            id=r["id"], fingerprint=r["fingerprint"], service=r["service"],
            pattern_text=r["pattern_text"], root_cause=r["root_cause"],
            fix_summary=r["fix_summary"], servicenow_incident=r["servicenow_incident"] or "",
            verified_by=r["verified_by"], verified_at=r["verified_at"],
        )
        out.append(KBMatch(entry=entry, confidence=round(min(1.0, score), 3)))
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


async def upsert_entry(entry: KBEntry) -> Optional[str]:
    """Embeds and stores a verified entry. Returns the entry id, or None if embeddings
    aren't configured (the caller should treat that as a soft failure, not an error)."""
    embedding = await embed_text(entry.pattern_text)
    if embedding is None:
        if _USE_POSTGRES and _pg_available:
            log.warning("kb.upsert_entry: no embeddings client configured — entry not stored")
            return None
        # Plan B (SQLite): store with an empty vector; retrieval falls back to keywords.
        log.info("kb.upsert_entry: no embeddings client — entry stored for keyword retrieval only")
        embedding = []
    if _USE_POSTGRES and _pg_available:
        return await _pg_upsert(entry, embedding)
    return _sqlite_upsert(entry, embedding)


async def search(pattern_text: str, service: str = "", top_k: int = 3) -> list:
    """Up to top_k KBMatch objects, best match first. Empty list if embeddings aren't
    configured or nothing is stored yet — callers should treat that as "no L1 match"."""
    embedding = await embed_text(pattern_text, kind="query")
    if embedding is None:
        if _USE_POSTGRES and _pg_available:
            return []
        return _lexical_search_entries(pattern_text, service, top_k)
    if _USE_POSTGRES and _pg_available:
        return await _pg_search(embedding, service, top_k)
    return _sqlite_search(embedding, service, top_k)


async def ingest_document(title: str, text: str, service: str = "", source: str = "seed") -> dict:
    """Chunks a block of incident-detail text, embeds each chunk, and stores it as
    retrieval context (kb_documents). Returns {"doc_id", "chunks"}; chunks is 0 when
    embeddings aren't configured (soft failure — callers should surface, not crash)."""
    chunks = chunk_text(text)
    if not chunks:
        return {"doc_id": "", "chunks": 0}
    doc_id = str(uuid.uuid4())
    if _USE_POSTGRES and _pg_available:
        stored = await _pg_ingest_documents(doc_id, title, service, source, chunks)
    else:
        stored = await _sqlite_ingest_documents(doc_id, title, service, source, chunks)
    if stored == 0:
        return {"doc_id": "", "chunks": 0}
    return {"doc_id": doc_id, "chunks": stored}


async def search_documents(pattern_text: str, service: str = "", top_k: int = 3) -> list:
    """Up to top_k KBDocumentMatch objects, best match first. Without an embeddings
    client (Plan B) retrieval falls back to keyword overlap; empty list if nothing
    matches or nothing is ingested yet."""
    embedding = await embed_text(pattern_text, kind="query")
    if embedding is None:
        return _lexical_search_documents(pattern_text, service, top_k)
    if _USE_POSTGRES and _pg_available:
        return await _pg_search_documents(embedding, service, top_k)
    return await _sqlite_search_documents(embedding, service, top_k)


async def kb_stats() -> dict:
    """Entry/chunk counts + whether embeddings are configured. Best-effort."""
    counts = {"kb_entries": 0, "kb_documents": 0, "kb_doc_chunks": 0}
    try:
        if _USE_POSTGRES and _pg_available:
            pool = await _get_pg_pool()
            async with pool.acquire() as conn:
                counts["kb_entries"] = await conn.fetchval("SELECT count(*) FROM kb_entries")
                counts["kb_documents"] = await conn.fetchval(
                    "SELECT count(DISTINCT doc_id) FROM kb_documents")
                counts["kb_doc_chunks"] = await conn.fetchval("SELECT count(*) FROM kb_documents")
        else:
            conn = _get_sqlite()
            counts["kb_entries"] = conn.execute("SELECT count(*) FROM kb_entries").fetchone()[0]
            counts["kb_documents"] = conn.execute(
                "SELECT count(DISTINCT doc_id) FROM kb_documents").fetchone()[0]
            counts["kb_doc_chunks"] = conn.execute(
                "SELECT count(*) FROM kb_documents").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        counts["error"] = str(exc)
    counts["embeddings_enabled"] = embeddings_enabled()
    counts["retrieval_mode"] = "embeddings" if counts["embeddings_enabled"] else "keyword-fallback"
    return counts


# --- Demo seed ---------------------------------------------------------------
# Pre-loaded verified resolutions so the resolver demo and tests work on a fresh
# database without a manual /api/kb/ingest step. Fixed ids make repeated seeding
# idempotent (upsert on conflict).

_MOCK_KB_SEED: list = [
    (
        "seed-tms-acknowledgement",
        "INC0098421\nBookings remain pending after submission\n"
        "Booking GHDGW54NC00 remains pending after Send to TMS was requested. "
        "One of two transport orders has not received a TMS acknowledgement.\n"
        "Send-to-TMS was initiated successfully, but acknowledgement is missing for 1 of 2 transport orders.\n"
        "Send to TMS is In Progress because acknowledgement has not been received from TMS. "
        "The workflow cannot proceed until the missing transport-order acknowledgement arrives.\n"
        "TMS acknowledgement still pending for transport order TO-78451201 after 20 minutes\n"
        "Booking GHDGW54NC00 work process SEND_TO_TMS changed to STARTED",
        "The booking request was accepted and SEND_TO_TMS started, but one transport "
        "order has no acknowledgement, so the workflow cannot complete.",
        "Check the outbound TMS topic and acknowledgement consumer. If the outbound "
        "message exists, replay the missing acknowledgement; otherwise perform one "
        "idempotent Send-to-TMS retrigger.",
    ),
    (
        "seed-master-data-timeout",
        "INC0098399\nPort information times out during booking\n"
        "Booking SHP-84219373 stopped in master-data enrichment; port information "
        "could not be loaded.\nHTTP 504 while reading master data",
        "A downstream master-data read timed out, so the booking workflow stopped "
        "before confirmation.",
        "Check master-data service health and latency, confirm the client timeout "
        "and circuit-breaker state, then retry the enrichment after the dependency "
        "has recovered.",
    ),
    (
        "seed-s3-access-denied",
        "INC0098417\nDocuments unavailable for completed shipment\n"
        "S3 is returning HTTP 403 for the document lookup\nS3Exception Access Denied",
        "S3 returns HTTP 403 for the document lookup, matching an expired or "
        "mis-scoped workload identity rather than booking logic.",
        "Validate the current workload identity and S3 bucket policy, then refresh "
        "the configured credential reference. Keep permissions limited to the "
        "document bucket and retry the lookup.",
    ),
    (
        "seed-consumer-lag",
        "Billing consumer lag\nconsumer group falling behind\npartition lag "
        "keeps growing\nrecords pending in the outbound queue",
        "The billing consumer group is falling behind, so bookings stay pending "
        "while records wait in the queue.",
        "Identify the slow partition, scale the consumer group or restart the "
        "stuck consumer, then confirm lag returns to zero and bookings confirm.",
    ),
]


async def seed_mock_kb() -> None:
    """Idempotent demo seed of verified KB entries (keyword-retrieval friendly)."""
    for entry_id, pattern, root_cause, fix in _MOCK_KB_SEED:
        await upsert_entry(KBEntry(
            id=entry_id,
            fingerprint=f"seed:{entry_id}",
            service="",
            pattern_text=pattern[:4000],
            root_cause=root_cause,
            fix_summary=fix,
            servicenow_incident="",
            verified_by="seed",
        ))
