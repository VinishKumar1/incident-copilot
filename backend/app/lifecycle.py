"""Booking lifecycle lookup — where a booking is in its real workflow.

Demo/mock data is keyed by booking id so Incident Search can show a lifecycle
trail without a live booking API. Swap `_MOCK_BOOKINGS` for a Telikos client
later; the return shape stays the same.
"""
from __future__ import annotations

from typing import Any, Optional


def _steps(accepted: str, started: str, feedback: str, completed: str) -> list[dict[str, Any]]:
    states = {
        "done": ("done", "✓"),
        "active": ("active", "!"),
        "pending": ("pending", "4"),
        "failed": ("failed", "✕"),
    }

    def one(label: str, spec: str) -> dict[str, Any]:
        state, detail = spec.split("|", 1) if "|" in spec else (spec, "")
        css, mark = states.get(state, states["pending"])
        return {"label": label, "state": css, "mark": mark, "detail": detail}

    return [
        one("API accepted", accepted),
        one("Workflow started", started),
        one("Downstream feedback", feedback),
        one("Completed", completed),
    ]


# Known demo bookings. Unknown ids return a generic "not found in lifecycle store".
_MOCK_BOOKINGS: dict[str, dict[str, Any]] = {
    "GHDGW54NC00": {
        "booking_id": "GHDGW54NC00",
        "booking_status": "BOOKED",
        "work_process": "SEND_TO_TMS",
        "work_process_status": "STARTED",
        "stuck_at": "tms_acknowledgement",
        "summary": "Send-to-TMS was initiated successfully, but acknowledgement is missing for 1 of 2 transport orders.",
        "api_status": 202,
        "headline_tag": "Awaiting acknowledgement",
        "transport_orders": [
            {"number": "TO-78451201", "version": 1, "acknowledgement": "PENDING", "received_at": None},
            {"number": "TO-78451202", "version": 1, "acknowledgement": "ACCEPTED", "received_at": "2026-08-28T09:43:21Z"},
        ],
        "steps": _steps("done|HTTP 202", "done|SEND_TO_TMS", "active|1 of 2 pending", "pending|Waiting"),
    },
    "BK-90218431": {
        "booking_id": "BK-90218431",
        "booking_status": "FAILED",
        "work_process": "CONFIRMATION",
        "work_process_status": "FAILED",
        "stuck_at": "response_mapping",
        "summary": "Confirmation failed while mapping the booking response; the SEND_TO_TMS workflow never started.",
        "api_status": 500,
        "headline_tag": "Confirmation failed",
        "transport_orders": [],
        "steps": _steps("done|HTTP 500", "failed|CONFIRMATION", "pending|Not started", "pending|Blocked"),
    },
    "SHP-84219373": {
        "booking_id": "SHP-84219373",
        "booking_status": "PENDING",
        "work_process": "MASTER_DATA",
        "work_process_status": "FAILED",
        "stuck_at": "master_data",
        "summary": "Booking cannot load port master data; the workflow stopped before confirmation.",
        "api_status": 504,
        "headline_tag": "Dependency timeout",
        "transport_orders": [],
        "steps": _steps("done|HTTP 504", "failed|MASTER_DATA", "pending|Not started", "pending|Blocked"),
    },
}


def first_booking_id(identifiers: dict[str, list[str]] | None) -> Optional[str]:
    if not identifiers:
        return None
    for key in ("booking", "shipment"):
        values = identifiers.get(key) or []
        if values:
            return values[0]
    return None


def get_booking_lifecycle(booking_id: str | None) -> Optional[dict[str, Any]]:
    """Return a lifecycle snapshot, or None when there is no booking id / no record."""
    if not booking_id:
        return None
    record = _MOCK_BOOKINGS.get(booking_id.strip())
    if record is None:
        return {
            "booking_id": booking_id,
            "booking_status": "UNKNOWN",
            "work_process": "",
            "work_process_status": "UNKNOWN",
            "stuck_at": None,
            "summary": f"No lifecycle record found for {booking_id} in the booking store.",
            "api_status": None,
            "headline_tag": "Not in store",
            "transport_orders": [],
            "steps": _steps("pending|Unknown", "pending|Unknown", "pending|Unknown", "pending|Unknown"),
        }
    return dict(record)
