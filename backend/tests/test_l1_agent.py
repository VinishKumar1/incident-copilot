"""Tests for the L1 confidence-gated lookup — l1_agent.py"""
from __future__ import annotations

import pytest

from app import l1_agent
from app.config import settings
from app.models import KBEntry, KBMatch


def _match(confidence: float) -> KBMatch:
    entry = KBEntry(
        id="kb-1", service="orders", pattern_text="x",
        root_cause="the pricing API returned 503",
        fix_summary="retry with exponential backoff",
        verified_by="vinish",
    )
    return KBMatch(entry=entry, confidence=confidence)


@pytest.mark.asyncio
async def test_returns_none_when_no_matches(monkeypatch):
    async def fake_search(pattern_text, service="", top_k=1):
        return []

    monkeypatch.setattr(l1_agent.kb, "search", fake_search)
    assert await l1_agent.l1_lookup("anything") is None


@pytest.mark.asyncio
async def test_returns_none_below_threshold(monkeypatch):
    monkeypatch.setattr(settings, "kb_confidence_threshold", 0.85)

    async def fake_search(pattern_text, service="", top_k=1):
        return [_match(0.6)]

    monkeypatch.setattr(l1_agent.kb, "search", fake_search)
    assert await l1_agent.l1_lookup("anything") is None


@pytest.mark.asyncio
async def test_returns_recommendation_above_threshold(monkeypatch):
    monkeypatch.setattr(settings, "kb_confidence_threshold", 0.85)

    async def fake_search(pattern_text, service="", top_k=1):
        return [_match(0.93)]

    monkeypatch.setattr(l1_agent.kb, "search", fake_search)

    result = await l1_agent.l1_lookup("pricing API timeout", service="orders")
    assert result is not None
    assert result.source == "l1"
    assert result.confidence == pytest.approx(0.93)
    assert result.confidence_label == "high"
    assert result.kb_entry_id == "kb-1"
    assert "retry with exponential backoff" in result.servicenow_work_note
