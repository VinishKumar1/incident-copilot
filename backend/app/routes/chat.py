from __future__ import annotations

from fastapi import APIRouter

from ..llm import llm_client
from ..models import ChatRequest, ChatResponse
from ..store import store

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    issue = await store.get(req.issue_id) if req.issue_id else None
    # Reuse code context if it's already cached (from Analyze/Find-in-code) — no extra fetch.
    ctx = store.get_cached_context(req.issue_id) if req.issue_id else None
    reply = await llm_client.chat(req.messages, issue, ctx)
    return ChatResponse(reply=reply)
