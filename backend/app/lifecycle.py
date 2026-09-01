"""Diagnose where a booking is stuck in the Telikos inland E2E flow.

This is not a booking engine. An external history service will pass free text
into `diagnose_booking_lifecycle`; until that exists, Incident Search passes
incident + log text. Send-to-Customer is out of scope (future PDF).
Cancellation applies both P6 (execution / facility date) and P13 (container
executed / ETA passed); overall cancel is allowed only when both pass.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Documented PDF final flow. Create is implied before Search.
STAGES: list[tuple[str, str]] = [
    ("search", "Search — routes & offers"),
    ("select_offer", "Select route & offer"),
    ("booking_details", "Booking details (Draft)"),
    ("pricing", "Pricing"),
    ("ready_for_planning", "Ready for planning"),
    ("send_to_tms", "Send to TMS"),
    ("execution", "Execution (TMS)"),
    ("fsd", "FSD completed"),
    ("finance", "Finance processing"),
    ("invoice", "Invoice generated"),
]

_STAGE_INDEX = {sid: i for i, (sid, _) in enumerate(STAGES)}
_PROCESS = {
    "search": "SEARCH",
    "select_offer": "OFFER_SELECTION",
    "booking_details": "BOOKING_DETAILS",
    "pricing": "PRICING",
    "ready_for_planning": "READY_FOR_PLANNING",
    "send_to_tms": "SEND_TO_TMS",
    "execution": "EXECUTION",
    "fsd": "FSD",
    "finance": "FINANCE",
    "invoice": "INVOICE",
}

# Fallback history for demo booking ids when no external text is supplied.
_SAMPLE_HISTORY: dict[str, str] = {
    "GHDGW54NC00": (
        "Booking GHDGW54NC00 is BOOKED. Send to TMS is In Progress. "
        "Acknowledgement is not yet received from TMS. "
        "Transport order TO-78451201 acknowledgement PENDING. "
        "Transport order TO-78451202 acknowledgement ACCEPTED received_at 2026-08-28T09:43:21Z."
    ),
    "BK-90218431": (
        "Booking BK-90218431 confirmation failed while mapping the booking response. "
        "NullPointerException in BookingResponseMapper. Send to TMS never started."
    ),
    "SHP-84219373": (
        "Booking SHP-84219373 cannot load port master data from Location Master (SMDS). "
        "Downstream timed out HTTP 504. Workflow stopped before confirmation."
    ),
    "HNZSJN7MMNK": (
        "Error updating customs mileStone workProcess for bookingId HNZSJN7MMNK: "
        "workProcess SEND_TO_CUSTOMS STARTED. Mongo E11000 DuplicateKey on "
        "telikos-booking-database.bookings. Temporal resetCustomsStatusInServicePlanDB retrying."
    ),
    "HNMQBWJSBRW": (
        "Error updating customs mileStone workProcess for bookingId HNMQBWJSBRW: "
        "workProcess SEND_TO_CUSTOMS STARTED. Mongo E11000 DuplicateKey on "
        "telikos-booking-database.bookings. Temporal resetCustomsStatusInServicePlanDB retrying."
    ),
}


def first_booking_id(identifiers: dict[str, list[str]] | None) -> Optional[str]:
    if not identifiers:
        return None
    for key in ("booking", "shipment"):
        values = identifiers.get(key) or []
        if values:
            return values[0]
    return None


def get_booking_lifecycle(
    booking_id: str | None,
    text: str | None = None,
    **cancel_signals: Any,
) -> Optional[dict[str, Any]]:
    """Snapshot for a booking id. `text` is the preferred diagnostic input."""
    if not booking_id:
        return None
    blob = (text or "").strip() or _SAMPLE_HISTORY.get(booking_id.strip(), "")
    if not blob:
        return _unknown_snapshot(booking_id)
    return diagnose_booking_lifecycle(blob, booking_id=booking_id, **cancel_signals)


def diagnose_booking_lifecycle(
    text: str,
    booking_id: str | None = None,
    **cancel_signals: Any,
) -> dict[str, Any]:
    """Locate the booking in the PDF flow from history/incident text."""
    blob = text or ""
    found = _match_stuck(blob)
    signals = dict(cancel_signals)
    provided_execution = signals.pop("execution_status", None)
    execution_hint = _norm_execution(provided_execution) or _infer_execution_status(blob, found)
    cancellation = assess_cancellation(blob, execution_status=execution_hint, **signals)
    orders = _transport_orders(blob)
    steps = _timeline(found)
    return {
        "booking_id": booking_id or _booking_id_from_text(blob) or "",
        "booking_status": found["booking_status"],
        "work_process": found["work_process"],
        "work_process_status": found["work_process_status"],
        "stuck_at": found["stuck_at"],
        "summary": found["summary"],
        "headline_tag": found["headline_tag"],
        "api_status": found.get("api_status"),
        "documented_flow": found["documented_flow"],
        "execution_status": execution_hint,
        "transport_orders": orders,
        "steps": steps,
        "cancellation": cancellation,
    }


def assess_cancellation(
    text: str = "",
    *,
    execution_status: str | None = None,
    any_container_executed: bool | None = None,
    eta_passed: bool | None = None,
    cargo_facility_date_reached: bool | None = None,
    trade_type: str | None = None,
    dispatched: bool | None = None,
) -> dict[str, Any]:
    """P6 and P13 must both pass. Unknown facts are not invented as True/False."""
    blob = text or ""
    trade = _norm_trade(trade_type) or _infer_trade(blob)
    execution = _norm_execution(execution_status) or _infer_execution_status(blob, None)
    facility = _first_bool(cargo_facility_date_reached, _infer_facility_reached(blob, trade))
    eta = _first_bool(eta_passed, _infer_eta_passed(blob))
    dispatched_flag = _first_bool(dispatched, _infer_dispatched(blob))
    container_executed = _first_bool(any_container_executed, _infer_container_executed(blob, execution))

    p6_executed = None if execution is None else execution == "executed"
    p6_facility = facility
    p13_container = container_executed
    p13_eta = eta

    blockers: list[str] = []
    if p6_executed is True:
        blockers.append("p6_executed")
    if p6_facility is True:
        blockers.append("p6_facility_date")
    if p13_container is True:
        blockers.append("p13_container_executed")
    if p13_eta is True:
        blockers.append("p13_eta_passed")

    unknown = [flag is None for flag in (p6_executed, p6_facility, p13_container, p13_eta)]
    if blockers:
        allowed: bool | None = False
    elif any(unknown):
        allowed = None
    else:
        allowed = True

    notes: list[str] = []
    if dispatched_flag is True:
        notes.append("If cancelled after dispatch, TMS adds a futile-trip cost that is reflected in Telikos.")
    if trade == "export":
        notes.append("P6 facility date is CARGO LOADING FACILITY start date (export).")
    elif trade == "import":
        notes.append("P6 facility date is CARGO DELIVERY FACILITY (import).")

    return {
        "allowed": allowed,
        "blocked_by": blockers,
        "trade_type": trade,
        "dispatched": dispatched_flag,
        "p6": {
            "executed": p6_executed,
            "cargo_facility_date_reached": p6_facility,
            "blocks": p6_executed is True or p6_facility is True,
        },
        "p13": {
            "any_container_executed": p13_container,
            "eta_passed": p13_eta,
            "blocks": p13_container is True or p13_eta is True,
        },
        "notes": notes,
    }


# ── matching ──────────────────────────────────────────────────────────────────

_Rule = dict[str, Any]


def _rule(
    stage: str,
    stuck_at: str | None,
    summary: str,
    *,
    status: str = "STUCK",
    booking_status: str = "BOOKED",
    headline: str = "",
    documented: bool = True,
    api_status: int | None = None,
    failed: bool = False,
    complete: bool = False,
) -> _Rule:
    process_status = "FAILED" if failed else ("COMPLETE" if complete else status)
    return {
        "stage": stage,
        "stuck_at": stuck_at,
        "work_process": "" if not documented else _PROCESS.get(stage, stage.upper()),
        "work_process_status": process_status if documented else "UNDOCUMENTED",
        "booking_status": booking_status,
        "headline_tag": headline or ("Complete" if complete else ("Failed" if failed else "Stuck")),
        "summary": summary,
        "documented_flow": documented,
        "api_status": api_status,
        "failed": failed,
        "complete": complete,
    }


def _match_stuck(text: str) -> _Rule:
    t = text
    low = text.lower()

    if _search(r"\b204\b|no routes available|no routes", t):
        return _rule(
            "search", "no_routes",
            "Search returned no routes (204). Configure the route in MePC and retry Search — booking cannot proceed without a valid route.",
            booking_status="FAILED", failed=True, headline="No routes", api_status=204,
        )
    if _search(r"\b206\b|no offers available|no o(?:ff|ﬀ)ers available|no pricing", t):
        return _rule(
            "search", "no_offers",
            "Search returned no offers/pricing (206). Set up pricing in Athena and retry Search — booking needs a valid route and valid pricing.",
            booking_status="FAILED", failed=True, headline="No offers", api_status=206,
        )
    if _search(r"dates are not in sequential order|not in ascending order|invalid date sequence", t):
        return _rule(
            "booking_details", "invalid_date_sequence",
            "Dates are not in sequential/ascending order, so the booking cannot be created. Enter start → end in the required sequence.",
            booking_status="FAILED", failed=True, headline="Invalid dates",
        )
    if _search(r"required field missing|mandatory fields? missing", t):
        return _rule(
            "booking_details", "mandatory_fields",
            "A required field is missing (highlighted in Telikos). Fill all mandatory fields before continuing.",
            booking_status="FAILED", failed=True, headline="Mandatory fields",
        )
    if _search(r"idoc failure|idoc fail", t):
        return _rule(
            "finance", "idoc_failure",
            "IDOC failure in Finance — typically master-data alignment or a contract issue between Telikos and Finance. Raise an incident.",
            failed=True, headline="IDOC failure",
        )
    if _search(r"vendor invoice.{0,80}disput|invoice.{0,40}(high|low) tolerance", t):
        return _rule(
            "finance", "vendor_invoice_dispute",
            "Vendor invoice is in a disputed state in TMS (high or low tolerance). Dispatcher must approve or reject; rejected invoices must be resubmitted.",
            headline="Vendor invoice disputed",
        )
    if _search(r"posting started|vendor invoice.{0,60}stuck", t):
        return _rule(
            "finance", "vendor_invoice_posting",
            "Vendor invoice remains stuck in Posting Started in TMS. Raise an incident for posting/batch processing.",
            headline="Invoice posting stuck",
        )
    if _search(r"ready for invoicing.{0,40}pending|vat partner", t):
        return _rule(
            "invoice", "vat_partner",
            "Ready for Invoicing is Pending because mandatory finance data is missing (typically VAT partner code). Add it in FinOps view so billing can resume.",
            headline="VAT partner missing",
        )
    if _search(r"fro.{0,80}vendor (is )?not assigned|vendor not assigned.{0,40}mre|rate is not set up in mre", t):
        return _rule(
            "send_to_tms", "vendor_assignment",
            "Freight Order (FRO) was created but no vendor was assigned — the buying rate is not set up in MRE.",
            headline="Vendor not assigned",
        )
    if _search(r"not auto-?planned|vendor not being extended in lns|vendor not extended in lns", t):
        return _rule(
            "send_to_tms", "auto_planning",
            "Order was not auto-planned in TMS. A common cause is the vendor not being extended in LNS.",
            headline="Not auto-planned",
        )
    if _search(r"transit time.{0,40}insufficient", t):
        return _rule(
            "execution", "transit_time",
            "Transit time in TMS is insufficient. Correct the duration from Port to Customer Facility (and vice versa) in Telikos.",
            headline="Transit time",
        )
    if _tms_ack_stuck(t):
        return _rule(
            "send_to_tms", "tms_acknowledgement",
            "Send to TMS is In Progress because acknowledgement has not been received from TMS. The workflow cannot proceed until the missing transport-order acknowledgement arrives.",
            status="STARTED", headline="Awaiting acknowledgement", api_status=202,
        )
    if _search(r"bookingresponsemapper|response mapping|confirmation failed", t):
        return _rule(
            "booking_details", "response_mapping",
            "Booking confirmation failed while mapping the booking response; Send to TMS never started.",
            booking_status="FAILED", failed=True, headline="Confirmation failed", api_status=500,
        )
    if _search(r"master[- ]data|location master|cannot load port|\bsmds\b", t) and _search(
        r"timed? ?out|timeout|504|cannot load", t,
    ):
        return _rule(
            "search", "master_data",
            "Booking cannot load location/port master data (SMDS). Search/details stop until the master-data dependency recovers.",
            booking_status="PENDING", failed=True, headline="Dependency timeout", api_status=504,
        )
    if _search(r"send[_ ]to[_ ]customs|send to customer", t):
        return _rule(
            "search", None,
            "This history refers to Send to Customer / SEND_TO_CUSTOMS, which is not in the current inland E2E PDF. Treat it as a future enhancement, not a documented lifecycle stage.",
            booking_status="BOOKED", documented=False, headline="Outside documented flow",
        )
    if _search(r"invoice generated|invoice (is )?triggered|invoice available", t) and not _search(
        r"pending|fail|stuck|credit note", t,
    ):
        return _rule(
            "invoice", None,
            "Invoice has been generated. Default trigger is ETA + 5 days unless Decision Hub billing frequency applies.",
            status="COMPLETE", complete=True, headline="Invoice generated",
        )
    return _progress_or_unknown(low, t)


def _tms_ack_stuck(text: str) -> bool:
    if _search(r"acknowledgement.{0,50}(pending|not (yet )?receiv|missing|still pending)|has not received a tms acknowledgement|no acknowledgment received", text):
        return True
    if _search(r"send to tms.{0,40}(in progress|stuck)|send_to_tms.{0,40}(started|in progress)", text):
        return True
    return False


def _progress_or_unknown(low: str, text: str) -> _Rule:
    furthest = -1
    probes = [
        (0, r"\bsearch\b|routes? and o|create (new )?booking"),
        (1, r"select(ed)? (the )?(route|offer)|offer & route"),
        (2, r"booking details|saved as draft|service plan"),
        (3, r"\bpricing\b|athena|mandatory charges"),
        (4, r"ready for planning|order id"),
        (5, r"send to tms|send_to_tms|transport order"),
        (6, r"in[- ]execution|not started|executed|execution \(tms\)"),
        (7, r"\bfsd\b|freight settlement"),
        (8, r"\bfinance\b|\bfact\b|sales order|create_rev"),
        (9, r"\binvoice\b"),
    ]
    for idx, pat in probes:
        if re.search(pat, low):
            furthest = max(furthest, idx)
    if furthest < 0:
        return _rule(
            "search", None,
            "History text did not match a documented inland booking stage.",
            booking_status="UNKNOWN", status="UNKNOWN", headline="Unrecognised",
        )
    stage = STAGES[furthest][0]
    label = STAGES[furthest][1]
    return _rule(
        stage, None,
        f"Booking appears to have reached {label}; no stuck-error pattern was found in the history text.",
        status="IN_PROGRESS", headline=label, booking_status="BOOKED" if furthest >= 4 else "DRAFT",
    )


def _timeline(found: _Rule) -> list[dict[str, Any]]:
    stage = found.get("stage") or "search"
    idx = _STAGE_INDEX.get(stage, 0)
    failed = bool(found.get("failed"))
    complete = bool(found.get("complete"))
    documented = bool(found.get("documented_flow"))
    steps: list[dict[str, Any]] = []
    for i, (_sid, label) in enumerate(STAGES):
        if not documented:
            state, mark, detail = "pending", "·", "Not in current PDF"
        elif complete or i < idx:
            state, mark, detail = "done", "✓", "Reached"
        elif i == idx and failed:
            state, mark, detail = "failed", "✕", found.get("headline_tag") or "Failed"
        elif i == idx:
            state, mark, detail = "active", "!", found.get("headline_tag") or "Current"
        else:
            state, mark, detail = "pending", "·", "Not reached"
        if i == idx and found.get("stuck_at") and documented:
            detail = found.get("headline_tag") or detail
        steps.append({"label": label, "state": state, "mark": mark, "detail": detail})
    return steps


def _unknown_snapshot(booking_id: str) -> dict[str, Any]:
    empty = _rule(
        "search", None,
        f"No lifecycle record found for {booking_id} in the booking store, and no history text was supplied.",
        booking_status="UNKNOWN", status="UNKNOWN", headline="Not in store",
    )
    return {
        "booking_id": booking_id,
        "booking_status": "UNKNOWN",
        "work_process": "",
        "work_process_status": "UNKNOWN",
        "stuck_at": None,
        "summary": empty["summary"],
        "headline_tag": "Not in store",
        "api_status": None,
        "documented_flow": True,
        "execution_status": None,
        "transport_orders": [],
        "steps": _timeline(empty),
        "cancellation": assess_cancellation(""),
    }


# ── inference helpers ─────────────────────────────────────────────────────────

def _search(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) is not None


def _booking_id_from_text(text: str) -> Optional[str]:
    m = re.search(r"\b(?:booking(?:id)?|booking id)\s*[:#]?\s*([A-Z0-9-]{6,})\b", text, re.I)
    if m:
        return m.group(1)
    return None


_TO_STATUS = re.compile(
    r"(?:"
    r"(?P<s1>still\s+pending|\bPENDING\b|\bACCEPTED\b).{0,60}?(?:transport order\s+)(?P<t1>TO-\d+)"
    r"|"
    r"(?P<t2>TO-\d+)\s+acknowledgement\s+(?P<s2>PENDING|ACCEPTED)"
    r")",
    re.I,
)


def _transport_orders(text: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}

    def _ack(raw: str) -> str:
        return "PENDING" if re.search(r"pending", raw, re.I) else "ACCEPTED"

    for m in _TO_STATUS.finditer(text):
        number = (m.group("t1") or m.group("t2")).upper()
        ack = _ack(m.group("s1") or m.group("s2"))
        window = text[m.start(): min(len(text), m.end() + 80)]
        ts = re.search(r"\d{4}-\d{2}-\d{2}T[\d:]+Z", window)
        ver = re.search(r"version\s+(\d+)", window, re.I)
        seen[number] = {
            "number": number,
            "version": int(ver.group(1)) if ver else 1,
            "acknowledgement": ack,
            "received_at": ts.group(0) if ts else None,
        }
    for m in re.finditer(r"\b(TO-\d+)\b", text, re.I):
        number = m.group(1).upper()
        seen.setdefault(number, {
            "number": number,
            "version": 1,
            "acknowledgement": "PENDING",
            "received_at": None,
        })
    return list(seen.values())


def _norm_execution(value: str | None) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower().replace("_", " ").replace("-", " ")
    if v in {"executed"}:
        return "executed"
    if v in {"in execution", "inexecution"}:
        return "in_execution"
    if v in {"not started", "notstarted"}:
        return "not_started"
    return None


def _norm_trade(value: str | None) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in {"export", "import"}:
        return v
    return None


def _infer_trade(text: str) -> Optional[str]:
    if _search(r"\bexport\b", text):
        return "export"
    if _search(r"\bimport\b", text):
        return "import"
    return None


def _infer_execution_status(text: str, found: _Rule | None) -> Optional[str]:
    if _search(r"\bin[- ]execution\b", text):
        return "in_execution"
    if _search(r"\bnot started\b", text):
        return "not_started"
    if _search(r"\bexecuted\b", text) and not _search(r"in[- ]execution", text):
        return "executed"
    if _tms_ack_stuck(text) or (found and found.get("stuck_at") == "tms_acknowledgement"):
        return "not_started"
    if found:
        idx = _STAGE_INDEX.get(found.get("stage") or "", -1)
        if found.get("stage") == "send_to_tms":
            return "not_started"
        if idx >= _STAGE_INDEX["fsd"]:
            return "executed"
        if found.get("stage") == "execution":
            return "in_execution"
        if idx >= 0 and idx < _STAGE_INDEX["execution"]:
            return "not_started"
    return None


def _infer_eta_passed(text: str) -> Optional[bool]:
    if _search(r"eta (already )?passed|eta has (already )?passed", text):
        return True
    if _search(r"before eta|eta not (yet )?(passed|reached)", text):
        return False
    return None


def _infer_facility_reached(text: str, trade: str | None) -> Optional[bool]:
    if _search(r"cargo (loading|delivery) facility.{0,60}(reached|passed|start date)", text):
        return True
    if _search(r"facility date (has (already )?been )?reached", text):
        return True
    if trade == "export" and _search(r"cargo loading facility start date.{0,30}(reached|passed)", text):
        return True
    if trade == "import" and _search(r"cargo delivery facility.{0,30}(reached|passed)", text):
        return True
    return None


def _infer_dispatched(text: str) -> Optional[bool]:
    if _search(r"after dispatch|already dispatched|sent to vendor", text):
        return True
    return None


def _infer_container_executed(text: str, execution: str | None) -> Optional[bool]:
    if _search(r"any (one )?container.{0,40}executed|container.{0,40}already executed", text):
        return True
    if execution == "executed":
        return True
    if execution == "not_started":
        return False
    return None


def _first_bool(*values: bool | None) -> Optional[bool]:
    for value in values:
        if value is not None:
            return value
    return None
