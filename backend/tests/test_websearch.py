"""Tests for the approved web-search client — websearch.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.websearch import WebSearchClient


@pytest.mark.asyncio
async def test_not_configured_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    client = WebSearchClient()
    assert client.configured is False
    assert await client.search("kubernetes crashloopbackoff") == []


@pytest.mark.asyncio
async def test_empty_query_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "fake-key")
    client = WebSearchClient()
    assert await client.search("   ") == []


@pytest.mark.asyncio
async def test_configured_search_parses_results_and_sends_domain_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "fake-key")
    monkeypatch.setattr(settings, "web_search_allowed_domains", "kubernetes.io")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "results": [
            {"title": "CrashLoopBackOff", "url": "https://kubernetes.io/docs/x",
             "content": "It means " + "x" * 1000},
        ]
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        client = WebSearchClient()
        results = await client.search("CrashLoopBackOff")

    assert len(results) == 1
    assert results[0]["title"] == "CrashLoopBackOff"
    assert len(results[0]["content"]) <= 600
    sent_kwargs = mock_ctx.post.call_args.kwargs
    assert sent_kwargs["json"]["include_domains"] == ["kubernetes.io"]


@pytest.mark.asyncio
async def test_search_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "fake-key")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(side_effect=RuntimeError("boom"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        client = WebSearchClient()
        results = await client.search("anything")

    assert results == []
