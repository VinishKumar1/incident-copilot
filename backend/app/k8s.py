from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import List

from .config import settings
from .logparse import ERROR_LEVELS, extract_trace, parse_line
from .models import LogEntry
from .state import runtime

log = logging.getLogger("k8s")

_ERROR_RE = re.compile(settings.k8s_error_pattern)

# Kafka / event-streaming failures are often logged at WARN or with words like
# "failed to publish" / "offset commit failed" that the base error pattern misses.
# Flag a line as an event problem when it mentions a Kafka/event term AND a failure term.
_KAFKA_RE = re.compile(
    r"(?i)\b(kafka|topic|partition|consumer\s?group|consumer|producer|offset|rebalanc\w*|"
    r"deserial\w*|serializ\w*|listener|dead[-\s]?letter|dlq|dlt|broker|publish\w*|produce[rd]?|"
    r"consume[rd]?|subscrib\w*|@?kafkalistener)\b"
)
_FAIL_RE = re.compile(
    r"(?i)\b(fail\w*|error|exception|timed?\s?out|timeout|unable|cannot|could\s?n.?t|reject\w*|"
    r"retry|retries|exhaust\w*|disconnect\w*|unavailable|refused|broken|lag)\b"
)


def _is_event_problem(message: str) -> bool:
    return bool(_KAFKA_RE.search(message) and _FAIL_RE.search(message))


def is_problem(message: str, level) -> bool:
    """Whether a log line represents a problem worth surfacing — error/fatal level,
    or a warn/levelless line matching the error pattern or a Kafka/event failure."""
    lvl = (level or "").lower()
    if lvl in ERROR_LEVELS:
        return True
    if lvl in ("warn", "warning") or not lvl:
        return bool(_ERROR_RE.search(message) or _is_event_problem(message))
    return False


def _parse_ts(token: str) -> float:
    """Parse the RFC3339 timestamp K8s prefixes to each log line (timestamps=True)."""
    try:
        s = token.strip().replace("Z", "+00:00")
        # Python 3.9's fromisoformat rejects >6 fractional digits — truncate nanos.
        if "." in s:
            head, frac = s.split(".", 1)
            tz = ""
            for marker in ("+", "-"):
                if marker in frac:
                    frac, tz = frac.split(marker, 1)
                    tz = marker + tz
                    break
            frac = frac[:6]
            s = f"{head}.{frac}{tz}"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


class K8sClient:
    """Reads recent error lines straight from the Kubernetes API using a kubeconfig.

    No Loki required. Uses get/list on pods and pods/log in one namespace.
    The kubernetes client is synchronous, so calls run in a worker thread.
    """

    def __init__(self) -> None:
        self._v1 = None

    def reset(self) -> None:
        """Drop the cached client so the next call reconnects (recovers from a
        broken keep-alive connection that can otherwise return empty results)."""
        self._v1 = None

    def _api(self):
        if self._v1 is not None:
            return self._v1
        from kubernetes import client, config as kconfig

        if settings.kubeconfig:
            kconfig.load_kube_config(
                config_file=settings.kubeconfig, context=settings.k8s_context or None
            )
        else:
            try:
                kconfig.load_incluster_config()
            except Exception:
                kconfig.load_kube_config(context=settings.k8s_context or None)
        self._v1 = client.CoreV1Api()
        return self._v1

    async def list_namespaces(self) -> List[str]:
        return await asyncio.to_thread(self._list_namespaces)

    def _list_namespaces(self) -> List[str]:
        v1 = self._api()
        return sorted(ns.metadata.name for ns in v1.list_namespace().items)

    async def fetch_recent_errors(self) -> List[LogEntry]:
        return await asyncio.to_thread(self._collect)

    async def search_logs(self, key: str, namespaces: List[str], minutes: int = 120, max_lines: int = 2000, per_service: int = 40) -> dict:
        return await asyncio.to_thread(self._search, key, namespaces, minutes, max_lines, per_service)

    def _search(self, key: str, namespaces: List[str], minutes: int, max_lines: int, per_service: int) -> dict:
        """Scan all pods across the given namespaces for lines containing `key`, grouped by
        (namespace, service), highlighting error/warn lines as 'problems' and collecting trace ids."""
        from kubernetes.client.exceptions import ApiException

        v1 = self._api()
        key_l = key.lower()
        since = max(1, int(minutes)) * 60
        groups: dict = {}
        total = problems_total = 0
        all_traces: set = set()
        searched: List[str] = []

        for ns in namespaces:
            try:
                pods = v1.list_namespaced_pod(ns).items
            except ApiException:
                continue  # namespace missing or not accessible — skip
            searched.append(ns)
            for pod in pods:
                name = pod.metadata.name
                app = (pod.metadata.labels or {}).get("app", name)
                for c in pod.spec.containers or []:
                    try:
                        raw = v1.read_namespaced_pod_log(
                            name=name, namespace=ns, container=c.name,
                            since_seconds=since, tail_lines=max_lines, timestamps=True,
                        )
                    except ApiException:
                        continue
                    for line in raw.splitlines():
                        if not line.strip() or key_l not in line.lower():
                            continue
                        ts, _, rest = line.partition(" ")
                        msg, lvl = parse_line(rest)
                        trace = extract_trace(rest)
                        total += 1
                        g = groups.setdefault((ns, app), {"namespace": ns, "service": app, "total": 0, "problems": [], "problem_count": 0, "traces": set()})
                        g["total"] += 1
                        if trace:
                            g["traces"].add(trace)
                            all_traces.add(trace)
                        if is_problem(msg, lvl):
                            g["problem_count"] += 1
                            problems_total += 1
                            if len(g["problems"]) < per_service:
                                g["problems"].append({"ts": ts, "namespace": ns, "service": app, "pod": name, "level": lvl or "error", "message": msg[:400], "trace_id": trace})

        services = []
        for (_ns, _app), g in sorted(groups.items(), key=lambda kv: -kv[1]["problem_count"]):
            services.append({
                "namespace": g["namespace"], "service": g["service"], "total": g["total"],
                "problem_count": g["problem_count"], "problems": g["problems"], "trace_ids": sorted(g["traces"])[:10],
            })
        return {
            "key": key, "namespace": ", ".join(searched), "namespaces": searched, "minutes": minutes,
            "total_matches": total, "problem_count": problems_total,
            "services": services, "trace_ids": sorted(all_traces)[:20],
        }

    async def fetch_service_logs(
        self, service: str, minutes: int = 5, level: str = "", max_lines: int = 60
    ) -> dict:
        return await asyncio.to_thread(self._collect_service, service, minutes, level, max_lines)

    def _collect_service(self, service: str, minutes: int, level: str, max_lines: int) -> dict:
        """Fetch recent log lines (all levels) for pods matching a service name."""
        from kubernetes.client.exceptions import ApiException

        v1 = self._api()
        ns = runtime.namespace
        want = (service or "").lower()
        since = max(1, int(minutes)) * 60
        level = (level or "").lower()
        matched_pods = 0
        lines: List[dict] = []
        pods = v1.list_namespaced_pod(ns)
        for pod in pods.items:
            name = pod.metadata.name
            app = (pod.metadata.labels or {}).get("app", "")
            if want and want not in name.lower() and want not in app.lower():
                continue
            matched_pods += 1
            for c in pod.spec.containers or []:
                try:
                    raw = v1.read_namespaced_pod_log(
                        name=name, namespace=ns, container=c.name,
                        since_seconds=since, tail_lines=500, timestamps=True,
                    )
                except ApiException:
                    continue
                for line in raw.splitlines():
                    if not line.strip():
                        continue
                    ts_token, _, rest = line.partition(" ")
                    msg, lvl = parse_line(rest)
                    if level and (lvl or "").lower() != level:
                        continue
                    lines.append({"ts": ts_token, "pod": name, "level": lvl or "", "message": msg[:400]})
        return {
            "service": service,
            "namespace": ns,
            "minutes": minutes,
            "matched_pods": matched_pods,
            "lines": lines[-max_lines:],
        }

    def _collect(self) -> List[LogEntry]:
        from kubernetes.client.exceptions import ApiException

        v1 = self._api()
        ns = runtime.namespace
        entries: List[LogEntry] = []
        pods = v1.list_namespaced_pod(ns)
        for pod in pods.items:
            pod_name = pod.metadata.name
            app = (pod.metadata.labels or {}).get("app", pod_name)
            for c in pod.spec.containers or []:
                try:
                    raw = v1.read_namespaced_pod_log(
                        name=pod_name,
                        namespace=ns,
                        container=c.name,
                        since_seconds=settings.lookback_seconds,
                        tail_lines=settings.k8s_tail_lines,
                        timestamps=True,
                    )
                except ApiException as exc:
                    log.debug("log read failed for %s/%s: %s", pod_name, c.name, exc)
                    continue
                for line in raw.splitlines():
                    if not line.strip():
                        continue
                    ts_token, _, rest = line.partition(" ")
                    message, level = parse_line(rest)
                    # Surface error/fatal lines, plus warn/levelless lines that match the
                    # error pattern OR a Kafka/event failure. Matching the parsed message
                    # (not the whole JSON) avoids false positives.
                    if not is_problem(message, level):
                        continue
                    entries.append(
                        LogEntry(
                            timestamp=_parse_ts(ts_token),
                            line=message,
                            labels={
                                "namespace": ns,
                                "pod": pod_name,
                                "container": c.name,
                                "app": app,
                                "level": (level or "error"),
                            },
                        )
                    )
        return entries


k8s_client = K8sClient()
