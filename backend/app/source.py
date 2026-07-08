from __future__ import annotations

import re
from typing import List

from .config import settings
from .models import LogEntry
from .state import runtime

# Services are split across paired namespaces by environment, e.g. iom-preprod ↔ telikos-preprod.
_NS_PAIR_RE = re.compile(r"^(iom|telikos)-(.+)$")
_NS_PREFIXES = ("iom", "telikos")


def paired_namespaces(ns: str) -> List[str]:
    """For an iom-<env>/telikos-<env> namespace, return both paired namespaces (active first)."""
    m = _NS_PAIR_RE.match(ns or "")
    if not m:
        return [ns]
    env = m.group(2)
    ordered = [ns] + [f"{p}-{env}" for p in _NS_PREFIXES]
    seen, out = set(), []
    for n in ordered:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


async def fetch_recent_errors() -> List[LogEntry]:
    """Pull recent error lines from the configured source.

    Mock mode short-circuits regardless of source so the app runs with no infra.
    """
    if settings.use_mock:
        from .loki import loki_client

        return await loki_client.fetch_recent_errors()

    if settings.log_source == "k8s":
        from .k8s import k8s_client

        return await k8s_client.fetch_recent_errors()

    # "loki" or "grafana" — both go through the Loki HTTP client.
    from .loki import loki_client

    return await loki_client.fetch_recent_errors()


async def get_service_logs(service: str, minutes: int = 5, level: str = "", max_lines: int = 60) -> dict:
    """On-demand log fetch for a named service (used by the chatbot tool)."""
    if settings.use_mock:
        return {
            "service": service,
            "namespace": runtime.namespace,
            "minutes": minutes,
            "matched_pods": 1,
            "lines": [
                {"ts": "mock", "pod": f"{service}-abc", "level": "info", "message": f"[mock] sample log for {service}"},
                {"ts": "mock", "pod": f"{service}-abc", "level": "error", "message": f"[mock] sample error for {service}"},
            ],
        }

    if settings.log_source == "k8s":
        from .k8s import k8s_client

        return await k8s_client.fetch_service_logs(service, minutes, level, max_lines)

    # loki / grafana
    from .loki import loki_client

    return await loki_client.get_service_logs(service, runtime.namespace, minutes, level, max_lines)


async def search_key(key: str, minutes: int = 120) -> dict:
    """Find log lines containing `key` across ALL known namespaces.

    We search all namespaces (not just the active live-feed namespace) because
    a booking/container/trace ID can appear in any namespace. The active
    namespace is listed first so its results appear at the top.
    """
    active_namespaces = paired_namespaces(runtime.namespace)
    if settings.use_mock:
        ns0 = active_namespaces[0]
        ns1 = active_namespaces[1] if len(active_namespaces) > 1 else ns0
        return {
            "key": key, "namespace": ", ".join(active_namespaces), "namespaces": active_namespaces, "minutes": minutes,
            "total_matches": 3, "problem_count": 2,
            "services": [
                {"namespace": ns0, "service": "iom-web-integrator", "total": 2, "problem_count": 2, "trace_ids": ["abc123def456"],
                 "problems": [
                     {"ts": "mock", "namespace": ns0, "service": "iom-web-integrator", "pod": "iom-web-integrator-x", "level": "error", "message": f"[mock] Error fetching costs for {key}", "trace_id": "abc123def456"},
                 ]},
                {"namespace": ns1, "service": "telikos-billing-service", "total": 1, "problem_count": 1, "trace_ids": ["abc123def456"],
                 "problems": [
                     {"ts": "mock", "namespace": ns1, "service": "telikos-billing-service", "pod": "telikos-billing-service-y", "level": "error", "message": f"[mock] No financial-job-lines for {key}", "trace_id": "abc123def456"},
                 ]},
            ],
            "trace_ids": ["abc123def456"],
        }
    if settings.log_source == "k8s":
        from .k8s import k8s_client

        return await k8s_client.search_logs(key, active_namespaces, minutes)

    # loki / grafana — search ALL known namespaces so a booking/trace that
    # lives in iom-prod is found even when the live feed shows iom-preprod.
    from .loki import loki_client

    try:
        all_namespaces = await loki_client.list_namespaces()
    except Exception:
        all_namespaces = []

    # Merge: active namespace(s) first (preserves priority), then the rest.
    seen: set = set()
    namespaces: List[str] = []
    for ns in active_namespaces + all_namespaces:
        if ns and ns not in seen:
            seen.add(ns)
            namespaces.append(ns)

    if not namespaces:
        namespaces = active_namespaces

    return await loki_client.search_key(key, namespaces, minutes)
