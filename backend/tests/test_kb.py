"""Tests for the knowledge base — kb.py (SQLite backend; no Postgres/pgvector needed)."""
from __future__ import annotations

import pytest

from app import kb
from app.models import KBEntry


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path, monkeypatch):
    """Each test gets its own throwaway SQLite file so entries don't leak between tests."""
    monkeypatch.setattr(kb, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(kb, "_KB_DB_PATH", tmp_path / "kb.db")
    monkeypatch.setattr(kb, "_sqlite_conn", None)
    monkeypatch.setattr(kb, "_USE_POSTGRES", False)
    yield


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Deterministic fake embeddings so tests don't need a real API key: derived from
    the first 8 characters, so identical text embeds identically."""
    async def fake_embed_text(text):
        return [float(ord(c)) for c in (text or "")[:8].ljust(8)]

    monkeypatch.setattr(kb, "embed_text", fake_embed_text)
    return fake_embed_text


@pytest.mark.asyncio
async def test_search_returns_empty_when_nothing_stored(fake_embeddings):
    assert await kb.search("some pattern") == []


@pytest.mark.asyncio
async def test_upsert_then_search_finds_the_entry(fake_embeddings):
    entry = KBEntry(
        fingerprint="fp1", service="orders",
        pattern_text="NullPointerException in OrderMapper",
        root_cause="OrderMapper.map() dereferences a null customer field",
        fix_summary="Add a null check before mapping",
        verified_by="vinish",
    )
    entry_id = await kb.upsert_entry(entry)
    assert entry_id

    matches = await kb.search("NullPointerException in OrderMapper", service="orders")
    assert len(matches) == 1
    assert matches[0].entry.root_cause == entry.root_cause
    assert matches[0].confidence > 0.99  # identical text -> identical fake embedding


@pytest.mark.asyncio
async def test_search_scopes_by_service(fake_embeddings):
    await kb.upsert_entry(KBEntry(
        service="orders", pattern_text="timeout calling pricing API",
        root_cause="pricing API 503", fix_summary="retry with backoff", verified_by="a",
    ))
    assert await kb.search("timeout calling pricing API", service="billing") == []


@pytest.mark.asyncio
async def test_search_returns_empty_when_embeddings_not_configured(monkeypatch):
    async def no_client(text):
        return None

    monkeypatch.setattr(kb, "embed_text", no_client)
    assert await kb.search("anything") == []


@pytest.mark.asyncio
async def test_upsert_returns_none_when_embeddings_not_configured(monkeypatch):
    async def no_client(text):
        return None

    monkeypatch.setattr(kb, "embed_text", no_client)
    entry_id = await kb.upsert_entry(
        KBEntry(pattern_text="x", root_cause="y", fix_summary="z", verified_by="a")
    )
    assert entry_id is None
