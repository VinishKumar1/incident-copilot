"""Tests for the ServiceNow client — snow.py"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.snow import extract_identifiers, _clean_incident, SnowClient


# ── extract_identifiers ────────────────────────────────────────────────────────

def test_extract_identifiers_finds_booking_number():
    incident = {
        "short_description": "Error processing booking MHXHTFLMG9P9",
        "description": "",
    }
    ids = extract_identifiers(incident)
    assert "booking" in ids
    assert "MHXHTFLMG9P9" in ids["booking"]


def test_extract_identifiers_finds_container_number():
    incident = {
        "short_description": "Container MRKU1234567 not found",
        "description": "",
    }
    ids = extract_identifiers(incident)
    assert "container" in ids
    assert "MRKU1234567" in ids["container"]


def test_extract_identifiers_finds_invoice():
    incident = {
        "short_description": "Invoice INV-123456789 payment failed",
        "description": "",
    }
    ids = extract_identifiers(incident)
    assert "invoice" in ids


def test_extract_identifiers_empty_incident():
    ids = extract_identifiers({})
    assert ids == {}


def test_extract_identifiers_searches_multiple_fields():
    incident = {
        "short_description": "Some error",
        "description": "Booking MHXHTFLMG9P9 failed",
        "work_notes": "Container MRKU1234567 also affected",
        "comments": "",
    }
    ids = extract_identifiers(incident)
    assert "booking" in ids
    assert "container" in ids


# ── _clean_incident ────────────────────────────────────────────────────────────

def test_clean_incident_maps_state_and_priority():
    raw = {
        "number": "INC0012345",
        "sys_id": "abc123",
        "short_description": "Test incident",
        "description": "Full description",
        "state": "1",
        "priority": "2",
        "opened_at": "2026-07-12 10:00:00",
        "resolved_at": "",
        "caller_id": {"display_value": "John Doe"},
        "assignment_group": {"display_value": "Platform Team"},
        "category": "software",
        "close_notes": "",
    }
    cleaned = _clean_incident(raw)
    assert cleaned["state"] == "New"
    assert cleaned["priority"] == "High"
    assert cleaned["number"] == "INC0012345"
    assert cleaned["caller"] == "John Doe"
    assert cleaned["assignment_group"] == "Platform Team"


def test_clean_incident_handles_missing_fields():
    cleaned = _clean_incident({})
    assert cleaned["number"] == ""
    assert cleaned["state"] == ""


# ── SnowClient ─────────────────────────────────────────────────────────────────

def test_snow_client_configured_false_when_no_credentials(monkeypatch):
    from app.config import settings
    original_client_id = settings.snow_client_id
    original_secret = settings.snow_client_secret
    settings.snow_client_id = ""
    settings.snow_client_secret = ""
    client = SnowClient()
    assert not client.configured
    settings.snow_client_id = original_client_id
    settings.snow_client_secret = original_secret


@pytest.mark.asyncio
async def test_get_incident_raises_on_empty_result():
    client = SnowClient()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"result": []}

    with patch.object(client, "_get_token", new=AsyncMock(return_value="fake-token")):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ValueError, match="not found"):
                await client.get_incident("INC9999999")


@pytest.mark.asyncio
async def test_get_incident_with_identifiers_returns_combined_result():
    client = SnowClient()
    fake_incident = {
        "number": "INC0012345",
        "sys_id": "abc",
        "short_description": "Booking MHXHTFLMG9P9 failed",
        "description": "",
        "state": "1",
        "priority": "2",
        "opened_at": "",
        "resolved_at": "",
        "caller_id": "",
        "assignment_group": "",
        "category": "",
        "close_notes": "",
        "work_notes": "",
        "comments": "",
        "u_business_impact": "",
    }
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"result": [fake_incident]}

    with patch.object(client, "_get_token", new=AsyncMock(return_value="fake-token")):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.get_incident_with_identifiers("INC0012345")

    assert result["incident"]["number"] == "INC0012345"
    assert "booking" in result["identifiers"]
