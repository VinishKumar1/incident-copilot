"""Tests for the new incident-copilot routes in routes/snow.py: the L1
knowledge-base lookup, the LLM+web fallback, and the usage-feedback endpoint.
get_incident's own behavior is unchanged and already covered by test_snow.py
at the SnowClient level, so these tests mock out the shared
_lookup_incident_and_logs helper and focus on what's new."""
from __future__ import annotations

from app.models import Recommendation
from app.routes import snow


def _fake_lookup_data() -> dict:
    return {
        "incident": {
            "number": "INC0012345",
            "short_description": "Booking MHXHTFLMG9P9 failed",
            "description": "Payment step timed out",
            "priority": "High",
            "state": "New",
        },
        "identifiers": {"booking": ["MHXHTFLMG9P9"]},
        "loki_results": {
            "MHXHTFLMG9P9": {
                "services": [
                    {
                        "service": "payments",
                        "namespace": "telikos-dev",
                        "problems": [{"message": "ERROR payment gateway timeout"}],
                    }
                ]
            }
        },
        "searched_keys": ["MHXHTFLMG9P9"],
    }


class _UnusedLLMClient:
    """Fails the test if the LLM fallback runs when it shouldn't."""

    async def analyze_incident(self, context_text):
        raise AssertionError("LLM fallback should not run when L1 has a confident match")


def test_analyze_uses_l1_match_when_confident(client, monkeypatch):
    async def fake_lookup(number, minutes):
        assert number == "INC0012345"
        return _fake_lookup_data()

    monkeypatch.setattr(snow, "_lookup_incident_and_logs", fake_lookup)

    recommendation = Recommendation(
        source="l1",
        summary="Payment gateway timeout",
        root_cause="Gateway times out under load",
        suggested_fix="Increase gateway timeout to 30s",
        confidence=0.93,
        confidence_label="high",
        kb_entry_id="kb-1",
        servicenow_work_note="[AI-assisted] Increase gateway timeout to 30s.",
        sources_used=["knowledge_base"],
    )

    async def fake_l1_lookup(pattern_text, service=""):
        assert "payment gateway timeout" in pattern_text.lower()
        return recommendation

    monkeypatch.setattr(snow, "l1_lookup", fake_l1_lookup)
    monkeypatch.setattr(snow, "llm_client", _UnusedLLMClient())

    response = client.post("/api/snow/incident/INC0012345/analyze")

    assert response.status_code == 200
    payload = response.json()["recommendation"]
    assert payload["source"] == "l1"
    assert payload["kb_entry_id"] == "kb-1"
    assert payload["confidence"] == 0.93
    assert payload["incident_number"] == "INC0012345"


def test_analyze_falls_back_to_llm_and_web_when_l1_has_no_match(client, monkeypatch):
    async def fake_lookup(number, minutes):
        return _fake_lookup_data()

    monkeypatch.setattr(snow, "_lookup_incident_and_logs", fake_lookup)

    async def fake_l1_lookup(pattern_text, service=""):
        return None

    monkeypatch.setattr(snow, "l1_lookup", fake_l1_lookup)

    search_calls = []

    class FakeWebSearchClient:
        configured = True

        async def search(self, query, max_results=3):
            search_calls.append(query)
            return [{
                "title": "Gateway timeouts",
                "url": "https://kubernetes.io/docs/x",
                "content": "Increase the timeout and pool size.",
            }]

    monkeypatch.setattr(snow, "web_search_client", FakeWebSearchClient())

    class FakeLLMClient:
        def __init__(self):
            self.received_context = None

        async def analyze_incident(self, context_text):
            self.received_context = context_text
            return {
                "summary": "Payment gateway times out under load",
                "root_cause": "Gateway connection pool exhausted",
                "suggested_fix": "Increase timeout and pool size",
                "confidence": "medium",
                "servicenow_work_note": "[AI-assisted] Increase timeout and pool size.",
            }

    fake_llm_client = FakeLLMClient()
    monkeypatch.setattr(snow, "llm_client", fake_llm_client)

    response = client.post("/api/snow/incident/INC0012345/analyze")

    assert response.status_code == 200
    rec = response.json()["recommendation"]
    assert rec["source"] == "llm"
    assert rec["confidence_label"] == "medium"
    assert set(rec["sources_used"]) == {"logs", "web"}
    assert rec["incident_number"] == "INC0012345"

    # web search actually ran, and its results were folded into the LLM's context
    assert len(search_calls) == 1
    assert "Approved web sources" in fake_llm_client.received_context
    assert "payment gateway timeout" in fake_llm_client.received_context.lower()


def test_mark_used_records_feedback(client, monkeypatch):
    calls = []

    async def fake_record_kb_feedback(incident_number, used, edited=False, notes=""):
        calls.append((incident_number, used, edited, notes))

    monkeypatch.setattr(snow, "record_kb_feedback", fake_record_kb_feedback)

    response = client.post(
        "/api/snow/incident/inc0012345/mark-used",
        json={"used": True, "edited": True, "notes": "adjusted the timeout value"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls == [("INC0012345", True, True, "adjusted the timeout value")]
