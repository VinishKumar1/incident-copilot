"""Realistic ServiceNow-shaped fixtures for local incident-search development."""
from __future__ import annotations

from typing import Any


def _l1_solution(headline: str, root_cause: str, solution: str, confidence: float, basis: str) -> dict[str, Any]:
    return {
        "status": "L1_RECOMMENDED",
        "final_confidence": confidence,
        "headline": headline,
        "root_cause": root_cause,
        "recommended_solution": solution,
        "agents": [{
            "level": "L1", "name": "Knowledge Agent", "confidence": confidence,
            "decision": "RECOMMENDED", "summary": "A configured runbook matched this incident and can be applied without a code change.",
            "basis": basis,
        }],
    }


def _l2_solution(headline: str, root_cause: str, solution: str, confidence: float,
                 code_change: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "L2_RECOMMENDED",
        "final_confidence": confidence,
        "headline": headline,
        "root_cause": root_cause,
        "recommended_solution": solution,
        "agents": [
            {
                "level": "L1", "name": "Knowledge Agent", "confidence": 0.96,
                "decision": "ESCALATED", "summary": "The configured classifier identified a source-code defect, which is outside L1 remediation scope.",
                "basis": "Incident routing policy: code changes must be handled by L2",
            },
            {
                "level": "L2", "name": "Code Reasoning Agent", "confidence": confidence,
                "decision": "RECOMMENDED", "summary": "The stack trace was mapped to the owning repository and exact failing code location.",
                "basis": "Stack trace, correlated logs and repository analysis",
            },
        ],
        "code_change": code_change,
    }


MOCK_INCIDENTS: list[dict[str, Any]] = [
    {
        "incident": {"number": "INC0098421", "sys_id": "mock-98421", "short_description": "Bookings remain pending after submission", "description": "Booking GHDGW54NC00 remains pending after Send to TMS was requested. One of two transport orders has not received a TMS acknowledgement.", "state": "In Progress", "priority": "High", "opened_at": "2026-08-28T07:42:00Z", "resolved_at": "", "caller": "Operations Control", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"booking": ["GHDGW54NC00"]},
        "agent_solution": _l1_solution(
            "Recover the missing TMS acknowledgement before retriggering the booking",
            "The booking request was accepted and SEND_TO_TMS started, but TO-78451201 has no acknowledgement, so the workflow cannot complete.",
            "Check the outbound TMS topic and acknowledgement consumer for TO-78451201. Replay the missing acknowledgement when the outbound message exists; otherwise perform one idempotent Send-to-TMS retrigger.",
            0.94,
            "Booking-to-TMS acknowledgement recovery runbook",
        ),
        "suggested_assignment": {"team": "TMS Integration Support", "reason": "The booking workflow is healthy and is waiting for a transport-order acknowledgement owned by the TMS integration path."},
        "tms_delivery": {
            "summary": "Send-to-TMS was initiated successfully, but acknowledgement is missing for 1 of 2 transport orders.",
            "booking_status": "BOOKED",
            "work_process": "SEND_TO_TMS",
            "work_process_status": "STARTED",
            "api_status": 202,
            "retrigger_to_finance": False,
            "started_at": "2026-08-28T09:43:04Z",
            "transport_orders": [
                {"number": "TO-78451201", "version": 1, "acknowledgement": "PENDING", "received_at": None},
                {"number": "TO-78451202", "version": 1, "acknowledgement": "ACCEPTED", "received_at": "2026-08-28T09:43:21Z"},
            ],
        },
        "evidence": [
            {"service": "telikos-booking-service", "namespace": "telikos", "source": "identifier", "matched_identifier": "GHDGW54NC00", "count": 3, "logs": [
                {"ts": "2026-08-28T09:43:04Z", "level": "info", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "POST /bookings/GHDGW54NC00/send-to-tms accepted with HTTP 202; retriggerToFinance=false"},
                {"ts": "2026-08-28T09:43:05Z", "level": "info", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "Booking GHDGW54NC00 work process SEND_TO_TMS changed to STARTED"},
            ]},
            {"service": "transport-order-feedback", "namespace": "telikos", "source": "trace", "matched_identifier": "GHDGW54NC00", "count": 2, "logs": [
                {"ts": "2026-08-28T09:43:21Z", "level": "info", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "TMS acknowledgement ACCEPTED for transport order TO-78451202"},
                {"ts": "2026-08-28T10:03:48Z", "level": "warn", "trace_id": "f46ac10b-58cc-4372-a567-0e02b2c3d479", "message": "TMS acknowledgement still pending for transport order TO-78451201 after 20 minutes"},
            ]},
        ],
        "actions": [
            {"title": "Check the TMS acknowledgement topic", "detail": "Search by booking GHDGW54NC00, transport order TO-78451201 and the correlated trace ID.", "kind": "investigate"},
            {"title": "Inspect the booking workflow", "detail": "Verify that the SEND_TO_TMS workflow is running and waiting for the expected transport-order acknowledgement.", "kind": "investigate"},
            {"title": "Confirm the outbound publish", "detail": "Check that TO-78451201 was published successfully and capture its correlation ID before retrying.", "kind": "mitigate"},
            {"title": "Retrigger only after validation", "detail": "Retry Send to TMS only when the previous message is confirmed missing or failed, to avoid duplicate transport orders.", "kind": "mitigate"},
            {"title": "Update the incident", "detail": "Record the missing acknowledgement, current owner and the next checkpoint in ServiceNow.", "kind": "communicate"},
        ],
    },
    {
        "incident": {"number": "INC0098417", "sys_id": "mock-98417", "short_description": "Documents unavailable for completed shipment", "description": "Customer cannot download documents for container MRKU1234567.", "state": "New", "priority": "Moderate", "opened_at": "2026-08-28T07:18:00Z", "resolved_at": "", "caller": "Customer Experience", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"container": ["MRKU1234567"]},
        "agent_solution": _l1_solution(
            "Restore the document-storage workload identity's S3 access",
            "S3 is returning HTTP 403 for the document lookup, matching the expired or mis-scoped workload identity runbook.",
            "Validate the current workload identity and S3 bucket policy, then refresh the configured credential reference. Keep permissions limited to the document bucket and retry the lookup.",
            0.92,
            "Document-storage S3 access-denied runbook",
        ),
        "suggested_assignment": {"team": "Cloud Platform Operations", "reason": "The evidence points to workload identity or S3 resource-policy configuration rather than booking logic."},
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
        "agent_solution": _l1_solution(
            "Stabilize the master-data dependency before retrying the booking",
            "The web integrator is timing out while reading the ports endpoint; the failure matches the downstream-latency runbook.",
            "Check master-data health and latency, confirm the configured client timeout and circuit-breaker state, then retry after dependency recovery.",
            0.89,
            "Downstream HTTP timeout runbook",
        ),
        "suggested_assignment": {"team": "Master Data Platform", "reason": "The failing dependency is the master-data ports endpoint and should be triaged by its owning team."},
        "evidence": [
            {"service": "web-integrator", "namespace": "telikos", "source": "trace", "matched_identifier": "SHP-84219373", "count": 4, "logs": [
                {"ts": "2026-08-28T08:26:31Z", "level": "error", "trace_id": "c36d2fbb978142cd", "message": "ERROR feign.RetryableException: Read timed out executing GET http://master-data/api/v1/ports"},
            ]},
        ],
    },
    {
        "incident": {"number": "INC0098360", "sys_id": "mock-98360", "short_description": "Booking confirmation fails during response mapping", "description": "Booking BK-90218431 fails with a NullPointerException in BookingResponseMapper.", "state": "New", "priority": "High", "opened_at": "2026-08-28T05:33:00Z", "resolved_at": "", "caller": "Booking Support", "assignment_group": "Booking Platform", "category": "software", "close_notes": ""},
        "identifiers": {"booking": ["BK-90218431"]},
        "agent_solution": _l2_solution(
            "Make booking response mapping null-safe",
            "BookingResponseMapper dereferences transportPlan.getCarrier() when an upstream response omits the optional carrier object.",
            "Guard the optional carrier before mapping its code and add a mapper test for a transport plan without carrier information.",
            0.93,
            {"repository": "telikos-booking-service", "file": "service/src/main/java/net/apmoller/crb/telikos/microservices/booking/api/mapper/BookingResponseMapper.java", "line": 118, "symbol": "mapCarrier", "problem": "transportPlan.getCarrier().getCode() dereferences a nullable carrier", "fix": "Return null or an empty optional when getCarrier() is null, then map the code only when present."},
        ),
        "suggested_assignment": {"team": "Booking Engineering", "reason": "L2 located a null-safety defect in BookingResponseMapper.java that requires a code change and regression test."},
        "evidence": [{"service": "telikos-booking-service", "namespace": "telikos", "source": "trace", "matched_identifier": "BK-90218431", "count": 6, "logs": [
            {"ts": "2026-08-28T08:12:40Z", "level": "error", "trace_id": "be81b40919f74c81", "message": "NullPointerException at BookingResponseMapper.mapCarrier(BookingResponseMapper.java:118) for booking BK-90218431"},
        ]}],
    },
    {
        "incident": {"number": "INC0098352", "sys_id": "mock-98352", "short_description": "Invoice remains in PROCESSING after finance callback", "description": "Invoice INV-77192014 did not transition after a successful finance callback.", "state": "In Progress", "priority": "High", "opened_at": "2026-08-28T05:06:00Z", "resolved_at": "", "caller": "Finance Operations", "assignment_group": "Billing Platform", "category": "software", "close_notes": ""},
        "identifiers": {"invoice": ["INV-77192014"]},
        "agent_solution": _l2_solution(
            "Persist the invoice status returned by the finance callback",
            "FinanceCallbackHandler maps the callback but does not save the updated invoice on the SUCCESS branch.",
            "Return invoiceRepository.save(updatedInvoice) from the SUCCESS branch and cover the state transition with a unit test.",
            0.95,
            {"repository": "telikos-billing-service", "file": "service/src/main/java/net/apmoller/crb/telikos/billing/service/FinanceCallbackHandler.java", "line": 147, "symbol": "handleSuccess", "problem": "The mapped invoice is returned without invoking invoiceRepository.save(...)", "fix": "Replace Mono.just(updatedInvoice) with invoiceRepository.save(updatedInvoice) and preserve error propagation."},
        ),
        "suggested_assignment": {"team": "Billing Engineering", "reason": "L2 identified a missing persistence call in FinanceCallbackHandler.java that requires a source-code fix."},
        "evidence": [{"service": "telikos-billing-service", "namespace": "telikos", "source": "identifier", "matched_identifier": "INV-77192014", "count": 7, "logs": [
            {"ts": "2026-08-28T09:15:10Z", "level": "info", "trace_id": "b14b69a2a8454fa2", "message": "Finance callback SUCCESS received for INV-77192014"},
            {"ts": "2026-08-28T09:15:11Z", "level": "warn", "trace_id": "b14b69a2a8454fa2", "message": "Invoice INV-77192014 remains PROCESSING after callback completion at FinanceCallbackHandler.handleSuccess(FinanceCallbackHandler.java:147)"},
        ]}],
    },
    {
        "incident": {"number": "INC0098344", "sys_id": "mock-98344", "short_description": "Billing events delayed by consumer lag", "description": "Billing events for invoice INV-77191888 are delayed while Kafka consumer lag is elevated.", "state": "New", "priority": "Moderate", "opened_at": "2026-08-28T04:48:00Z", "resolved_at": "", "caller": "Finance Operations", "assignment_group": "Billing Platform", "category": "software", "close_notes": ""},
        "identifiers": {"invoice": ["INV-77191888"]},
        "agent_solution": _l1_solution(
            "Restore billing consumer throughput and monitor lag recovery",
            "The billing consumer group is healthy but processing more slowly than the incoming event rate, matching the consumer-lag runbook.",
            "Check for a stuck partition and downstream throttling, then scale the consumer within the configured partition limit and monitor lag until it drains.",
            0.90,
            "Billing Kafka consumer-lag runbook",
        ),
        "suggested_assignment": {"team": "Billing Platform Operations", "reason": "The incident matches the operational consumer-lag runbook and does not currently require a code change."},
        "evidence": [{"service": "billing-event-consumer", "namespace": "telikos", "source": "identifier", "matched_identifier": "INV-77191888", "count": 12, "logs": [
            {"ts": "2026-08-28T09:20:44Z", "level": "warn", "trace_id": "fa7eab55a8124ccd", "message": "Consumer group telikos-billing lag=1842 partition=3 for INV-77191888"},
        ]}],
    },
    {
        "incident": {
            "number": "INC0098501",
            "sys_id": "mock-98501",
            "short_description": "Customs milestone reset failing for booking HNZSJN7MMNK",
            "description": "Temporal activity resetCustomsStatusInServicePlanDB keeps failing while updating SEND_TO_CUSTOMS for booking HNZSJN7MMNK. Grafana shows Mongo E11000 DuplicateKey on telikos-booking-database.bookings (attempt 148170).",
            "state": "In Progress",
            "priority": "High",
            "opened_at": "2026-09-01T09:06:13Z",
            "resolved_at": "",
            "caller": "Booking Support",
            "assignment_group": "Booking Platform",
            "category": "software",
            "close_notes": "",
        },
        "identifiers": {"booking": ["HNZSJN7MMNK"]},
        "suggested_assignment": {
            "team": "Booking Engineering",
            "reason": "L2 located a DuplicateKeyException in ServicePlanMongoTemplate.updateCustomsMilestoneWorkProcess; the Temporal activity is not idempotent and is retrying without bound.",
        },
        "evidence": [{
            "service": "telikos-booking-service",
            "namespace": "telikos-spt-cdt",
            "source": "identifier",
            "matched_identifier": "HNZSJN7MMNK",
            "count": 2,
            "logs": [
                {
                    "ts": "2026-09-01T09:06:13.134Z",
                    "level": "info",
                    "trace_id": "8a4c2e27-2fd1-382f-b4dd-b1ea76b49bc1",
                    "message": "Error occurred while updating customs mileStone workProcess data for bookingId : HNZSJN7MMNK : workProcess : SEND_TO_CUSTOMS STARTED, errorMessage : Command failed with error 11000 (DuplicateKey): E11000 duplicate key error collection: telikos-booking-database.bookings",
                },
                {
                    "ts": "2026-09-01T09:06:13.134Z",
                    "level": "warn",
                    "trace_id": "8a4c2e27-2fd1-382f-b4dd-b1ea76b49bc1",
                    "message": "Activity failure. ActivityId=8a4c2e27-2fd1-382f-b4dd-b1ea76b49bc1, activityType=resetCustomsStatusInServicePlanDB, attempt=148170 DuplicateKeyException at ServicePlanMongoTemplate.updateCustomsMilestoneWorkProcess(ServicePlanMongoTemplate.java:530)",
                },
            ],
        }],
    },
    {
        "incident": {
            "number": "INC0098502",
            "sys_id": "mock-98502",
            "short_description": "Customs milestone reset failing for booking HNMQBWJSBRW",
            "description": "Temporal activity resetCustomsStatusInServicePlanDB keeps failing while updating SEND_TO_CUSTOMS for booking HNMQBWJSBRW. Grafana shows Mongo E11000 DuplicateKey on telikos-booking-database.bookings (attempt 129033).",
            "state": "In Progress",
            "priority": "High",
            "opened_at": "2026-09-01T09:06:12Z",
            "resolved_at": "",
            "caller": "Booking Support",
            "assignment_group": "Booking Platform",
            "category": "software",
            "close_notes": "",
        },
        "identifiers": {"booking": ["HNMQBWJSBRW"]},
        "suggested_assignment": {
            "team": "Booking Engineering",
            "reason": "Same DuplicateKeyException in ServicePlanMongoTemplate.updateCustomsMilestoneWorkProcess as INC0098501; Temporal activity is not idempotent and is retrying without bound.",
        },
        "evidence": [{
            "service": "telikos-booking-service",
            "namespace": "telikos-spt-cdt",
            "source": "identifier",
            "matched_identifier": "HNMQBWJSBRW",
            "count": 2,
            "logs": [
                {
                    "ts": "2026-09-01T09:06:12.836Z",
                    "level": "info",
                    "trace_id": "657dea8e-021f-33d2-ad7b-e42a21a632bb",
                    "message": "Error occurred while updating customs mileStone workProcess data for bookingId : HNMQBWJSBRW : workProcess : SEND_TO_CUSTOMS STARTED, errorMessage : Command failed with error 11000 (DuplicateKey): E11000 duplicate key error collection: telikos-booking-database.bookings",
                },
                {
                    "ts": "2026-09-01T09:06:12.837Z",
                    "level": "warn",
                    "trace_id": "657dea8e-021f-33d2-ad7b-e42a21a632bb",
                    "message": "Activity failure. ActivityId=657dea8e-021f-33d2-ad7b-e42a21a632bb, activityType=resetCustomsStatusInServicePlanDB, attempt=129033 DuplicateKeyException at ServicePlanMongoTemplate.updateCustomsMilestoneWorkProcess(ServicePlanMongoTemplate.java:530)",
                },
            ],
        }],
    },
    {
        "incident": {
            "number": "INC0098503",
            "sys_id": "mock-98503",
            "short_description": "Reprice fails: no charges found for service plan",
            "description": "IOM web-integrator OfferController.repriceServicePlan failed with NoAppropriateDataFoundException: No charges found for reprice. Grafana ERROR from WebIntegratorExceptionHandler, trace ae1821a9dd1c65fe1f6f2f858a1d42da.",
            "state": "New",
            "priority": "High",
            "opened_at": "2026-08-25T12:17:41Z",
            "resolved_at": "",
            "caller": "Booking Support",
            "assignment_group": "Booking Platform",
            "category": "software",
            "close_notes": "",
        },
        "identifiers": {},
        "suggested_assignment": {
            "team": "Offer / Pricing",
            "reason": "L2 located NoAppropriateDataFoundException in OfferService.updateServicePlanWithRepricedCharges (OfferService.kt:293). No booking id in the log; reprice returned no charge lines.",
        },
        "evidence": [{
            "service": "iom-web-integrator",
            "namespace": "iom",
            "source": "identifier",
            "matched_identifier": "ae1821a9dd1c65fe1f6f2f858a1d42da",
            "count": 2,
            "logs": [
                {
                    "ts": "2026-08-25T12:17:41.602Z",
                    "level": "error",
                    "trace_id": "ae1821a9dd1c65fe1f6f2f858a1d42da",
                    "message": "No appropriate data found : NoAppropriateDataFoundException: No charges found for reprice",
                },
                {
                    "ts": "2026-08-25T12:17:41.602Z",
                    "level": "error",
                    "trace_id": "ae1821a9dd1c65fe1f6f2f858a1d42da",
                    "message": "at com.maersk.iom.webintegrator.service.OfferService.updateServicePlanWithRepricedCharges.invoke(OfferService.kt:293) Handler OfferController#repriceServicePlan",
                },
            ],
        }],
    },
]


def mock_group(group: str, minutes: int, limit: int, actions_for) -> dict[str, Any]:
    items = []
    normalized_group = group.strip().lower()
    matching = [
        fixture for fixture in MOCK_INCIDENTS
        if normalized_group in fixture["incident"]["assignment_group"].lower()
        or fixture["incident"]["assignment_group"].lower() in normalized_group
    ]
    for fixture in matching[:limit]:
        evidence = fixture["evidence"]
        items.append({**fixture, "relevance": "high" if evidence else "unconfirmed", "actions": fixture.get("actions") or actions_for(fixture["incident"], evidence)})
    return {"group": group, "minutes": minutes, "incident_count": len(items), "relevant_count": sum(i["relevance"] == "high" for i in items), "incidents": items}


def mock_incident(number: str) -> dict[str, Any]:
    fixture = next((item for item in MOCK_INCIDENTS if item["incident"]["number"] == number), MOCK_INCIDENTS[0])
    loki_results: dict[str, Any] = {}
    for values in fixture["identifiers"].values():
        for identifier in values:
            evidence = [item for item in fixture["evidence"] if item["matched_identifier"] == identifier]
            loki_results[identifier] = {"services": [{"service": item["service"], "namespace": item["namespace"], "problem_count": item["count"], "problems": item["logs"]} for item in evidence if item["source"] == "identifier"], "trace_issues": [{"service": item["service"], "namespace": item["namespace"], "problem_count": item["count"], "problems": item["logs"]} for item in evidence if item["source"] == "trace"], "namespaces": sorted({item["namespace"] for item in evidence})}
    return {
        "incident": fixture["incident"],
        "identifiers": fixture["identifiers"],
        "loki_results": loki_results,
        "searched_keys": list(loki_results),
        "agent_solution": fixture.get("agent_solution"),
        "suggested_assignment": fixture.get("suggested_assignment"),
    }
