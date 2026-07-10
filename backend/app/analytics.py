"""Usage analytics — stores events in PostgreSQL (production) or SQLite (local dev).

Set DATABASE_URL in .env to a PostgreSQL connection string to use Postgres:
    postgresql://user:pass@host:5432/dbname?ssl=true

Leave DATABASE_URL blank (or unset) to fall back to SQLite at backend/data/usage.db.

Production note (SQLite fallback): if you use SQLite in Kubernetes, mount a
PersistentVolumeClaim at the path defined by DATA_DIR so data survives pod restarts.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("analytics")

# ─── Backend selection ────────────────────────────────────────────────────────

_USE_POSTGRES = bool(settings.database_url)
_pg_available = False  # set True only after successful init

# ─── PostgreSQL backend (asyncpg) ────────────────────────────────────────────

_pg_pool = None  # asyncpg.Pool, created on first use

async def _get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    import asyncpg
    url = settings.database_url
    # asyncpg uses 'ssl=require' not '?ssl=true' — normalise
    if url.endswith("?ssl=true") or url.endswith("&ssl=true"):
        url = url.rsplit("?ssl", 1)[0].rsplit("&ssl", 1)[0]
        _pg_pool = await asyncpg.create_pool(
            url, ssl="require", min_size=1, max_size=5,
            command_timeout=10, timeout=10,
        )
    else:
        _pg_pool = await asyncpg.create_pool(
            url, min_size=1, max_size=5,
            command_timeout=10, timeout=10,
        )
    return _pg_pool


async def _init_pg(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id         BIGSERIAL PRIMARY KEY,
                ts         DOUBLE PRECISION NOT NULL,
                user_email TEXT,
                user_name  TEXT,
                action     TEXT NOT NULL,
                detail     TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ue_ts    ON usage_events(ts)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ue_email ON usage_events(user_email)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id               BIGSERIAL PRIMARY KEY,
                ts               DOUBLE PRECISION NOT NULL,
                api              TEXT NOT NULL,
                model            TEXT,
                prompt_tokens    INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens     INTEGER DEFAULT 0,
                call_count       INTEGER DEFAULT 1
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_au_ts  ON api_usage(ts)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_au_api ON api_usage(api)")


async def _pg_record(user_email: str, user_name: str, action: str, detail: str) -> None:
    try:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO usage_events (ts, user_email, user_name, action, detail) VALUES ($1,$2,$3,$4,$5)",
                time.time(), user_email or "anonymous", user_name or "", action, detail or "",
            )
    except Exception as exc:
        log.warning("postgres analytics write failed: %s", exc)


async def _pg_record_api_usage(api: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    try:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO api_usage (ts, api, model, prompt_tokens, completion_tokens, total_tokens) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                time.time(), api, model or "", prompt_tokens, completion_tokens,
                prompt_tokens + completion_tokens,
            )
    except Exception as exc:
        log.warning("postgres api_usage write failed: %s", exc)


async def _pg_stats(since_hours: int) -> dict:
    pool = await _get_pg_pool()
    since = time.time() - since_hours * 3600
    async with pool.acquire() as conn:
        total_events  = await conn.fetchval("SELECT COUNT(*) FROM usage_events WHERE ts >= $1", since)
        unique_users  = await conn.fetchval("SELECT COUNT(DISTINCT user_email) FROM usage_events WHERE ts >= $1", since)
        logins        = await conn.fetchval("SELECT COUNT(*) FROM usage_events WHERE ts >= $1 AND action='login'", since)
        searches      = await conn.fetchval("SELECT COUNT(*) FROM usage_events WHERE ts >= $1 AND action='search'", since)
        analyses      = await conn.fetchval("SELECT COUNT(*) FROM usage_events WHERE ts >= $1 AND action='analyze'", since)
        ns_changes    = await conn.fetchval("SELECT COUNT(*) FROM usage_events WHERE ts >= $1 AND action='namespace_change'", since)

        top_users = [dict(r) for r in await conn.fetch(
            "SELECT user_email, user_name, COUNT(*) AS event_count FROM usage_events "
            "WHERE ts >= $1 GROUP BY user_email, user_name ORDER BY event_count DESC LIMIT 10", since)]

        action_counts = [dict(r) for r in await conn.fetch(
            "SELECT action, COUNT(*) AS cnt FROM usage_events WHERE ts >= $1 "
            "GROUP BY action ORDER BY cnt DESC", since)]

        recent = [dict(r) for r in await conn.fetch(
            "SELECT ts, user_email, user_name, action, detail FROM usage_events "
            "WHERE ts >= $1 ORDER BY ts DESC LIMIT 50", since)]

        hourly_since = time.time() - 24 * 3600
        hourly_rows = await conn.fetch(
            "SELECT FLOOR((ts - $1) / 3600)::int AS hour_bucket, COUNT(*) AS cnt "
            "FROM usage_events WHERE ts >= $1 GROUP BY hour_bucket ORDER BY hour_bucket",
            hourly_since)
        hourly = [{"hour": r["hour_bucket"], "count": r["cnt"]} for r in hourly_rows]

        # API usage stats
        api_usage_rows = await conn.fetch(
            "SELECT api, model, SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion, "
            "SUM(total_tokens) AS total, COUNT(*) AS calls "
            "FROM api_usage WHERE ts >= $1 GROUP BY api, model ORDER BY total DESC",
            since)
        api_usage = [dict(r) for r in api_usage_rows]

        grafana_rows = await conn.fetch(
            "SELECT DATE_TRUNC('hour', TO_TIMESTAMP(ts)) AS hr, COUNT(*) AS calls "
            "FROM api_usage WHERE ts >= $1 AND api='grafana' GROUP BY hr ORDER BY hr",
            since)
        grafana_hourly = [{"hour": str(r["hr"]), "calls": r["calls"]} for r in grafana_rows]

    return _build_stats(since_hours, total_events, unique_users, logins, searches,
                        analyses, ns_changes, top_users, action_counts, recent, hourly,
                        api_usage, grafana_hourly)


# ─── SQLite backend (local dev fallback) ─────────────────────────────────────

_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent / "data"))
_DB_PATH  = _DATA_DIR / "usage.db"
_sqlite_conn = None


def _get_sqlite():
    global _sqlite_conn
    if _sqlite_conn is not None:
        return _sqlite_conn
    import sqlite3
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            user_email TEXT,
            user_name  TEXT,
            action     TEXT NOT NULL,
            detail     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_email ON events(user_email);
        CREATE TABLE IF NOT EXISTS api_usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                REAL NOT NULL,
            api               TEXT NOT NULL,
            model             TEXT,
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens      INTEGER DEFAULT 0,
            call_count        INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_au_ts  ON api_usage(ts);
        CREATE INDEX IF NOT EXISTS idx_au_api ON api_usage(api);
    """)
    conn.commit()
    _sqlite_conn = conn
    return conn


def _sqlite_record_api_usage(api: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    try:
        conn = _get_sqlite()
        conn.execute(
            "INSERT INTO api_usage (ts, api, model, prompt_tokens, completion_tokens, total_tokens) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), api, model or "", prompt_tokens, completion_tokens,
             prompt_tokens + completion_tokens),
        )
        conn.commit()
    except Exception as exc:
        log.warning("sqlite api_usage write failed: %s", exc)


def _sqlite_record(user_email: str, user_name: str, action: str, detail: str) -> None:
    try:
        conn = _get_sqlite()
        conn.execute(
            "INSERT INTO events (ts, user_email, user_name, action, detail) VALUES (?,?,?,?,?)",
            (time.time(), user_email or "anonymous", user_name or "", action, detail or ""),
        )
        conn.commit()
    except Exception as exc:
        log.warning("sqlite analytics write failed: %s", exc)


def _sqlite_stats(since_hours: int) -> dict:
    conn = _get_sqlite()
    since = time.time() - since_hours * 3600
    total_events = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ?", (since,)).fetchone()[0]
    unique_users = conn.execute("SELECT COUNT(DISTINCT user_email) FROM events WHERE ts >= ?", (since,)).fetchone()[0]
    logins       = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ? AND action='login'", (since,)).fetchone()[0]
    searches     = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ? AND action='search'", (since,)).fetchone()[0]
    analyses     = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ? AND action='analyze'", (since,)).fetchone()[0]
    ns_changes   = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ? AND action='namespace_change'", (since,)).fetchone()[0]

    top_users    = [dict(r) for r in conn.execute(
        "SELECT user_email, user_name, COUNT(*) as event_count FROM events WHERE ts >= ? "
        "GROUP BY user_email ORDER BY event_count DESC LIMIT 10", (since,)).fetchall()]
    action_counts = [dict(r) for r in conn.execute(
        "SELECT action, COUNT(*) as cnt FROM events WHERE ts >= ? GROUP BY action ORDER BY cnt DESC", (since,)).fetchall()]
    recent = [dict(r) for r in conn.execute(
        "SELECT ts, user_email, user_name, action, detail FROM events WHERE ts >= ? "
        "ORDER BY ts DESC LIMIT 50", (since,)).fetchall()]

    hourly_since = time.time() - 24 * 3600
    hourly_rows = conn.execute(
        "SELECT CAST((ts - ?) / 3600 AS INTEGER) as hour_bucket, COUNT(*) as cnt "
        "FROM events WHERE ts >= ? GROUP BY hour_bucket ORDER BY hour_bucket",
        (hourly_since, hourly_since)).fetchall()
    hourly = [{"hour": int(r["hour_bucket"]), "count": r["cnt"]} for r in hourly_rows]

    api_usage = [dict(r) for r in conn.execute(
        "SELECT api, model, SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion, "
        "SUM(total_tokens) AS total, COUNT(*) AS calls "
        "FROM api_usage WHERE ts >= ? GROUP BY api, model ORDER BY total DESC", (since,)).fetchall()]

    grafana_hourly = [{"hour": r["hr"], "calls": r["calls"]} for r in conn.execute(
        "SELECT CAST((ts - ?) / 3600 AS INTEGER) AS hr, COUNT(*) AS calls "
        "FROM api_usage WHERE ts >= ? AND api='grafana' GROUP BY hr ORDER BY hr",
        (hourly_since, hourly_since)).fetchall()]

    return _build_stats(since_hours, total_events, unique_users, logins, searches,
                        analyses, ns_changes, top_users, action_counts, recent, hourly,
                        api_usage, grafana_hourly)


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _build_stats(since_hours, total_events, unique_users, logins, searches,
                 analyses, ns_changes, top_users, action_counts, recent, hourly,
                 api_usage=None, grafana_hourly=None) -> dict:
    return {
        "backend": "postgres" if (_USE_POSTGRES and _pg_available) else "sqlite",
        "since_hours": since_hours,
        "total_events": total_events,
        "unique_users": unique_users,
        "logins": logins,
        "searches": searches,
        "analyses": analyses,
        "namespace_changes": ns_changes,
        "top_users": top_users,
        "action_counts": action_counts,
        "recent_events": recent,
        "hourly_activity": hourly,
        "api_usage": api_usage or [],
        "grafana_hourly": grafana_hourly or [],
    }


# ─── Public API (called by main.py middleware and routes/analytics.py) ────────

async def init_analytics() -> None:
    """Called once at startup. Falls back to SQLite if Postgres is unreachable."""
    global _pg_available
    if _USE_POSTGRES:
        try:
            pool = await _get_pg_pool()
            await _init_pg(pool)
            _pg_available = True
            log.info("analytics: connected to PostgreSQL ✅")
        except Exception as exc:
            _pg_available = False
            log.warning("analytics: PostgreSQL unavailable (%s) — falling back to SQLite", exc)
            _get_sqlite()
            log.info("analytics: using SQLite at %s (fallback)", _DB_PATH)
    else:
        _get_sqlite()
        log.info("analytics: using SQLite at %s", _DB_PATH)


async def record_event(user_email: str, user_name: str, action: str, detail: str = "") -> None:
    """Insert a usage event. Never raises — analytics must not break the app."""
    if _USE_POSTGRES and _pg_available:
        await _pg_record(user_email, user_name, action, detail)
    else:
        _sqlite_record(user_email, user_name, action, detail)


async def record_api_usage(api: str, model: str = "", prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """Record an API call (openai or grafana). Never raises."""
    try:
        if _USE_POSTGRES and _pg_available:
            await _pg_record_api_usage(api, model, prompt_tokens, completion_tokens)
        else:
            _sqlite_record_api_usage(api, model, prompt_tokens, completion_tokens)
    except Exception as exc:
        log.warning("record_api_usage failed: %s", exc)

async def get_stats(since_hours: int = 24) -> dict:
    if _USE_POSTGRES and _pg_available:
        return await _pg_stats(since_hours)
    return _sqlite_stats(since_hours)
