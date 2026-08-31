"""L1 Agent — confidence-gated knowledge-base lookup.

Searches the RAG knowledge base (kb.py) for a similar, previously-verified
incident pattern. Returns a Recommendation only when the match clears
KB_CONFIDENCE_THRESHOLD; otherwise returns None so the caller falls back to a
fresh analyze()/match_code() (+ web) call instead of trusting a weak match.
"""
from __future__ import annotations

from typing import Optional

from . import kb
from .config import settings
from .models import Recommendation


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.9:
        return "high"
    if confidence >= settings.kb_confidence_threshold:
        return "medium"
    return "low"


def _format_work_note(root_cause: str, fix_summary: str, confidence: float) -> str:
    return (
        f"[AI-assisted — matched a previously verified incident, {confidence:.0%} similar]\n\n"
        f"Root cause: {root_cause}\n\n"
        f"Suggested fix: {fix_summary}\n\n"
        "Please review before applying — this is a match to a past incident, not a fresh "
        "analysis of this one."
    )


async def l1_lookup(pattern_text: str, service: str = "") -> Optional[Recommendation]:
    """Returns a Recommendation drawn from the knowledge base, or None if there's no
    confident match — including when the KB or embeddings aren't configured yet, in
    which case kb.search() itself returns an empty list."""
    matches = await kb.search(pattern_text, service=service, top_k=1)
    if not matches:
        return None
    match = matches[0]
    if match.confidence < settings.kb_confidence_threshold:
        return None
    entry = match.entry
    return Recommendation(
        source="l1",
        summary=entry.root_cause[:200],
        root_cause=entry.root_cause,
        suggested_fix=entry.fix_summary,
        confidence=match.confidence,
        confidence_label=_confidence_label(match.confidence),
        kb_entry_id=entry.id,
        servicenow_work_note=_format_work_note(entry.root_cause, entry.fix_summary, match.confidence),
        sources_used=["knowledge_base"],
    )
