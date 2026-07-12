"""ServiceNow incident search routes."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_user
from ..snow import snow_client
from ..source import search_key

router = APIRouter(prefix="/api/snow", tags=["snow"])


@router.get("/incident/{number}", dependencies=[Depends(require_user)])
async def get_incident(number: str, minutes: int = 360) -> dict[str, Any]:
    """Fetch a ServiceNow incident, extract business identifiers, and search Loki for each.

    Returns:
    - incident: cleaned incident record
    - identifiers: dict of {type: [values]} found in the incident text
    - loki_results: dict of {identifier_value: search_result} for each extracted key
    """
    if not snow_client.configured:
        raise HTTPException(
            status_code=503,
            detail="ServiceNow not configured — set SNOW_USERNAME and SNOW_PASSWORD",
        )

    # Normalise: strip whitespace, uppercase
    number = number.strip().upper()
    if not number.startswith("INC"):
        raise HTTPException(status_code=400, detail="Incident number must start with INC")

    # Fetch incident + extract identifiers
    try:
        result = await snow_client.get_incident_with_identifiers(number)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow error: {exc}")

    # Gather all unique identifier values across all types
    identifiers: dict[str, list[str]] = result["identifiers"]
    all_keys: list[str] = []
    for values in identifiers.values():
        all_keys.extend(values)
    all_keys = list(dict.fromkeys(all_keys))  # deduplicate preserving order

    # Search Loki for each identifier in parallel (cap at 10 to avoid overloading)
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
        "minutes": minutes,
    }


@router.get("/status", dependencies=[Depends(require_user)])
async def snow_status() -> dict[str, Any]:
    """Returns whether ServiceNow is configured."""
    return {
        "configured": snow_client.configured,
        "instance_url": snow_client._base if snow_client.configured else None,
    }
