"""Realistic ServiceNow-shaped fixtures for local incident-search development."""
from __future__ import annotations

from typing import Any


MOCK_INCIDENTS: list[dict[str, Any]] = [
    {
        "incident": {"number": "INC0098421", "sys_id": "mock-98421", "short_description": "Bookings remain pending after submission", "description": "Bookings MHXHTFLMG9P9 and MAEU123456789 are not progressing beyond intake.", "state": "In Progress", "priority": "High", "opened_at": "2026-08-28T07:42:00Z", "resolved_at": "", "caller": "Operations Control", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"booking": ["MHXHTFLMG9P9", "MAEU123456789"]},
        "evidence": [
            {"service": "booking-intake", "namespace": "telikos", "source": "identifier", "matched_identifier": "MHXHTFLMG9P9", "count": 10, "logs": [
                {"ts": "2026-08-28T08:31:18Z", "level": "error", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "ERROR Connection pool exhausted: could not get connection from HikariPool-1 after 30000ms"},
                {"ts": "2026-08-28T08:30:52Z", "level": "error", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "ERROR Booking MHXHTFLMG9P9 could not be persisted; transaction rolled back"},
            ]},
            {"service": "offer-service", "namespace": "telikos", "source": "trace", "matched_identifier": "MHXHTFLMG9P9", "count": 4, "logs": [
                {"ts": "2026-08-28T08:30:49Z", "level": "error", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "ERROR org.postgresql.util.PSQLException: deadlock detected"},
            ]},
        ],
    },
    {
        "incident": {"number": "INC0098417", "sys_id": "mock-98417", "short_description": "Documents unavailable for completed shipment", "description": "Customer cannot download documents for container MRKU1234567.", "state": "New", "priority": "Moderate", "opened_at": "2026-08-28T07:18:00Z", "resolved_at": "", "caller": "Customer Experience", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"container": ["MRKU1234567"]},
        "evidence": [
            {"service": "document-storage", "namespace": "telikos", "source": "identifier", "matched_identifier": "MRKU1234567", "count": 5, "logs": [
                {"ts": "2026-08-28T08:29:04Z", "level": "error", "trace_id": "7f92d1434b6a4a41", "message": "EXCEPTION software.amazon.awssdk.services.s3.S3Exception: Access Denied (Status Code: 403)"},
                {"ts": "2026-08-28T08:28:57Z", "level": "warn", "trace_id": "7f92d1434b6a4a41", "message": "WARN Document lookup failed for container MRKU1234567"},
            ]},
        ],
    },
    {
        "incident": {"number": "INC0098399", "sys_id": "mock-98399", "short_description": "Port information times out during booking", "description": "Booking SHP-84219373 cannot load port master data.", "state": "In Progress", "priority": "High", "opened_at": "2026-08-28T06:51:00Z", "resolved_at": "", "caller": "Booking Support", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"shipment": ["SHP-84219373"]},
        "evidence": [
            {"service": "web-integrator", "namespace": "telikos", "source": "trace", "matched_identifier": "SHP-84219373", "count": 4, "logs": [
                {"ts": "2026-08-28T08:26:31Z", "level": "error", "trace_id": "c36d2fbb978142cd", "message": "ERROR feign.RetryableException: Read timed out executing GET http://master-data/api/v1/ports"},
            ]},
        ],
    },
    {
        "incident": {"number": "INC0098360", "sys_id": "mock-98360", "short_description": "Invoice status not updated", "description": "Invoice INV-90218431 remains in processing but no matching application error is visible.", "state": "New", "priority": "Low", "opened_at": "2026-08-28T05:33:00Z", "resolved_at": "", "caller": "Finance Operations", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"invoice": ["INV-90218431"]},
        "evidence": [],
    },
]


def mock_group(group: str, minutes: int, limit: int, actions_for) -> dict[str, Any]:
    items = []
    for fixture in MOCK_INCIDENTS[:limit]:
        evidence = fixture["evidence"]
        items.append({**fixture, "relevance": "high" if evidence else "unconfirmed", "actions": actions_for(fixture["incident"], evidence)})
    return {"group": group, "minutes": minutes, "incident_count": len(items), "relevant_count": sum(i["relevance"] == "high" for i in items), "incidents": items}


def mock_incident(number: str) -> dict[str, Any]:
    fixture = next((item for item in MOCK_INCIDENTS if item["incident"]["number"] == number), MOCK_INCIDENTS[0])
    loki_results: dict[str, Any] = {}
    for values in fixture["identifiers"].values():
        for identifier in values:
            evidence = [item for item in fixture["evidence"] if item["matched_identifier"] == identifier]
            loki_results[identifier] = {"services": [{"service": item["service"], "namespace": item["namespace"], "problem_count": item["count"], "problems": item["logs"]} for item in evidence if item["source"] == "identifier"], "trace_issues": [{"service": item["service"], "namespace": item["namespace"], "problem_count": item["count"], "problems": item["logs"]} for item in evidence if item["source"] == "trace"], "namespaces": sorted({item["namespace"] for item in evidence})}
    return {"incident": fixture["incident"], "identifiers": fixture["identifiers"], "loki_results": loki_results, "searched_keys": list(loki_results)}
