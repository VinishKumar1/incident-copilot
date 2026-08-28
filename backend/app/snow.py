"""ServiceNow integration — fetch incident details and extract business identifiers.

Authentication: OAuth 2.0 Client Credentials flow via Azure AD.
Requires: SNOW_CLIENT_ID, SNOW_CLIENT_SECRET, AZURE_AD_TENANT_ID in environment.
The Azure AD app must have the ServiceNow enterprise app permission granted.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .config import settings

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# ServiceNow enterprise app ID in Azure AD
_SNOW_RESOURCE_ID = "f9fdc385-82e7-48b4-b70d-821e7392834c"

# ── Regex patterns for business identifiers ────────────────────────────────────
_PATTERNS: dict[str, re.Pattern] = {
    "booking":   re.compile(r"\b([A-Z]{3,4}[A-Z0-9]{8,12})\b"),
    "bol":       re.compile(r"\b(MAEU\d{9,12}|[A-Z]{4}\d{9,12})\b"),
    "container": re.compile(r"\b([A-Z]{4}\d{7})\b"),
    "invoice":   re.compile(r"\b(INV[-/]?\d{6,12})\b", re.IGNORECASE),
    "shipment":  re.compile(r"\b(SHP[-/]?\d{6,12})\b", re.IGNORECASE),
    "po":        re.compile(r"\b(?:PO|P\.O\.)[-/]?\s*(\d{6,12})\b", re.IGNORECASE),
}

_SEARCH_FIELDS = (
    "short_description",
    "description",
    "work_notes",
    "comments",
    "close_notes",
    "u_business_impact",
)


class SnowClient:
    """ServiceNow Table REST API client using Azure AD Client Credentials OAuth."""

    def __init__(self) -> None:
        self._base = settings.snow_instance_url.rstrip("/")
        self._cached_token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def configured(self) -> bool:
        """True when OAuth client credentials are available."""
        return bool(
            settings.snow_client_id
            and settings.snow_client_secret
            and settings.azure_ad_tenant_id
        )

    async def _get_token(self) -> str:
        """Obtain a Bearer token from Azure AD using Client Credentials flow."""
        if self._cached_token and time.time() < self._token_expiry - 300:
            return self._cached_token

        url = _TOKEN_URL.format(tenant=settings.azure_ad_tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": settings.snow_client_id,
            "client_secret": settings.snow_client_secret,
            "scope": f"{_SNOW_RESOURCE_ID}/.default",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, data=data)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Azure AD token request failed {resp.status_code}: {resp.text[:300]}"
                )
            token_data = resp.json()
            self._cached_token = token_data["access_token"]
            self._token_expiry = time.time() + token_data.get("expires_in", 3600)
            return self._cached_token

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def get_incident(self, number: str) -> dict[str, Any]:
        """Fetch a single incident by number (e.g. INC0012345)."""
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
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=headers)
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

    async def list_incidents_by_group(self, group: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recently updated active incidents assigned to a group."""
        url = f"{self._base}/api/now/table/incident"
        params = {
            "sysparm_query": f"assignment_group.name={group}^active=true^ORDERBYDESCsys_updated_on",
            "sysparm_limit": max(1, min(limit, 50)),
            "sysparm_display_value": "true",
            "sysparm_fields": ",".join(
                ["number", "sys_id", "short_description", "description", "state",
                 "priority", "urgency", "impact", "opened_at", "resolved_at",
                 "caller_id", "assignment_group", "work_notes", "comments",
                 "close_notes", "u_business_impact", "category", "subcategory"]
            ),
        }
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json().get("result", [])


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
