from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends

from ..analytics import get_stats
from ..auth import require_admin
from ..config import settings

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("", dependencies=[Depends(require_admin)])
async def analytics(hours: int = 24):
    """Return usage stats for the dashboard. Requires TFR_Admin role."""
    hours = max(1, min(hours, 720))
    return await get_stats(since_hours=hours)


@router.get("/vibe-usage", dependencies=[Depends(require_admin)])
async def vibe_usage():
    """Fetch live OpenAI key spend + budget from the Maersk Vibe proxy."""
    base = str(settings.openai_base_url).rstrip("/")
    if not base or not settings.openai_api_key:
        return {"error": "Vibe proxy not configured"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base}/key/info",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json().get("info", {})
            return {
                "key_alias": data.get("key_alias", ""),
                "spend": round(data.get("spend", 0), 4),
                "max_budget": data.get("max_budget"),
                "budget_duration": data.get("budget_duration"),
                "budget_reset_at": data.get("budget_reset_at"),
                "expires": data.get("expires"),
                "models": data.get("models", []),
                "last_active": data.get("last_active"),
            }
    except Exception as exc:
        return {"error": str(exc)}
