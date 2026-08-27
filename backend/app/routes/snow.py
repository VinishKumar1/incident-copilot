"""ServiceNow incident routes — read, cross-reference with logs, and (new) draft an
AI-assisted recommendation ready to paste into the incident as a work note."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..analytics import record_kb_feedback
from ..auth import require_user, require_admin
from ..l1_agent import l1_lookup
from ..llm import llm_client
from ..models import MarkUsedRequest, Recommendation
from ..snow import snow_client
from ..source import search_key
from ..websearch import web_search_client

router = APIRouter(prefix="/api/snow", tags=["snow"])


async def _lookup_incident_and_logs(number: str, minutes: int) -> dict[str, Any]:
    """Shared by /incident/{number} and /incident/{number}/analyze: fetch the incident,
    extract business identifiers, and cross-search logs for each one. Same validation
    and error handling get_incident always had — behavior there is unchanged."""
    if not snow_client.configured:
        raise HTTPException(
            status_code=503,
            detail="ServiceNow not configured — set SNOW_USERNAME and SNOW_PASSWORD",
        )

    number = number.strip().upper()
    if not number.startswith("INC"):
        raise HTTPException(status_code=400, detail="Incident number must start with INC")

    try:
        result = await snow_client.get_incident_with_identifiers(number)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow error: {exc}")

    identifiers: dict[str, list[str]] = result["identifiers"]
    all_keys: list[str] = []
    for values in identifiers.values():
        all_keys.extend(values)
    all_keys = list(dict.fromkeys(all_keys))  # deduplicate preserving order

    loki_results: dict[str, Any] = {}
    if all_keys:
        async def _search(key: str) -> tuple[str, Any]:
            try:
                return key, await search_key(key, minutes=minutes)
            except Exception as exc:
                return key, {"error": str(exc), "issues": [], "namespaces": []}

        tasks = [_search(k) for k in all_keys[:10]]
        pairs = await asyncio.gather(*tasks)
        loki_results = dict(pairs)

    return {
        "incident": result["incident"],
        "identifiers": identifiers,
        "loki_results": loki_results,
        "searched_keys": all_keys[:10],
    }


@router.get("/incident/{number}", dependencies=[Depends(require_admin)])
async def get_incident(number: str, minutes: int = 43200) -> dict[str, Any]:
    """Fetch a ServiceNow incident, extract business identifiers, and search Loki for each.

    Returns:
    - incident: cleaned incident record
    - identifiers: dict of {type: [values]} found in the incident text
    - loki_results: dict of {identifier_value: search_result} for each extracted key
    """
    data = await _lookup_incident_and_logs(number, minutes)
    return {**data, "minutes": minutes}


def _pattern_text(incident: dict, loki_results: dict) -> str:
    """Normalized text the knowledge base embeds against and searches with — the
    incident's own description plus a compact digest of what the logs actually show."""
    parts = [incident.get("short_description", ""), incident.get("description", "")]
    for res in loki_results.values():
        for svc in (res or {}).get("services", []):
            for p in svc.get("problems", [])[:3]:
                parts.append(f"{svc.get('service', '')}: {p.get('message', '')}")
    return "\n".join(p for p in parts if p)[:4000]


def _service_hint(loki_results: dict) -> str:
    """Best-effort single service name to scope the KB search, if the logs point at one."""
    for res in loki_results.values():
        services = (res or {}).get("services", [])
        if services:
            return services[0].get("service", "") or ""
    return ""


def _context_text(incident: dict, loki_results: dict, web_results: list) -> str:
    lines = [
        f"Incident: {incident.get('number', '')} — {incident.get('short_description', '')}",
        f"Description: {incident.get('description', '')}",
        f"Priority: {incident.get('priority', '')}  State: {incident.get('state', '')}",
        "",
        "Log evidence found across services:",
    ]
    any_logs = False
    for res in loki_results.values():
        for svc in (res or {}).get("services", []):
            for p in svc.get("problems", [])[:5]:
                any_logs = True
                lines.append(f"- [{svc.get('namespace', '')}/{svc.get('service', '')}] {p.get('message', '')}")
    if not any_logs:
        lines.append("(no matching error log lines found for the extracted identifiers)")
    if web_results:
        lines.append("")
        lines.append("Approved web sources:")
        for w in web_results:
            lines.append(f"- {w.get('title', '')} ({w.get('url', '')}): {w.get('content', '')[:300]}")
    return "\n".join(lines)


@router.post("/incident/{number}/analyze", dependencies=[Depends(require_admin)])
async def analyze_incident(number: str, minutes: int = 43200) -> dict[str, Any]:
    """Phase 1 of the incident copilot (design doc §3): reuses the identifier + log
    search above, tries the knowledge base first (L1), and falls back to a fresh LLM
    analysis — optionally enriched with an approved web search — when there's no
    confident match. Always returns a Recommendation with a ready-to-paste
    servicenow_work_note. Nothing here writes back to ServiceNow itself; that's a
    later phase (see the design doc §9) — a human pastes this in for now."""
    data = await _lookup_incident_and_logs(number, minutes)
    incident, loki_results = data["incident"], data["loki_results"]

    pattern_text = _pattern_text(incident, loki_results)
    service = _service_hint(loki_results)

    recommendation = await l1_lookup(pattern_text, service=service)
    if recommendation is None:
        web_results: list = []
        if web_search_client.configured:
            web_results = await web_search_client.search(incident.get("short_description", ""))
        context = _context_text(incident, loki_results, web_results)
        payload = await llm_client.analyze_incident(context)
        sources = ["logs"] + (["web"] if web_results else [])
        recommendation = Recommendation(
            source="llm",
            summary=payload.get("summary", ""),
            root_cause=payload.get("root_cause", ""),
            suggested_fix=payload.get("suggested_fix", ""),
            confidence_label=payload.get("confidence", "unknown"),
            servicenow_work_note=payload.get("servicenow_work_note", ""),
            sources_used=sources,
        )
    recommendation.incident_number = number

    return {
        "incident": incident,
        "identifiers": data["identifiers"],
        "recommendation": recommendation.model_dump(),
    }


@router.post("/incident/{number}/mark-used", dependencies=[Depends(require_admin)])
async def mark_used(number: str, body: MarkUsedRequest) -> dict[str, Any]:
    """The Phase-1 usage signal (design doc §3): did the engineer actually use the
    drafted recommendation? No formal verification queue exists yet — that's a later
    phase — this is deliberately lightweight, just enough for the knowledge-base
    backfill to start from real signal instead of an empty table."""
    await record_kb_feedback(number.strip().upper(), body.used, body.edited, body.notes)
    return {"ok": True}


@router.get("/status", dependencies=[Depends(require_admin)])
async def snow_status() -> dict[str, Any]:
    """Returns whether ServiceNow is configured."""
    return {
        "configured": snow_client.configured,
        "auth_mode": "client_credentials" if snow_client.configured else "none",
        "instance_url": snow_client._base if snow_client.configured else None,
    }
