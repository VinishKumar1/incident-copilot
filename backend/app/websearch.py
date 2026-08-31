"""Approved web search — Tavily, restricted to an allowlist of domains.

Requires TAVILY_API_KEY. WEB_SEARCH_ALLOWED_DOMAINS (comma-separated) restricts
every query to that domain list — nothing outside it is ever searched or returned.
Degrades to a no-op (empty results) when no key is configured, the same pattern
every other optional integration in this app already follows (snow.py, github.py).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("websearch")

_TAVILY_URL = "https://api.tavily.com/search"


class WebSearchClient:
    @property
    def configured(self) -> bool:
        return bool(settings.tavily_api_key)

    async def search(self, query: str, max_results: int = 3) -> list:
        """Returns [{title, url, content}]. Empty list if not configured, on any
        error, or if the query is empty — this is a best-effort enrichment step,
        never a hard dependency for the incident-analysis endpoint."""
        query = (query or "").strip()
        if not self.configured or not query:
            return []
        domains = settings.web_search_allowed_domain_list
        payload: dict = {
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        if domains:
            payload["include_domains"] = domains
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(_TAVILY_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            log.warning("web search failed: %s", exc)
            return []
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": (r.get("content") or "")[:600],
            }
            for r in (data.get("results") or [])[:max_results]
        ]


web_search_client = WebSearchClient()
