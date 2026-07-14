from __future__ import annotations

from typing import List

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..github import github_client
from ..llm import llm_client
from ..models import (
    AnalyzeResponse,
    CodeMatchResponse,
    FixEdit,
    FixResponse,
    Issue,
    NamespaceSummaryResponse,
    SearchResponse,
    SearchSummaryIssue,
    SearchSummaryResponse,
)
from ..source import search_key
from ..state import runtime
from ..store import store

router = APIRouter(prefix="/api", tags=["issues"])


class NamespaceRequest(BaseModel):
    namespace: str


class AdhocIssueRequest(BaseModel):
    service: str
    message: str
    level: str = ""
    namespace: str = ""


async def _get_context(issue: Issue) -> dict:
    """Fetch (and cache per issue) the GitHub code context for an issue. Best-effort:
    returns a not-found context on any failure so callers can degrade gracefully."""
    cached = store.get_cached_context(issue.id)
    if cached is not None:
        return cached
    try:
        ctx = await github_client.gather_context(issue)
    except Exception as exc:
        ctx = {"found": False, "reason": f"github_error: {exc}", "repo": issue.service, "files": []}
    store.cache_context(issue.id, ctx)
    return ctx


@router.get("/issues", response_model=List[Issue])
async def list_issues():
    return await store.list_issues()


@router.get("/issues/{issue_id}", response_model=Issue)
async def get_issue(issue_id: str):
    issue = await store.get(issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    return issue


@router.post("/issues/{issue_id}/analyze", response_model=AnalyzeResponse)
async def analyze_issue(issue_id: str, refresh: bool = False):
    issue = await store.get(issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")

    if not refresh:
        cached = store.get_cached_analysis(issue_id)
        if cached is not None:
            return cached.model_copy(update={"cached": True})

    # Pull in code context so the analysis can name the exact failing call.
    ctx = await _get_context(issue)
    analysis = await llm_client.analyze(issue, ctx)
    store.cache_analysis(analysis)
    return analysis


@router.post("/issues/{issue_id}/code-match", response_model=CodeMatchResponse)
async def code_match(issue_id: str, refresh: bool = False):
    """Find the code in the service's GitHub repo that relates to this error."""
    issue = await store.get(issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")

    if not refresh:
        cached = store.get_cached_code_match(issue_id)
        if cached is not None:
            return cached.model_copy(update={"cached": True})

    ctx = await _get_context(issue)
    result = await llm_client.match_code(issue, ctx)
    store.cache_code_match(result)
    return result


@router.post("/issues/{issue_id}/fix-it", response_model=FixResponse)
async def fix_it(issue_id: str):
    """Generate a fix from the located code and open a DRAFT PR targeting `develop`."""
    issue = await store.get(issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")

    # Need a located code match first (reuse cache or compute).
    match = store.get_cached_code_match(issue_id)
    if match is None or not match.located:
        ctx = await _get_context(issue)
        match = await llm_client.match_code(issue, ctx)
        store.cache_code_match(match)
    if not match.located or not match.files:
        return FixResponse(issue_id=issue_id, created=False, error="No located code to fix — run 'Find in code' first.")

    repo = match.repo
    async with httpx.AsyncClient(timeout=30.0) as client:
        if not await github_client.can_push(client, repo):
            return FixResponse(issue_id=issue_id, created=False, error=f"No push access to {settings.github_org}/{repo}.")

        base = "develop"
        if await github_client._branch_sha(client, repo, base) is None:
            info = await github_client.repo_info(client, repo)
            base = (info or {}).get("default_branch", "main")

        files_full = []
        for f in match.files[:2]:
            content, sha = await github_client.get_file_full(client, repo, f.path, base)
            if content is not None:
                files_full.append({"path": f.path, "content": content, "sha": sha})
    if not files_full:
        return FixResponse(issue_id=issue_id, created=False, base=base, error="Could not fetch the source files to edit.")

    fix = await llm_client.generate_fix(issue, match, files_full)
    contents = {f["path"]: f["content"] for f in files_full}
    shas = {f["path"]: f["sha"] for f in files_full}

    applied: List[FixEdit] = []
    failures: List[str] = []
    for e in fix.get("edits", []):
        p, old, new = e.get("path"), e.get("old_string", ""), e.get("new_string", "")
        if p not in contents:
            failures.append(f"{p}: file not in scope")
        elif old and old in contents[p]:
            contents[p] = contents[p].replace(old, new, 1)
            applied.append(FixEdit(path=p, explanation=e.get("explanation", "")))
        else:
            failures.append(f"{p}: target text didn't match")

    if failures or not applied:
        reason = "; ".join(failures) if failures else "the model proposed no edits"
        return FixResponse(issue_id=issue_id, created=False, base=base, title=fix.get("pr_title", ""),
                           error=f"Couldn't apply a safe fix ({reason}).")

    changed_paths = sorted({e.path for e in applied})
    changes = [{"path": p, "new_content": contents[p], "sha": shas[p]} for p in changed_paths]
    title = fix.get("pr_title") or f"fix: {issue.service} — {issue.title[:50]}"
    body = (
        "🤖 **AI-generated fix** proposed by the K8s Issue Assistant — please review carefully before merging.\n\n"
        f"**Service:** `{repo}`\n**Error:** {issue.title}\n\n"
        f"**Root cause:** {match.root_cause}\n\n"
        "**Changes:**\n" + "\n".join(f"- `{e.path}` — {e.explanation}" for e in applied) + "\n\n"
        f"{fix.get('pr_summary', '')}\n\n_Draft PR into `{base}`; not auto-merged._"
    )

    result = await github_client.create_fix_pr(repo, base, changes, title, body)
    if not result.get("ok"):
        return FixResponse(issue_id=issue_id, created=False, base=base, title=title, edits=applied,
                           error=result.get("error", "PR creation failed"))
    return FixResponse(
        issue_id=issue_id, created=True, pr_url=result["pr_url"], pr_number=result.get("pr_number"),
        branch=result.get("branch", ""), base=base, title=title, summary=fix.get("pr_summary", ""), edits=applied,
    )


@router.post("/issues/adhoc", response_model=Issue)
async def adhoc_issue(req: AdhocIssueRequest):
    """Materialize a search match into an issue (by fingerprint) so it can be analyzed,
    code-matched, and chatted about with the same endpoints as live issues."""
    from ..grouping import _title, detect_level, fingerprint

    service = (req.service or "").strip()
    message = (req.message or "").strip()
    if not service or not message:
        raise HTTPException(status_code=400, detail="service and message are required")
    fp = fingerprint(message, service)
    existing = await store.get(fp)
    if existing is not None:
        return existing
    issue = Issue(
        id=fp,
        title=_title(message),
        level=(req.level or detect_level(message)),
        service=service,
        count=1,
        sample_line=message,
        samples=[message],
        labels={"app": service, "namespace": (req.namespace or runtime.namespace)},
    )
    await store.add_adhoc(issue)
    return issue


@router.get("/search", response_model=SearchResponse)
async def search(key: str, minutes: int = 1440):
    """Find problems related to a key (booking/container/BOL number, trace id) across all services."""
    key = (key or "").strip()
    if len(key) < 3:
        raise HTTPException(status_code=400, detail="search key must be at least 3 characters")
    minutes = max(1, min(int(minutes), 2880))
    res = await search_key(key, minutes)
    store.cache_search(f"{key.lower()}|{minutes}", res)
    return SearchResponse(**res)


@router.get("/search/summary", response_model=SearchSummaryResponse)
async def search_summary(key: str, minutes: int = 1440):
    """Plain-English summary of what's wrong with a booking, from cross-service error logs."""
    key = (key or "").strip()
    if len(key) < 3:
        raise HTTPException(status_code=400, detail="search key must be at least 3 characters")
    minutes = max(1, min(int(minutes), 2880))
    res = store.get_cached_search(f"{key.lower()}|{minutes}")
    if res is None:
        res = await search_key(key, minutes)
        store.cache_search(f"{key.lower()}|{minutes}", res)

    problem_count = res.get("problem_count", 0)
    services_with_problems = [s for s in res.get("services", []) if s.get("problem_count", 0) > 0]
    base = SearchSummaryResponse(
        key=key, problem_count=problem_count, services_count=len(services_with_problems),
        namespaces=res.get("namespaces", []),
    )
    if problem_count == 0:
        base.found = res.get("total_matches", 0) > 0
        base.headline = (
            f"Booking {key} appears in the logs but has no errors right now."
            if base.found else f"No logs found for booking {key} in the selected window."
        )
        return base

    payload = await llm_client.summarize_search(key, res)
    base.found = True
    base.headline = payload.get("headline", f"Booking {key} has {problem_count} issue(s).")
    base.issues = [
        SearchSummaryIssue(service=i.get("service", ""), namespace=i.get("namespace", ""), text=i.get("text", ""))
        for i in payload.get("issues", []) if i.get("text")
    ]
    return base


@router.get("/summary", response_model=NamespaceSummaryResponse)
async def namespace_summary(refresh: bool = False):
    """Plain-English health summary of all current issues in the active namespace."""
    namespace = runtime.namespace
    minutes = settings.lookback_seconds // 60

    if not refresh:
        cached = store.get_cached_summary(namespace)
        if cached is not None:
            return cached.model_copy(update={"cached": True})

    issues = await store.list_issues()
    payload = await llm_client.summarize_namespace(namespace, minutes, issues)

    result = NamespaceSummaryResponse(
        namespace=namespace,
        minutes=minutes,
        total_issues=len(issues),
        affected_services=len({i.service for i in issues}),
        overall_health=payload.get("overall_health", "unknown"),
        headline=payload.get("headline", ""),
        issues=[
            {"service": i.get("service", ""), "level": i.get("level", ""), "count": i.get("count", 0), "text": i.get("text", "")}
            for i in payload.get("issues", [])
        ],
        top_concern=payload.get("top_concern", ""),
    )
    store.cache_summary(result)
    return result


@router.get("/namespaces", response_model=List[str])
async def list_namespaces():
    """All monitored namespaces. For grafana/loki sources, fetched from Loki's label API."""
    if settings.use_mock:
        return [runtime.namespace]
    if settings.log_source == "k8s":
        try:
            from ..k8s import k8s_client
            return await k8s_client.list_namespaces()
        except Exception as exc:
            store.last_error = f"namespace list failed: {exc}"
            return [runtime.namespace]
    # grafana / loki — query Loki for all namespace label values, filter to iom/telikos.
    try:
        from ..loki import loki_client
        return await loki_client.list_namespaces()
    except Exception as exc:
        store.last_error = f"namespace list failed: {exc}"
        return [runtime.namespace]


@router.post("/namespace")
async def set_namespace(req: NamespaceRequest):
    """Switch the actively monitored namespace, clear stale issues, and immediately
    populate the new one so the UI isn't empty until the next poll cycle."""
    import time

    from ..source import fetch_recent_errors

    name = req.namespace.strip()
    if not name:
        raise HTTPException(status_code=400, detail="namespace is required")
    runtime.namespace = name
    await store.clear()
    try:
        entries = await fetch_recent_errors()
        if entries:
            await store.ingest(entries)
        store.last_poll_ts = time.time()
        store.last_error = None
    except Exception as exc:  # e.g. forbidden namespace
        store.last_error = str(exc)
    issues = await store.list_issues()
    return {"namespace": runtime.namespace, "issues": len(issues)}


@router.get("/status")
async def status():
    return {
        "mock": settings.use_mock,
        "llm_enabled": settings.llm_enabled,
        "llm_provider": settings.llm_provider,
        "log_source": settings.log_source,
        "namespace": runtime.namespace,
        "last_poll_ts": store.last_poll_ts,
        "last_error": store.last_error,
        "endpoint": _source_endpoint(),
    }


def _source_endpoint() -> str:
    if settings.log_source == "grafana":
        return settings.grafana_url
    if settings.log_source == "k8s":
        return settings.kubeconfig or "in-cluster / default kubeconfig"
    return settings.loki_url
