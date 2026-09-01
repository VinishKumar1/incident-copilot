"""Incident resolver pipeline for the Komal/Swamy demo path.

Order:
  4. booking lifecycle
  5. RAG / L1 knowledge base
  6. if code issue → L2 agent
  7. draft a summary for human approval
"""
from __future__ import annotations

from typing import Any, Optional

from .l1_agent import l1_lookup
from .l2_agent import evidence_messages, is_code_issue, l2_solution
from .lifecycle import first_booking_id, get_booking_lifecycle
from .models import Recommendation


def _l1_solution(headline: str, root_cause: str, solution: str, confidence: float, basis: str) -> dict[str, Any]:
    return {
        "status": "L1_RECOMMENDED",
        "final_confidence": confidence,
        "headline": headline,
        "root_cause": root_cause,
        "recommended_solution": solution,
        "agents": [{
            "level": "L1",
            "name": "Knowledge Agent",
            "confidence": confidence,
            "decision": "RECOMMENDED",
            "summary": "A configured runbook matched this incident and can be applied without a code change.",
            "basis": basis,
        }],
    }


def _heuristic_l1(lifecycle: Optional[dict[str, Any]], messages: list[str]) -> dict[str, Any]:
    blob = " ".join(messages).lower()
    stuck = (lifecycle or {}).get("stuck_at")
    if stuck == "tms_acknowledgement" or "acknowledgement still pending" in blob:
        return _l1_solution(
            "Recover the missing TMS acknowledgement before retriggering the booking",
            "The booking request was accepted and SEND_TO_TMS started, but a transport order has no acknowledgement, so the workflow cannot complete.",
            "Check the outbound TMS topic and acknowledgement consumer. Replay the missing acknowledgement when the outbound message exists; otherwise perform one idempotent Send-to-TMS retrigger.",
            0.88,
            "Booking lifecycle: SEND_TO_TMS waiting on transport-order acknowledgement",
        )
    if stuck == "master_data" or "timed out" in blob or "timeout" in blob:
        return _l1_solution(
            "Stabilize the master-data dependency before retrying the booking",
            "A downstream read timed out (ports/master-data); the booking workflow stopped before confirmation.",
            "Check master-data health and latency, confirm the client timeout and circuit-breaker state, then retry after dependency recovery.",
            0.86,
            "Downstream HTTP timeout from logs + lifecycle",
        )
    if "access denied" in blob or "status code: 403" in blob or "s3exception" in blob:
        return _l1_solution(
            "Restore the document-storage workload identity's S3 access",
            "S3 is returning HTTP 403 for the document lookup, matching an expired or mis-scoped workload identity.",
            "Validate the current workload identity and S3 bucket policy, then refresh the configured credential reference.",
            0.87,
            "S3 access-denied pattern in logs",
        )
    if "consumer" in blob and "lag" in blob:
        return _l1_solution(
            "Restore billing consumer throughput and monitor lag recovery",
            "The billing consumer group is healthy but processing more slowly than the incoming event rate.",
            "Check for a stuck partition and downstream throttling, then scale the consumer within the configured partition limit.",
            0.86,
            "Kafka consumer-lag pattern in logs",
        )
    sample = messages[0] if messages else "No error logs were correlated for this incident."
    return _l1_solution(
        "Investigate correlated logs and booking state",
        sample,
        "Review the correlated evidence, confirm the owning service, and capture the next action in the incident.",
        0.7,
        "No confident runbook match — heuristic from logs",
    )


def _incident_history(incident: dict[str, Any], messages: list[str]) -> str:
    """Text passed into lifecycle diagnosis (stands in for the future history service)."""
    parts = [
        incident.get("short_description") or "",
        incident.get("description") or "",
        *messages[:12],
    ]
    return "\n".join(p for p in parts if p)[:8000]


def _pattern_text(incident: dict[str, Any], messages: list[str], lifecycle: Optional[dict[str, Any]]) -> str:
    parts = [
        incident.get("short_description") or "",
        incident.get("description") or "",
        (lifecycle or {}).get("summary") or "",
        *messages[:8],
    ]
    return "\n".join(p for p in parts if p)[:4000]


def _service_hint(evidence: list[dict[str, Any]] | None) -> str:
    for group in evidence or []:
        if group.get("service"):
            return group["service"]
    return ""


def _recommendation_from_solution(
    number: str,
    solution: dict[str, Any],
    source: str,
    kb_entry_id: str = "",
    confidence: float = 0.0,
    sources_used: list[str] | None = None,
) -> Recommendation:
    return Recommendation(
        incident_number=number,
        source=source,
        summary=solution.get("headline") or "",
        root_cause=solution.get("root_cause") or "",
        suggested_fix=solution.get("recommended_solution") or "",
        confidence=confidence or float(solution.get("final_confidence") or 0),
        confidence_label="high" if (confidence or solution.get("final_confidence") or 0) >= 0.9 else "medium",
        kb_entry_id=kb_entry_id,
        servicenow_work_note=(
            f"[AI-assisted — {source}]\n\n"
            f"Root cause: {solution.get('root_cause', '')}\n\n"
            f"Suggested fix: {solution.get('recommended_solution', '')}\n"
        ),
        sources_used=sources_used or ([source] if source else []),
    )


async def resolve_incident(
    incident: dict[str, Any],
    identifiers: dict[str, list[str]] | None,
    evidence: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Run steps 4–7 and return UI-ready pipeline + recommendation."""
    number = (incident or {}).get("number") or ""
    messages = evidence_messages(evidence)
    booking_id = first_booking_id(identifiers)
    history = _incident_history(incident or {}, messages)
    lifecycle = get_booking_lifecycle(booking_id, text=history) if booking_id else None
    service = _service_hint(evidence)
    pattern = _pattern_text(incident or {}, messages, lifecycle)

    pipeline: list[dict[str, Any]] = []

    if lifecycle:
        pipeline.append({
            "step": "lifecycle",
            "title": "Booking lifecycle",
            "status": "done",
            "detail": f"{lifecycle.get('booking_status')} · {lifecycle.get('work_process')} {lifecycle.get('work_process_status')}".strip(),
            "basis": lifecycle.get("summary") or "",
        })
    else:
        pipeline.append({
            "step": "lifecycle",
            "title": "Booking lifecycle",
            "status": "skipped",
            "detail": "No booking/shipment identifier on this incident",
            "basis": "",
        })

    rag = await l1_lookup(pattern, service=service)
    if rag is None:
        # Service-scoped search can miss a valid runbook; retry without the filter.
        rag = await l1_lookup(pattern, service="")
    if rag is not None:
        pipeline.append({
            "step": "rag",
            "title": "RAG knowledge base",
            "status": "hit",
            "detail": f"{rag.confidence:.0%} match · {rag.confidence_label} confidence",
            "basis": rag.root_cause,
            "kb_entry_id": rag.kb_entry_id,
        })
    else:
        pipeline.append({
            "step": "rag",
            "title": "RAG knowledge base",
            "status": "miss",
            "detail": "No confident runbook match — drafting from lifecycle + logs",
            "basis": "",
        })

    code_issue = is_code_issue(messages)
    if code_issue:
        solution = l2_solution(messages)
        pipeline.append({
            "step": "route",
            "title": "Agent routing",
            "status": "l2",
            "detail": "Code defect detected → L2 Code Reasoning Agent",
            "basis": (solution.get("code_change") or {}).get("file") or "",
        })
        rec = _recommendation_from_solution(number, solution, source="l2", confidence=0.93, sources_used=["logs", "l2"])
    elif rag is not None:
        solution = _l1_solution(
            rag.summary or rag.root_cause[:200],
            rag.root_cause,
            rag.suggested_fix,
            rag.confidence,
            f"Knowledge base entry {rag.kb_entry_id or ''}".strip(),
        )
        pipeline.append({
            "step": "route",
            "title": "Agent routing",
            "status": "l1",
            "detail": "Stay on L1 Knowledge Agent (runbook match, not a code defect)",
            "basis": rag.kb_entry_id or "",
        })
        rec = rag
        rec.incident_number = number
        sources = list(rec.sources_used or [])
        sources.append("knowledge_base")
        if lifecycle:
            sources.append("lifecycle")
        rec.sources_used = list(dict.fromkeys(sources))
    else:
        solution = _heuristic_l1(lifecycle, messages)
        pipeline.append({
            "step": "route",
            "title": "Agent routing",
            "status": "l1",
            "detail": "Stay on L1 — operational pattern from lifecycle/logs",
            "basis": solution["agents"][0]["basis"],
        })
        rec = _recommendation_from_solution(
            number, solution, source="lifecycle" if lifecycle else "logs",
            confidence=float(solution["final_confidence"]),
            sources_used=["lifecycle", "logs"] if lifecycle else ["logs"],
        )

    pipeline.append({
        "step": "summary",
        "title": "Suggested summary",
        "status": "pending_approval",
        "detail": "Human must approve before this is written to the knowledge bank",
        "basis": rec.summary,
    })

    return {
        "pipeline": pipeline,
        "booking_lifecycle": lifecycle,
        "agent_solution": solution,
        "recommendation": rec.model_dump() if hasattr(rec, "model_dump") else rec,
        "pattern_text": pattern,
        "service": service,
    }
