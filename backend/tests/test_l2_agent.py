from __future__ import annotations

from app.l2_agent import extract_code_change, is_code_issue, l2_solution


def test_stack_trace_is_a_code_issue():
    messages = [
        "NullPointerException at BookingResponseMapper.mapCarrier(BookingResponseMapper.java:118) for booking BK-90218431",
    ]
    assert is_code_issue(messages) is True
    change = extract_code_change(messages)
    assert change["file"].endswith("BookingResponseMapper.java")
    assert change["line"] == 118


def test_operational_logs_are_not_code_issues():
    messages = [
        "TMS acknowledgement still pending for transport order TO-78451201 after 20 minutes",
        "Consumer group telikos-billing lag=1842 partition=3",
    ]
    assert is_code_issue(messages) is False


def test_l2_solution_uses_known_finance_handler_fix():
    messages = [
        "Invoice INV-77192014 remains PROCESSING after callback completion at FinanceCallbackHandler.handleSuccess(FinanceCallbackHandler.java:147)",
    ]
    solution = l2_solution(messages)
    assert solution["status"] == "L2_RECOMMENDED"
    assert solution["code_change"]["symbol"] == "handleSuccess"
    assert any(a["level"] == "L2" for a in solution["agents"])
