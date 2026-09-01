from __future__ import annotations

from app.lifecycle import (
    assess_cancellation,
    diagnose_booking_lifecycle,
    first_booking_id,
    get_booking_lifecycle,
)


def test_first_booking_id_prefers_booking_over_shipment():
    assert first_booking_id({"shipment": ["SHP-1"], "booking": ["GHDGW54NC00"]}) == "GHDGW54NC00"
    assert first_booking_id({"shipment": ["SHP-84219373"]}) == "SHP-84219373"
    assert first_booking_id({}) is None


def test_known_booking_returns_tms_stuck_state():
    snap = get_booking_lifecycle("GHDGW54NC00")
    assert snap["stuck_at"] == "tms_acknowledgement"
    assert snap["work_process"] == "SEND_TO_TMS"
    assert snap["documented_flow"] is True
    assert any(o["acknowledgement"] == "PENDING" for o in snap["transport_orders"])
    labels = [s["label"] for s in snap["steps"]]
    assert "Send to TMS" in labels
    assert "Invoice generated" in labels


def test_mapping_failure_never_started_tms():
    snap = get_booking_lifecycle("BK-90218431")
    assert snap["stuck_at"] == "response_mapping"
    assert snap["work_process_status"] == "FAILED"


def test_customs_text_is_outside_documented_pdf_flow():
    snap = get_booking_lifecycle("HNZSJN7MMNK")
    assert snap["documented_flow"] is False
    assert snap["work_process"] != "SEND_TO_CUSTOMS"
    assert snap["stuck_at"] is None
    other = get_booking_lifecycle("HNMQBWJSBRW")
    assert other["documented_flow"] is False


def test_unknown_booking_is_explicit_not_found():
    snap = get_booking_lifecycle("NO-SUCH-BOOKING")
    assert snap["booking_status"] == "UNKNOWN"
    assert "NO-SUCH-BOOKING" in snap["summary"]


def test_diagnose_no_routes_204():
    snap = diagnose_booking_lifecycle("Search failed with 204 Response — No routes available for the selected combination.")
    assert snap["stuck_at"] == "no_routes"
    assert snap["work_process"] == "SEARCH"
    assert snap["api_status"] == 204


def test_diagnose_no_offers_206():
    snap = diagnose_booking_lifecycle("206 Response → No offers available. Pricing is not configured.")
    assert snap["stuck_at"] == "no_offers"
    assert snap["work_process"] == "SEARCH"


def test_diagnose_tms_stuck_from_incident_text():
    snap = diagnose_booking_lifecycle(
        "Booking GHDGW54NC00 remains pending after Send to TMS was requested. "
        "TMS acknowledgement still pending for transport order TO-78451201. "
        "TMS acknowledgement ACCEPTED for transport order TO-78451202 received_at 2026-08-28T09:43:21Z.",
        booking_id="GHDGW54NC00",
    )
    assert snap["stuck_at"] == "tms_acknowledgement"
    assert snap["work_process"] == "SEND_TO_TMS"
    assert snap["cancellation"]["p13"]["any_container_executed"] is False
    numbers = {o["number"]: o["acknowledgement"] for o in snap["transport_orders"]}
    assert numbers["TO-78451201"] == "PENDING"
    assert numbers["TO-78451202"] == "ACCEPTED"


def test_diagnose_vat_partner_blocks_invoicing():
    snap = diagnose_booking_lifecycle(
        "Activity Plan shows Ready for Invoicing → Pending. VAT Partner Code is missing."
    )
    assert snap["stuck_at"] == "vat_partner"
    assert snap["work_process"] == "INVOICE"


def test_diagnose_idoc_failure():
    snap = diagnose_booking_lifecycle("IDOC failure in Finance — master data alignment issue.")
    assert snap["stuck_at"] == "idoc_failure"
    assert snap["work_process"] == "FINANCE"


def test_diagnose_fro_vendor_not_assigned():
    snap = diagnose_booking_lifecycle("FRO is created but the vendor is not assigned; the rate is not set up in MRE.")
    assert snap["stuck_at"] == "vendor_assignment"


def test_cancellation_p6_blocks_when_executed():
    result = assess_cancellation("", execution_status="executed", any_container_executed=True, eta_passed=False, cargo_facility_date_reached=False)
    assert result["allowed"] is False
    assert "p6_executed" in result["blocked_by"]
    assert result["p6"]["blocks"] is True


def test_cancellation_p6_blocks_when_facility_date_reached():
    result = assess_cancellation(
        "",
        execution_status="not_started",
        any_container_executed=False,
        eta_passed=False,
        cargo_facility_date_reached=True,
        trade_type="export",
    )
    assert result["allowed"] is False
    assert "p6_facility_date" in result["blocked_by"]


def test_cancellation_p13_blocks_when_any_container_executed():
    result = assess_cancellation(
        "",
        execution_status="in_execution",
        any_container_executed=True,
        eta_passed=False,
        cargo_facility_date_reached=False,
    )
    assert result["allowed"] is False
    assert "p13_container_executed" in result["blocked_by"]
    assert result["p13"]["blocks"] is True


def test_cancellation_p13_blocks_when_eta_passed():
    result = assess_cancellation(
        "ETA already passed",
        execution_status="not_started",
        any_container_executed=False,
        cargo_facility_date_reached=False,
    )
    assert result["allowed"] is False
    assert "p13_eta_passed" in result["blocked_by"]


def test_cancellation_allowed_when_p6_and_p13_both_clear():
    result = assess_cancellation(
        "",
        execution_status="not_started",
        any_container_executed=False,
        eta_passed=False,
        cargo_facility_date_reached=False,
        dispatched=True,
    )
    assert result["allowed"] is True
    assert result["blocked_by"] == []
    assert result["p6"]["blocks"] is False
    assert result["p13"]["blocks"] is False
    assert any("futile-trip" in n for n in result["notes"])


def test_cancellation_unknown_when_facts_are_missing():
    result = assess_cancellation("Send to TMS In Progress, acknowledgement not yet received.")
    assert result["allowed"] is None
    assert result["p6"]["executed"] is False
    assert result["p13"]["any_container_executed"] is False
    assert result["p6"]["cargo_facility_date_reached"] is None
    assert result["p13"]["eta_passed"] is None
