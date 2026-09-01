from __future__ import annotations

import pytest

from app import kb
from app.resolver import resolve_incident


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(kb, "_KB_DB_PATH", tmp_path / "kb.db")
    monkeypatch.setattr(kb, "_sqlite_conn", None)
    monkeypatch.setattr(kb, "_USE_POSTGRES", False)
    monkeypatch.setattr(kb.settings, "use_mock", True)


@pytest.mark.asyncio
async def test_tms_incident_hits_rag_and_stays_l1():
    await kb.seed_mock_kb()
    resolution = await resolve_incident(
        {
            "number": "INC0098421",
            "short_description": "Bookings remain pending after submission",
            "description": "Booking GHDGW54NC00 remains pending after Send to TMS was requested. One of two transport orders has not received a TMS acknowledgement.",
        },
        {"booking": ["GHDGW54NC00"]},
        [{
            "service": "transport-order-feedback",
            "logs": [
                {"message": "TMS acknowledgement still pending for transport order TO-78451201 after 20 minutes"},
                {"message": "Booking GHDGW54NC00 work process SEND_TO_TMS changed to STARTED"},
            ],
        }],
    )
    assert resolution["booking_lifecycle"]["stuck_at"] == "tms_acknowledgement"
    statuses = {step["step"]: step["status"] for step in resolution["pipeline"]}
    assert statuses["lifecycle"] == "done"
    assert statuses["rag"] == "hit"
    assert statuses["route"] == "l1"
    assert resolution["agent_solution"]["status"] == "L1_RECOMMENDED"


@pytest.mark.asyncio
async def test_stack_trace_escalates_to_l2_even_if_rag_could_match():
    await kb.seed_mock_kb()
    resolution = await resolve_incident(
        {
            "number": "INC0098360",
            "short_description": "Booking confirmation fails during response mapping",
            "description": "Booking BK-90218431 fails with a NullPointerException in BookingResponseMapper.",
        },
        {"booking": ["BK-90218431"]},
        [{
            "service": "telikos-booking-service",
            "logs": [
                {"message": "NullPointerException at BookingResponseMapper.mapCarrier(BookingResponseMapper.java:118) for booking BK-90218431"},
            ],
        }],
    )
    statuses = {step["step"]: step["status"] for step in resolution["pipeline"]}
    assert statuses["route"] == "l2"
    assert resolution["agent_solution"]["status"] == "L2_RECOMMENDED"
    assert "BookingResponseMapper" in resolution["agent_solution"]["code_change"]["file"]
