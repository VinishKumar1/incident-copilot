from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class LogEntry(BaseModel):
    timestamp: float  # unix seconds
    line: str
    labels: Dict[str, str] = Field(default_factory=dict)

    @property
    def service(self) -> str:
        return (
            self.labels.get("app")
            or self.labels.get("container")
            or self.labels.get("pod")
            or self.labels.get("job")
            or "unknown"
        )


class Issue(BaseModel):
    id: str  # fingerprint
    title: str
    level: str = "error"
    service: str = "unknown"
    namespace: str = ""
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    sample_line: str = ""
    samples: List[str] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)


class AnalyzeResponse(BaseModel):
    issue_id: str
    summary: str
    likely_causes: List[str] = Field(default_factory=list)
    suggested_fixes: List[str] = Field(default_factory=list)
    severity: str = "unknown"
    cached: bool = False


class CodeMatchFile(BaseModel):
    path: str
    url: str = ""
    reason: str = ""


class CodeMatchResponse(BaseModel):
    issue_id: str
    located: bool = False
    repo: str = ""
    repo_url: str = ""
    summary: str = ""
    root_cause: str = ""
    suggested_fix: str = ""
    confidence: str = "unknown"
    files: List[CodeMatchFile] = Field(default_factory=list)
    reason: str = ""  # machine reason when not located (repo_not_found / no_github_token / no_match)
    cached: bool = False


class FixEdit(BaseModel):
    path: str
    explanation: str = ""


class FixResponse(BaseModel):
    issue_id: str
    created: bool = False
    pr_url: str = ""
    pr_number: Optional[int] = None
    branch: str = ""
    base: str = "develop"
    title: str = ""
    summary: str = ""
    edits: List[FixEdit] = Field(default_factory=list)
    error: str = ""


class SearchMatch(BaseModel):
    ts: str = ""
    namespace: str = ""
    service: str = ""
    pod: str = ""
    level: str = ""
    message: str = ""
    trace_id: str = ""


class SearchServiceGroup(BaseModel):
    service: str
    namespace: str = ""
    total: int = 0
    problem_count: int = 0
    problems: List[SearchMatch] = Field(default_factory=list)
    trace_ids: List[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    key: str
    namespace: str = ""
    namespaces: List[str] = Field(default_factory=list)
    minutes: int = 0
    total_matches: int = 0
    problem_count: int = 0
    services: List[SearchServiceGroup] = Field(default_factory=list)
    trace_ids: List[str] = Field(default_factory=list)
    # Phase-2: errors found by following trace IDs into other services
    trace_issues: List[SearchServiceGroup] = Field(default_factory=list)
    note: str = ""


class SearchSummaryIssue(BaseModel):
    service: str = ""
    namespace: str = ""
    text: str = ""  # plain-English one-liner


class SearchSummaryResponse(BaseModel):
    key: str
    found: bool = False
    headline: str = ""
    issues: List[SearchSummaryIssue] = Field(default_factory=list)
    problem_count: int = 0
    services_count: int = 0
    namespaces: List[str] = Field(default_factory=list)


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    issue_id: Optional[str] = None
    messages: List[ChatMessage]


class ChatResponse(BaseModel):
    reply: str


class NamespaceSummaryIssue(BaseModel):
    service: str = ""
    level: str = ""
    count: int = 0
    text: str = ""  # plain-English one-liner for this service


class NamespaceSummaryResponse(BaseModel):
    namespace: str
    minutes: int
    total_issues: int = 0
    affected_services: int = 0
    overall_health: str = "unknown"  # healthy | degraded | critical
    headline: str = ""
    issues: List[NamespaceSummaryIssue] = Field(default_factory=list)
    top_concern: str = ""
    cached: bool = False


class KBEntry(BaseModel):
    id: str = ""
    fingerprint: str = ""
    service: str = ""
    pattern_text: str
    root_cause: str
    fix_summary: str
    servicenow_incident: str = ""
    verified_by: str = ""
    verified_at: float = 0.0


class KBMatch(BaseModel):
    entry: KBEntry
    confidence: float


class Recommendation(BaseModel):
    incident_number: str = ""
    issue_id: str = ""
    source: str = "l1"  # "l1" (knowledge base) | "llm" (cold analysis, optionally + web)
    summary: str = ""
    root_cause: str = ""
    suggested_fix: str = ""
    confidence: float = 0.0
    confidence_label: str = "unknown"  # low | medium | high
    servicenow_work_note: str = ""
    kb_entry_id: str = ""
    sources_used: List[str] = Field(default_factory=list)


class MarkUsedRequest(BaseModel):
    used: bool
    edited: bool = False
    notes: str = ""
