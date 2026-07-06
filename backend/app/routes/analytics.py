from __future__ import annotations

from fastapi import APIRouter, Depends

from ..analytics import get_stats
from ..auth import require_admin

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("", dependencies=[Depends(require_admin)])
async def analytics(hours: int = 24):
    """Return usage stats for the dashboard. Requires TFR_Admin role."""
    hours = max(1, min(hours, 720))
    return await get_stats(since_hours=hours)
