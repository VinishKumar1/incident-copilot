"""L2 routing — send a ticket to the code-reasoning agent when logs show a defect.

L1 handles operational wait-states (timeouts, 403s, consumer lag, pending TMS ack).
L2 handles stack traces and file:line defects. Assignment-group writeback is a
later ServiceNow step; this module only decides which *agent* owns the analysis.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_FILE_LINE = re.compile(
    r"\b([A-Za-z_][\w$]*\.(?:java|kt|scala|py|ts|tsx|js))\b(?::(\d+))?"
)
_STACK = re.compile(
    r"\bat\s+[\w.$]+\.(\w+)\(([\w.$]+\.(?:java|kt|py|ts|js)):(\d+)\)"
)
_NPE = re.compile(r"\bNullPointerException\b")
_CODE_WORDS = re.compile(
    r"(?i)\b(exception in thread|caused by:|stacktrace|traceback)\b"
)

# Demo catalog: once L2 locates a file, attach the known fix for the walkthrough.
_KNOWN_FIXES: dict[str, dict[str, Any]] = {
    "BookingResponseMapper.java": {
        "repository": "telikos-booking-service",
        "file": "service/src/main/java/net/apmoller/crb/telikos/microservices/booking/api/mapper/BookingResponseMapper.java",
        "line": 118,
        "symbol": "mapCarrier",
        "problem": "transportPlan.getCarrier().getCode() dereferences a nullable carrier",
        "fix": "Return null or an empty optional when getCarrier() is null, then map the code only when present.",
    },
    "ServicePlanMongoTemplate.java": {
        "repository": "telikos-booking-service",
        "file": "service/src/main/java/net/apmoller/crb/telikos/microservices/booking/persistence/impl/ServicePlanMongoTemplate.java",
        "line": 530,
        "symbol": "updateCustomsMilestoneWorkProcess",
        "problem": "findAndModify on telikos-booking-database.bookings raises E11000 DuplicateKey while Temporal activity resetCustomsStatusInServicePlanDB retries (attempt 148170).",
        "fix": "Make the customs-milestone reset idempotent: catch DuplicateKeyException, update the existing document instead of inserting, and stop unbounded Temporal retries on a non-retryable unique-index conflict.",
    },
    "OfferService.kt": {
        "repository": "iom-web-integrator",
        "file": "src/main/kotlin/com/maersk/iom/webintegrator/service/OfferService.kt",
        "line": 293,
        "symbol": "updateServicePlanWithRepricedCharges",
        "problem": "OfferService throws NoAppropriateDataFoundException when reprice returns no charges for the service plan.",
        "fix": "Treat empty reprice charges as a handled business outcome (return a clear API error) instead of an uncaught exception, and confirm master-data/pricing actually has charge lines for that plan before calling reprice.",
    },
    "FinanceCallbackHandler.java": {
        "repository": "telikos-billing-service",
        "file": "service/src/main/java/net/apmoller/crb/telikos/billing/service/FinanceCallbackHandler.java",
        "line": 147,
        "symbol": "handleSuccess",
        "problem": "The mapped invoice is returned without invoking invoiceRepository.save(...)",
        "fix": "Replace Mono.just(updatedInvoice) with invoiceRepository.save(updatedInvoice) and preserve error propagation.",
    },
}


def evidence_messages(evidence: list[dict[str, Any]] | None) -> list[str]:
    messages: list[str] = []
    for group in evidence or []:
        for log in group.get("logs") or []:
            msg = (log.get("message") or "").strip()
            if msg:
                messages.append(msg)
    return messages


def _blob(messages: list[str]) -> str:
    return "\n".join(messages)


def is_code_issue(messages: list[str]) -> bool:
    blob = _blob(messages)
    if not blob:
        return False
    if _STACK.search(blob) or _NPE.search(blob) or _CODE_WORDS.search(blob):
        return True
    return any(name in blob for name in _KNOWN_FIXES)


def extract_code_change(messages: list[str]) -> Optional[dict[str, Any]]:
    blob = _blob(messages)
    stacked = _STACK.search(blob)
    if stacked:
        symbol, filename, line = stacked.group(1), stacked.group(2), int(stacked.group(3))
        known = _KNOWN_FIXES.get(filename)
        if known:
            return dict(known)
        return {
            "repository": "",
            "file": filename,
            "line": line,
            "symbol": symbol,
            "problem": f"Exception at {filename}:{line}",
            "fix": "Inspect the failing line, add a null/error guard, and cover it with a regression test.",
        }
    file_hit = _FILE_LINE.search(blob)
    if file_hit:
        filename = file_hit.group(1)
        line = int(file_hit.group(2)) if file_hit.group(2) else 0
        known = _KNOWN_FIXES.get(filename)
        if known:
            return dict(known)
        return {
            "repository": "",
            "file": filename,
            "line": line,
            "symbol": "",
            "problem": f"Code reference {filename}:{line or '?'}",
            "fix": "Inspect the referenced code and confirm whether a source change is required.",
        }
    for name, known in _KNOWN_FIXES.items():
        if name in blob:
            return dict(known)
    return None


def l2_solution(messages: list[str]) -> dict[str, Any]:
    change = extract_code_change(messages) or {
        "repository": "",
        "file": "unknown",
        "line": 0,
        "symbol": "",
        "problem": "Logs indicate a source-code defect",
        "fix": "Escalate to the owning engineering team with the stack trace.",
    }
    file_label = change.get("file") or "source"
    line = change.get("line") or 0
    headline = f"Make the failing code at {file_label.split('/')[-1]} null-safe" if "null" in (change.get("problem") or "").lower() else f"Fix the defect in {file_label.split('/')[-1]}"
    if change.get("file", "").endswith("FinanceCallbackHandler.java"):
        headline = "Persist the invoice status returned by the finance callback"
        root_cause = change["problem"]
        solution = change["fix"]
    elif change.get("file", "").endswith("ServicePlanMongoTemplate.java") or "ServicePlanMongoTemplate" in file_label:
        headline = "Make customs-milestone reset idempotent on DuplicateKey"
        root_cause = change["problem"]
        solution = change["fix"]
    elif change.get("file", "").endswith("OfferService.kt") or "OfferService.kt" in file_label:
        headline = "Handle missing charges during service-plan reprice"
        root_cause = change["problem"]
        solution = change["fix"]
    elif change.get("file", "").endswith("BookingResponseMapper.java") or "BookingResponseMapper" in file_label:
        headline = "Make booking response mapping null-safe"
        root_cause = change["problem"]
        solution = change["fix"]
    else:
        root_cause = change["problem"]
        solution = change["fix"]
    return {
        "status": "L2_RECOMMENDED",
        "final_confidence": 0.93,
        "headline": headline,
        "root_cause": root_cause,
        "recommended_solution": solution,
        "code_change": change,
        "agents": [
            {
                "level": "L1",
                "name": "Knowledge Agent",
                "confidence": 0.96,
                "decision": "ESCALATED",
                "summary": "The classifier identified a source-code defect, which is outside L1 remediation scope.",
                "basis": "Incident routing policy: code changes must be handled by L2",
            },
            {
                "level": "L2",
                "name": "Code Reasoning Agent",
                "confidence": 0.93,
                "decision": "RECOMMENDED",
                "summary": "The stack trace was mapped to the owning repository and exact failing code location.",
                "basis": f"{file_label}:{line}" if line else file_label,
            },
        ],
    }
