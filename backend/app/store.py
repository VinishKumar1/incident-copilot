from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from .grouping import merge_entries
from .models import AnalyzeResponse, CodeMatchResponse, Issue, LogEntry, NamespaceSummaryResponse


class IssueStore:
    """In-memory store of grouped issues. Swap for Redis/Postgres for persistence."""

    def __init__(self) -> None:
        self._issues: Dict[str, Issue] = {}
        self._analyses: Dict[str, AnalyzeResponse] = {}
        self._code_matches: Dict[str, CodeMatchResponse] = {}
        self._contexts: Dict[str, dict] = {}
        self._adhoc: Dict[str, Issue] = {}
        self._searches: Dict[str, dict] = {}
        self._summaries: Dict[str, NamespaceSummaryResponse] = {}  # namespace -> summary
        self._lock = asyncio.Lock()
        self.last_poll_ts: float = 0.0
        self.last_error: Optional[str] = None

    async def clear(self) -> None:
        async with self._lock:
            self._issues.clear()
            self._analyses.clear()
            self._code_matches.clear()
            self._contexts.clear()
            self._adhoc.clear()
            self._searches.clear()

    async def ingest(self, entries: List[LogEntry]) -> None:
        async with self._lock:
            merged = merge_entries(list(self._issues.values()), entries)
            self._issues = {i.id: i for i in merged}

    async def list_issues(self) -> List[Issue]:
        async with self._lock:
            return sorted(self._issues.values(), key=lambda i: i.last_seen, reverse=True)

    async def get(self, issue_id: str) -> Optional[Issue]:
        async with self._lock:
            return self._issues.get(issue_id) or self._adhoc.get(issue_id)

    async def add_adhoc(self, issue: Issue) -> None:
        async with self._lock:
            self._adhoc[issue.id] = issue

    def get_cached_search(self, sig: str) -> Optional[dict]:
        return self._searches.get(sig)

    def cache_search(self, sig: str, result: dict) -> None:
        # keep only the few most recent searches
        if len(self._searches) > 8:
            self._searches.pop(next(iter(self._searches)))
        self._searches[sig] = result

    def get_cached_analysis(self, issue_id: str) -> Optional[AnalyzeResponse]:
        return self._analyses.get(issue_id)

    def cache_analysis(self, analysis: AnalyzeResponse) -> None:
        self._analyses[analysis.issue_id] = analysis

    def get_cached_context(self, issue_id: str) -> Optional[dict]:
        return self._contexts.get(issue_id)

    def cache_context(self, issue_id: str, ctx: dict) -> None:
        self._contexts[issue_id] = ctx

    def get_cached_code_match(self, issue_id: str) -> Optional[CodeMatchResponse]:
        return self._code_matches.get(issue_id)

    def cache_code_match(self, match: CodeMatchResponse) -> None:
        self._code_matches[match.issue_id] = match

    def get_cached_summary(self, namespace: str) -> Optional[NamespaceSummaryResponse]:
        return self._summaries.get(namespace)

    def cache_summary(self, summary: NamespaceSummaryResponse) -> None:
        self._summaries[summary.namespace] = summary


store = IssueStore()
