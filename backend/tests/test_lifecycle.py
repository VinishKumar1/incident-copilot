from __future__ import annotations

from app.lifecycle import first_booking_id, get_booking_lifecycle


def test_first_booking_id_prefers_booking_over_shipment():
    assert first_booking_id({"shipment": ["SHP-1"], "booking": ["GHDGW54NC00"]}) == "GHDGW54NC00"
    assert first_booking_id({"shipment": ["SHP-84219373"]}) == "SHP-84219373"
    assert first_booking_id({}) is None


def test_known_booking_returns_tms_stuck_state():
    snap = get_booking_lifecycle("GHDGW54NC00")
    assert snap["stuck_at"] == "tms_acknowledgement"
    assert snap["work_process"] == "SEND_TO_TMS"
    assert any(o["acknowledgement"] == "PENDING" for o in snap["transport_orders"])


def test_mapping_failure_never_started_tms():
    snap = get_booking_lifecycle("BK-90218431")
    assert snap["stuck_at"] == "response_mapping"
    assert snap["work_process_status"] == "FAILED"


def test_customs_booking_is_stuck_on_send_to_customs():
    snap = get_booking_lifecycle("HNZSJN7MMNK")
    assert snap["work_process"] == "SEND_TO_CUSTOMS"
    assert snap["stuck_at"] == "customs_milestone"
    other = get_booking_lifecycle("HNMQBWJSBRW")
    assert other["stuck_at"] == "customs_milestone"


def test_unknown_booking_is_explicit_not_found():
    snap = get_booking_lifecycle("NO-SUCH-BOOKING")
    assert snap["booking_status"] == "UNKNOWN"
    assert "NO-SUCH-BOOKING" in snap["summary"]
