from __future__ import annotations

import json
from typing import List, Optional

from .config import settings
from .models import AnalyzeResponse, ChatMessage, CodeMatchFile, CodeMatchResponse, Issue

_SYSTEM = (
    "You are an SRE assistant embedded in a Kubernetes observability tool. "
    "You help engineers triage error logs from microservices. "
    "Be concrete and specific to the log evidence. Reference likely Kubernetes/"
    "Spring/JVM/Postgres/S3 causes when the logs point there. Never invent log "
    "lines that were not provided. If evidence is thin, say what to check next."
)

# Crisp-reply style for the chatbot.
_CHAT_STYLE = (
    " Answer crisply: lead with the direct answer in the first sentence, be specific (name the exact "
    "service/endpoint/method, or Kafka topic + produce/consume operation, and the concrete reason — "
    "status code, exception type, message), and keep "
    "it short. When source code is provided, cite the file/function. No preamble, no filler, no "
    "restating the question. Aim for a few short lines; use light formatting only when it helps — "
    "simple '-' bullets, **bold** for key terms, `code` for identifiers. Avoid markdown headings."
)

# JSON Schema for structured triage output. Used as a tool/function by both providers.
_ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "ONE crisp sentence: exactly what failed and why. If a remote/HTTP/DB/dependency call failed, name the exact target (service + endpoint/method, or query) and the concrete reason (e.g. 'GET iom-order-service /v3/service-plans/{id} → 404 Not Found')."},
        "likely_causes": {"type": "array", "items": {"type": "string"}, "description": "At most 3, one short line each, most-likely first. No generic advice."},
        "suggested_fixes": {"type": "array", "items": {"type": "string"}, "description": "At most 3, one short actionable line each."},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
    },
    "required": ["summary", "likely_causes", "suggested_fixes", "severity"],
}
_TOOL_NAME = "report_analysis"

_ANALYZE_SYSTEM = (
    "You are an SRE triaging a microservice error. Be TERSE and SPECIFIC — no padding, no restating, "
    "no generic advice. Pinpoint the precise failure: if a remote/HTTP/DB/dependency call fails, name "
    "the exact target (service + endpoint/method or query) and the concrete reason (status code, "
    "exception type, message). If a Kafka/event publish or consume fails, name the topic and the "
    "operation (produce/consume) and the reason (broker unavailable, serialization/deserialization "
    "error, offset commit failure, consumer rebalance, timeout). When source code is provided, use it "
    "to name the exact failing call and cite the file/function. Never invent log lines or code that "
    "weren't provided. If evidence is thin, say in one line what to check next."
)


def _analyze_code_snippet(ctx: Optional[dict]) -> str:
    if not ctx or not ctx.get("files"):
        return ""
    parts = ["", "Relevant source from the repo (use it to name the exact failing call):"]
    for f in ctx["files"][:2]:
        body = "\n".join((f.get("content") or "").splitlines()[:120])
        parts.append(f"\n----- {f['path']} -----\n{body}")
    return "\n".join(parts)

# Tool the chatbot can call to fetch logs on demand.
_GET_LOGS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_service_logs",
        "description": "Fetch recent log lines for a microservice in the monitored Kubernetes namespace. Use whenever the user asks to see, show, or inspect logs for a service.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service / deployment name, e.g. iom-offer-service"},
                "minutes": {"type": "integer", "description": "How many minutes back to fetch (default 5)"},
                "level": {"type": "string", "description": "Optional level filter: error, warn, info, debug. Omit for all levels."},
            },
            "required": ["service"],
        },
    },
}


_CODE_SCHEMA = {
    "type": "object",
    "properties": {
        "located": {"type": "boolean", "description": "True if the cause was found in the provided code."},
        "root_cause": {"type": "string", "description": "What in the code produces this error, citing file and line/function."},
        "suggested_fix": {"type": "string", "description": "Concrete fix, including a corrected code snippet where possible."},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "relevant_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["path", "reason"],
            },
        },
    },
    "required": ["located", "root_cause", "suggested_fix", "confidence"],
}
_CODE_TOOL = "report_code_match"

_CODE_SYSTEM = (
    "You are a senior engineer pinpointing the source of a production error in a service's "
    "own source code. You are given the error and real files fetched from its GitHub repo. "
    "Find where the error originates (the logging/throw site or the faulty logic), explain it "
    "with specific file + line/function references, and propose a concrete fix. Only cite code "
    "that appears in the provided files; if the cause isn't in them, set located=false and say "
    "what additional file or context is needed."
)


_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "pr_title": {"type": "string", "description": "Concise PR title for the fix."},
        "pr_summary": {"type": "string", "description": "What the fix does and why (markdown ok)."},
        "edits": {
            "type": "array",
            "description": "Minimal search/replace edits. old_string MUST be copied verbatim from the file.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact existing text to replace (include enough surrounding lines to be unique)."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "explanation": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    "required": ["pr_title", "pr_summary", "edits"],
}
_FIX_TOOL = "propose_fix"
_FIX_SYSTEM = (
    "You are fixing a bug in a service. You are given the production error, the diagnosed root "
    "cause, and the full current contents of the relevant source files. Produce MINIMAL, surgical "
    "search/replace edits that fix the root cause. Rules: old_string must be copied EXACTLY from the "
    "provided file (whitespace and indentation included) and be unique enough to match once; do not "
    "reformat or touch unrelated code; keep the change as small as possible; preserve the file's "
    "language and style. If you cannot produce a safe fix from the given files, return an empty edits list."
)


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One short line, e.g. 'Booking 5233223 has 3 issues across 2 services'."},
        "issues": {
            "type": "array",
            "description": "One plain-English line per distinct problem, for a non-technical user.",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "namespace": {"type": "string"},
                    "text": {"type": "string", "description": "Plain English: what failed + which downstream API + why. e.g. 'Fetching rates failed — the pricing API returned 500.'"},
                },
                "required": ["text"],
            },
        },
    },
    "required": ["headline", "issues"],
}
_SEARCH_TOOL = "report_booking_issues"
_SEARCH_SYSTEM = (
    "You explain production problems for a specific booking (a.k.a. service plan number) to a "
    "non-technical user in plain, short English. You are given error log lines from multiple "
    "microservices across namespaces. For each DISTINCT problem, write ONE short sentence: what the "
    "service was trying to do, which downstream API failed, and why (status code / reason) — read "
    "these from the log lines. This includes Kafka/event problems — if publishing or consuming an "
    "event fails, say which topic and why (broker unavailable, deserialization error, offset commit "
    "failure, timeout). Merge duplicates. No jargon, no stack traces, no log line copies. "
    "Example style: 'Fetching rates failed because the pricing API returned 500.', 'Booking "
    "validation failed because the master-data API was down.', 'Publishing the booking event to "
    "Kafka topic booking-events failed because the broker was unavailable.' Keep each line under ~20 words."
)


def _search_summary_context(key: str, res: dict) -> str:
    lines = [f"Booking / service plan number searched: {key}", "", "Error log lines found across services:"]
    for s in res.get("services", []):
        for p in s.get("problems", []):
            lines.append(f"- [{p.get('namespace','')}/{p.get('service','')}] {p.get('level','')}: {p.get('message','')}")
    return "\n".join(lines)


def _fix_context(issue: Issue, match, files_full: List[dict]) -> str:
    parts = [
        _issue_context(issue),
        "",
        f"Diagnosed root cause: {match.root_cause}",
        f"Suggested approach: {match.suggested_fix}",
        "",
        "Full current contents of the relevant files:",
    ]
    for f in files_full:
        parts.append(f"\n===== {f['path']} =====")
        parts.append(f["content"])
    return "\n".join(parts)


def _code_context(issue: Issue, ctx: dict) -> str:
    parts = [
        f"Repo: {settings.github_org}/{ctx['repo']} (branch {ctx.get('default_branch')})",
        "",
        _issue_context(issue),
        "",
        "Source files fetched from the repo:",
    ]
    for f in ctx.get("files", []):
        loc = f" (around line {f['line']})" if f.get("line") else ""
        parts.append(f"\n===== {f['path']}{loc} =====")
        parts.append(f.get("content", ""))
    return "\n".join(parts)


def _issue_context(issue: Issue) -> str:
    samples = "\n".join(f"  - {s}" for s in issue.samples[:5])
    return (
        f"Service: {issue.service}\n"
        f"Level: {issue.level}\n"
        f"Occurrences: {issue.count}\n"
        f"Labels: {json.dumps(issue.labels)}\n"
        f"Sample log lines:\n{samples}"
    )


def _to_analysis(issue_id: str, payload: dict) -> AnalyzeResponse:
    return AnalyzeResponse(
        issue_id=issue_id,
        summary=payload.get("summary", ""),
        likely_causes=payload.get("likely_causes", []),
        suggested_fixes=payload.get("suggested_fixes", []),
        severity=payload.get("severity", "unknown"),
    )


_NS_SUMMARY_SYSTEM = (
    "You are an SRE summarizing the health of a Kubernetes namespace. "
    "Write for an engineer who needs a quick briefing — be specific (name services, "
    "error types, counts), skip generic advice, no padding. "
    "Use plain English, no markdown headings. "
    "Pay special attention to remote API / HTTP / DB / Kafka call failures: "
    "when you see them, always name the calling service AND the exact target "
    "(e.g. 'iom-web-integrator → GET /api/v1/bookings → 503', "
    "'offer-service → Kafka topic order-events → produce timeout'). "
    "List every distinct failing remote call in remote_api_failures."
)

_NS_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_health": {
            "type": "string",
            "enum": ["healthy", "degraded", "critical"],
            "description": "healthy = no real errors; degraded = some errors but system likely operational; critical = major failures affecting core functionality.",
        },
        "headline": {
            "type": "string",
            "description": "One crisp sentence: the overall state and the single most important thing happening.",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "level": {"type": "string"},
                    "count": {"type": "integer"},
                    "text": {"type": "string", "description": "One plain-English sentence describing what is failing and why."},
                },
                "required": ["service", "level", "count", "text"],
            },
            "description": "One entry per affected service, most severe first. Max 10.",
        },
        "top_concern": {
            "type": "string",
            "description": "The single most urgent thing an engineer should investigate first.",
        },
        "remote_api_failures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "caller": {"type": "string", "description": "Service making the call, e.g. 'iom-web-integrator'"},
                    "target": {"type": "string", "description": "Target service + endpoint, e.g. 'GET iom-order-service /v3/orders/{id}'"},
                    "error": {"type": "string", "description": "Error detail, e.g. '503 Service Unavailable', 'connection timeout'"},
                    "count": {"type": "integer"},
                },
                "required": ["caller", "target", "error", "count"],
            },
            "description": "Distinct remote/HTTP/DB/Kafka call failures observed. Empty array if none.",
        },
    },
    "required": ["overall_health", "headline", "issues", "top_concern", "remote_api_failures"],
}


def _ns_summary_context(namespace: str, minutes: int, issues: list) -> str:
    period = f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"
    lines = [f"Namespace: {namespace}  |  Window: last {period}  |  Total grouped issues: {len(issues)}\n"]
    for i in issues[:20]:  # cap at 20 to stay within token budget
        lines.append(
            f"- [{i.level.upper()}] {i.service} (×{i.count}): {i.title[:120]}\n"
            f"  Sample: {i.sample_line[:200]}"
        )
    return "\n".join(lines)


class LLMClient:
    """Provider-pluggable client. Picks OpenAI or Anthropic from settings."""

    def __init__(self) -> None:
        self._provider = None
        self._client = None
        if not settings.llm_enabled:
            return
        if settings.llm_provider == "openai":
            if settings.is_azure_openai:
                from openai import AsyncAzureOpenAI

                self._client = AsyncAzureOpenAI(
                    api_key=settings.openai_api_key,
                    azure_endpoint=settings.openai_base_url,
                    api_version=settings.openai_api_version,
                )
            else:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url or None,
                )
            self._provider = "openai"
        elif settings.llm_provider == "anthropic":
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            self._provider = "anthropic"

    async def analyze(self, issue: Issue, ctx: Optional[dict] = None) -> AnalyzeResponse:
        if self._client is None:
            return self._mock_analysis(issue)
        prompt = (
            "Diagnose this error. Be crisp and specific — name the exact failing call and reason.\n\n"
            + _issue_context(issue)
            + _analyze_code_snippet(ctx)
        )
        payload = await self._structured(prompt, _ANALYZE_SYSTEM, _ANALYZE_SCHEMA, _TOOL_NAME, max_tokens=700)
        return _to_analysis(issue.id, payload)

    async def chat(self, history: List[ChatMessage], issue: Optional[Issue], ctx: Optional[dict] = None) -> str:
        if self._client is None:
            return self._mock_chat(history, issue)
        system = _SYSTEM + _CHAT_STYLE
        if issue is not None:
            system += "\n\nThe user is asking about this specific error:\n" + _issue_context(issue)
            if ctx and ctx.get("files"):
                system += _analyze_code_snippet(ctx)
        if self._provider == "openai":
            from .state import runtime

            system += (
                "\n\nTool: call get_service_logs(service, minutes, level) to pull recent logs "
                f"for any service in the namespace '{runtime.namespace}'. Use it whenever "
                "asked to show or inspect logs. Quote the most relevant lines, note how many pods "
                "matched, and if matched_pods is 0 say the service name wasn't found."
            )
            return await self._chat_openai(history, system)
        return await self._chat_anthropic(history, system)

    async def match_code(self, issue: Issue, ctx: dict) -> CodeMatchResponse:
        base = CodeMatchResponse(
            issue_id=issue.id,
            repo=ctx.get("repo", issue.service),
            repo_url=ctx.get("repo_url", ""),
            files=[CodeMatchFile(path=f["path"], url=f.get("url", "")) for f in ctx.get("files", [])],
        )
        if not ctx.get("found"):
            base.located = False
            base.reason = ctx.get("reason", "no_match")
            base.summary = {
                "no_github_token": "No GitHub token available — set GITHUB_TOKEN or run `gh auth login`.",
                "repo_not_found": f"No repo '{settings.github_org}/{issue.service}' found (or no access).",
            }.get(base.reason, "Couldn't find matching code in the repo for this error.")
            return base
        if self._client is None:
            base.located = False
            base.summary = "[mock] Code found; set an API key + USE_MOCK=false for real code analysis."
            return base

        prompt = (
            "Localize the root cause of this production error in the provided source and propose a fix.\n\n"
            + _code_context(issue, ctx)
        )
        payload = await self._structured(prompt, _CODE_SYSTEM, _CODE_SCHEMA, _CODE_TOOL)
        base.located = bool(payload.get("located"))
        base.root_cause = payload.get("root_cause", "")
        base.suggested_fix = payload.get("suggested_fix", "")
        base.confidence = payload.get("confidence", "unknown")
        base.summary = base.root_cause[:280]
        url_by_path = {f["path"]: f.get("url", "") for f in ctx.get("files", [])}
        rel = payload.get("relevant_files") or []
        if rel:
            base.files = [
                CodeMatchFile(path=rf.get("path", ""), url=url_by_path.get(rf.get("path", ""), base.repo_url), reason=rf.get("reason", ""))
                for rf in rel
            ]
        return base

    async def summarize_namespace(self, namespace: str, minutes: int, issues: list) -> dict:
        """Plain-English health summary of all current issues in a namespace."""
        if self._client is None:
            svcs = list({i.service for i in issues})
            return {
                "overall_health": "degraded" if issues else "healthy",
                "headline": f"[mock] {namespace} has {len(issues)} active issue(s) across {len(svcs)} service(s).",
                "issues": [{"service": i.service, "level": i.level, "count": i.count, "text": f"[mock] {i.title[:80]}"} for i in issues[:5]],
                "top_concern": issues[0].title[:100] if issues else "",
            }
        prompt = _ns_summary_context(namespace, minutes, issues)
        return await self._structured(prompt, _NS_SUMMARY_SYSTEM, _NS_SUMMARY_SCHEMA, "report_ns_summary", max_tokens=1200)

    async def summarize_search(self, key: str, res: dict) -> dict:
        """Plain-English issue list for a booking from cross-service error logs."""
        if self._client is None:
            problems = [p for s in res.get("services", []) for p in s.get("problems", [])]
            return {
                "headline": f"[mock] Booking {key} has {len(problems)} issue(s).",
                "issues": [{"service": p.get("service", ""), "namespace": p.get("namespace", ""), "text": f"[mock] {p.get('message','')[:80]}"} for p in problems[:5]],
            }
        prompt = _search_summary_context(key, res)
        return await self._structured(prompt, _SEARCH_SYSTEM, _SEARCH_SCHEMA, _SEARCH_TOOL, max_tokens=900)

    async def generate_fix(self, issue: Issue, match, files_full: List[dict]) -> dict:
        """Return {pr_title, pr_summary, edits:[{path, old_string, new_string, explanation}]}."""
        if self._client is None:
            return {"pr_title": "", "pr_summary": "", "edits": []}
        prompt = "Fix the root cause of this error with minimal edits.\n\n" + _fix_context(issue, match, files_full)
        return await self._structured(prompt, _FIX_SYSTEM, _FIX_SCHEMA, _FIX_TOOL, max_tokens=2500)

    async def _structured(self, prompt: str, system: str, schema: dict, tool_name: str, max_tokens: int = 1500) -> dict:
        if self._provider == "openai":
            resp = await self._client.chat.completions.create(
                model=settings.openai_analyze_model,
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                tools=[{"type": "function", "function": {"name": tool_name, "description": "Structured result.", "parameters": schema}}],
                tool_choice={"type": "function", "function": {"name": tool_name}},
            )
            # Track token usage
            if resp.usage:
                from .analytics import record_api_usage
                import asyncio
                asyncio.ensure_future(record_api_usage(
                    "openai", settings.openai_analyze_model,
                    resp.usage.prompt_tokens, resp.usage.completion_tokens,
                ))
            msg = resp.choices[0].message
            if msg.tool_calls:
                return json.loads(msg.tool_calls[0].function.arguments or "{}")
            return {}
        resp = await self._client.messages.create(
            model=settings.analyze_model,
            max_tokens=max_tokens,
            system=system,
            tools=[{"name": tool_name, "description": "Structured result.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        return {}

    # ---- OpenAI ----
    async def _chat_openai(self, history: List[ChatMessage], system: str) -> str:
        from .source import get_service_logs

        messages = [{"role": "system", "content": system}] + [
            {"role": m.role, "content": m.content} for m in history
        ]
        msg = None
        for _ in range(4):  # allow a few tool rounds
            resp = await self._client.chat.completions.create(
                model=settings.openai_chat_model,
                max_tokens=1024,
                messages=messages,
                tools=[_GET_LOGS_TOOL],
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                if tc.function.name == "get_service_logs":
                    try:
                        minutes = int(args.get("minutes", 5) or 5)
                    except (TypeError, ValueError):
                        minutes = 5
                    try:
                        result = await get_service_logs(
                            service=str(args.get("service", "")),
                            minutes=minutes,
                            level=str(args.get("level", "") or ""),
                        )
                    except Exception as exc:
                        log.warning("get_service_logs tool error: %s", exc)
                        result = {"error": f"Failed to fetch logs: {exc}", "lines": [], "matched_pods": 0}
                else:
                    result = {"error": f"unknown tool {tc.function.name}"}
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)[:8000]})
        return (msg.content if msg else None) or "I fetched logs but couldn't compose a final answer — try narrowing the time range."

    # ---- Anthropic ----
    async def _chat_anthropic(self, history: List[ChatMessage], system: str) -> str:
        messages = [{"role": m.role, "content": m.content} for m in history]
        resp = await self._client.messages.create(
            model=settings.chat_model, max_tokens=1024, system=system, messages=messages
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    # ---- mock fallbacks (no key / mock mode) ----
    def _mock_analysis(self, issue: Issue) -> AnalyzeResponse:
        return AnalyzeResponse(
            issue_id=issue.id,
            summary=f"[mock] '{issue.service}' is emitting a recurring {issue.level} ({issue.count}x). "
            "Set an API key and USE_MOCK=false for real analysis.",
            likely_causes=[
                "Mock cause: a dependency call is failing or a value is null.",
                "Mock cause: resource limit (connections, memory) reached.",
            ],
            suggested_fixes=[
                "Mock fix: inspect the stack trace and the failing dependency.",
                "Mock fix: check pod resource limits and recent deploys.",
            ],
            severity="medium",
        )

    def _mock_chat(self, history: List[ChatMessage], issue: Optional[Issue]) -> str:
        last = history[-1].content if history else ""
        ctx = f" about '{issue.service}'" if issue else ""
        return (
            f"[mock reply] You asked{ctx}: \"{last}\". "
            "Set an API key and USE_MOCK=false for real replies."
        )


llm_client = LLMClient()
