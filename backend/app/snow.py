"""ServiceNow integration — fetch incident details and extract business identifiers."""
from __future__ import annotations

import re
from typing import Any

import httpx

from .config import settings

# ── Regex patterns for business identifiers ────────────────────────────────────
# These cover the most common Maersk identifiers that would appear in log errors.
_PATTERNS: dict[str, re.Pattern] = {
    "booking":   re.compile(r"\b([A-Z]{3,4}[A-Z0-9]{8,12})\b"),           # e.g. MHXHTFLMG9P9
    "bol":       re.compile(r"\b(MAEU\d{9,12}|[A-Z]{4}\d{9,12})\b"),      # Bill of Lading
    "container": re.compile(r"\b([A-Z]{4}\d{7})\b"),                       # ISO container e.g. MRKU1234567
    "invoice":   re.compile(r"\b(INV[-/]?\d{6,12})\b", re.IGNORECASE),
    "shipment":  re.compile(r"\b(SHP[-/]?\d{6,12})\b", re.IGNORECASE),
    "po":        re.compile(r"\b(?:PO|P\.O\.)[-/]?\s*(\d{6,12})\b", re.IGNORECASE),
}

# Fields from the SNOW incident we search for identifiers
_SEARCH_FIELDS = (
    "short_description",
    "description",
    "work_notes",
    "comments",
    "close_notes",
    "u_business_impact",
)


class SnowClient:
    """Thin wrapper around the ServiceNow Table REST API."""

    def __init__(self) -> None:
        self._base = settings.snow_instance_url.rstrip("/")
        self._auth = (settings.snow_username, settings.snow_password)

    @property
    def configured(self) -> bool:
        return bool(settings.snow_username and settings.snow_password)

    async def get_incident(self, number: str) -> dict[str, Any]:
        """Fetch a single incident by number (e.g. INC0012345).

        Returns the raw incident record dict from SNOW.
        Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        url = f"{self._base}/api/now/table/incident"
        params = {
            "sysparm_query": f"number={number.upper()}",
            "sysparm_limit": 1,
            "sysparm_fields": ",".join(
                ["number", "sys_id", "short_description", "description",
                 "state", "priority", "urgency", "impact", "opened_at",
                 "resolved_at", "caller_id", "assignment_group",
                 "work_notes", "comments", "close_notes",
                 "u_business_impact", "category", "subcategory"]
            ),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, auth=self._auth,
                                    headers={"Accept": "application/json"})
            resp.raise_for_status()
            records = resp.json().get("result", [])
            if not records:
                raise ValueError(f"Incident {number} not found in ServiceNow")
            return records[0]

    async def get_incident_with_identifiers(self, number: str) -> dict[str, Any]:
        """Fetch incident and extract all business identifiers from the text fields."""
        incident = await self.get_incident(number)
        identifiers = extract_identifiers(incident)
        return {
            "incident": _clean_incident(incident),
            "identifiers": identifiers,
        }


def extract_identifiers(incident: dict[str, Any]) -> dict[str, list[str]]:
    """Extract business identifiers (booking, BOL, container, etc.) from incident fields."""
    # Concatenate all text fields
    text = " ".join(
        str(incident.get(f) or "")
        for f in _SEARCH_FIELDS
    )

    found: dict[str, list[str]] = {}
    for id_type, pattern in _PATTERNS.items():
        matches = list({m.group(1) if m.lastindex else m.group(0)
                        for m in pattern.finditer(text)})
        if matches:
            found[id_type] = matches

    return found


def _clean_incident(incident: dict[str, Any]) -> dict[str, Any]:
    """Return a clean dict with only display-relevant fields."""
    def val(field: str) -> str:
        v = incident.get(field)
        if isinstance(v, dict):
            return v.get("display_value") or v.get("value") or ""
        return str(v or "")

    state_map = {"1": "New", "2": "In Progress", "3": "On Hold",
                 "4": "Resolved", "5": "Closed", "6": "Cancelled"}
    priority_map = {"1": "Critical", "2": "High", "3": "Moderate",
                    "4": "Low", "5": "Planning"}

    return {
        "number":             val("number"),
        "sys_id":             val("sys_id"),
        "short_description":  val("short_description"),
        "description":        val("description"),
        "state":              state_map.get(val("state"), val("state")),
        "priority":           priority_map.get(val("priority"), val("priority")),
        "opened_at":          val("opened_at"),
        "resolved_at":        val("resolved_at"),
        "caller":             val("caller_id"),
        "assignment_group":   val("assignment_group"),
        "category":           val("category"),
        "close_notes":        val("close_notes"),
    }


snow_client = SnowClient()
