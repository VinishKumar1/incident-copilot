from __future__ import annotations

import logging
import re
import time
from typing import Dict, List

import httpx

from .config import settings
from .models import LogEntry

log = logging.getLogger(__name__)

def _cluster_selector() -> str:
    """Return a k8s_cluster label matcher covering all configured clusters.
    Used as the mandatory second label in every Loki query."""
    clusters = settings.k8s_cluster_list
    if len(clusters) == 1:
        return f'k8s_cluster="{clusters[0]}"'
    pattern = "|".join(clusters)
    return f'k8s_cluster=~"{pattern}"'


def _error_query(namespace: str) -> str:
    """Build a Loki LogQL query for the given namespace.
    Uses k8s_cluster as the second label so the query works for ANY namespace
    and satisfies Maersk Loki's 2-label minimum."""
    return (
        f'{{namespace="{namespace}", {_cluster_selector()}}} '
        r'|~ "(?i)\\b(error|exception|fatal|panic|traceback)\\b"'
    )

# Synthetic log lines used in mock mode so the app runs with no infra.
_MOCK_TEMPLATES = [
    ('order-service', 'ERROR c.t.o.OrderController - Unhandled exception: java.lang.NullPointerException at OrderMapper.map(OrderMapper.java:42)'),
    ('order-service', 'ERROR c.t.o.OrderController - Unhandled exception: java.lang.NullPointerException at OrderMapper.map(OrderMapper.java:42)'),
    ('booking-intake', 'ERROR Connection pool exhausted: could not get connection from pool after 30000ms (HikariPool-1)'),
    ('booking-intake', 'ERROR Connection pool exhausted: could not get connection from pool after 28411ms (HikariPool-1)'),
    ('document-storage', 'EXCEPTION software.amazon.awssdk.services.s3.S3Exception: Access Denied (Service: S3, Status Code: 403, Request ID: 8F2A1)'),
    ('master-data', 'FATAL OutOfMemoryError: Java heap space; pod restarting'),
    ('offer-service', 'ERROR org.postgresql.util.PSQLException: ERROR: deadlock detected'),
    ('offer-service', 'ERROR org.postgresql.util.PSQLException: ERROR: deadlock detected'),
    ('offer-service', 'ERROR org.postgresql.util.PSQLException: ERROR: deadlock detected'),
    ('web-integrator', 'ERROR feign.RetryableException: Read timed out executing GET http://master-data/api/v1/ports'),
]


class LokiClient:
    """Queries Loki's query_range API for recent error-level lines."""

    def __init__(self) -> None:
        self._mock_cursor = 0

    async def fetch_recent_errors(self) -> List[LogEntry]:
        if settings.use_mock:
            return self._mock_batch()
        return await self._query_loki()

    def _mock_batch(self) -> List[LogEntry]:
        now = time.time()
        # Rotate through a few templates each poll to simulate a live stream.
        batch = []
        for offset in range(3):
            svc, line = _MOCK_TEMPLATES[self._mock_cursor % len(_MOCK_TEMPLATES)]
            self._mock_cursor += 1
            batch.append(
                LogEntry(
                    timestamp=now - offset,
                    line=line,
                    labels={"namespace": settings.k8s_namespace, "app": svc, "pod": f"{svc}-7d9", "level": "error"},
                )
            )
        return batch

    def _grafana_token(self) -> str:
        """Return the current Grafana token — prefers the in-memory value kept
        fresh by the background refresh loop, falls back to .env then settings."""
        from .token_refresh import get_live_token
        live = get_live_token()
        if live:
            return live
        # Fall back to reading .env directly (covers the window before first refresh).
        from pathlib import Path
        env_file = Path(__file__).parent.parent / ".env"
        try:
            for line in env_file.read_text().splitlines():
                if line.startswith("GRAFANA_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    if token:
                        return token
        except Exception:
            pass
        return settings.grafana_token

    async def _get(self, client: httpx.AsyncClient, url: str, headers: dict, **kwargs) -> httpx.Response:
        """GET with automatic token refresh + retries on Grafana 401."""
        resp = await client.get(url, headers=headers, **kwargs)
        if resp.status_code == 401 and settings.log_source == "grafana":
            import logging
            from .token_refresh import _refresh_once
            for attempt in range(1, 4):  # up to 3 refresh attempts
                logging.getLogger("loki").warning("Grafana 401 — refresh attempt %d/3…", attempt)
                success = await _refresh_once()
                _, headers = self._endpoint()  # pick up freshly-set _live_token
                resp = await client.get(url, headers=headers, **kwargs)
                if resp.status_code != 401:
                    break
                if not success:
                    import asyncio
                    await asyncio.sleep(5)  # brief wait before next attempt
        resp.raise_for_status()
        return resp

    def _endpoint(self) -> tuple:
        """Return (url, headers) for the Loki query_range API, direct or via Grafana."""
        path = "loki/api/v1/query_range"
        if settings.log_source == "grafana":
            base = settings.grafana_url.rstrip("/")
            uid = settings.grafana_datasource_uid
            url = f"{base}/api/datasources/proxy/uid/{uid}/{path}"
            headers = {"Authorization": f"Bearer {self._grafana_token()}"}
            return url, headers
        url = f"{settings.loki_url.rstrip('/')}/{path}"
        return url, {}

    def _labels_endpoint(self, label: str) -> tuple:
        """Return (url, headers) for Loki's label values API."""
        path = f"loki/api/v1/label/{label}/values"
        if settings.log_source == "grafana":
            base = settings.grafana_url.rstrip("/")
            uid = settings.grafana_datasource_uid
            url = f"{base}/api/datasources/proxy/uid/{uid}/{path}"
            headers = {"Authorization": f"Bearer {self._grafana_token()}"}
            return url, headers
        url = f"{settings.loki_url.rstrip('/')}/{path}"
        return url, {}

    async def list_namespaces(self) -> List[str]:
        """Fetch application namespaces from configured k8s clusters via Loki's label API.
        Excludes well-known infrastructure/system namespaces."""
        _SYSTEM_NS = {
            "calico-system", "cert-manager", "chaos-mesh", "external-dns",
            "external-secrets", "flux-system", "gringotts-opencost", "infra-kyverno",
            "ingress-nginx", "keda", "kube-downscaler", "kube-janitor", "kube-system",
            "kuma-gateway", "kuma-system", "oauth2-proxy", "perpetual-mss-webhook",
            "platform-jobs", "platform-monitoring", "tigera-operator",
            "vault-secrets-webhook", "vpa",
        }
        clusters = settings.k8s_cluster_list
        cluster_pattern = "|".join(clusters)
        url, headers = self._labels_endpoint("namespace")
        # Strip the path suffix and add the query param for cluster filtering.
        base_url = url.rsplit("/label/", 1)[0]
        ns_url = f"{base_url}/label/namespace/values"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await self._get(
                client, ns_url,
                headers=headers,
                params={"query": f'{{k8s_cluster=~"{cluster_pattern}"}}'},
            )
            data = resp.json()

        all_ns = data.get("data", [])
        filtered = sorted(
            ns for ns in all_ns
            if ns not in _SYSTEM_NS
            and not ns.startswith("falcon-")
        )
        return filtered if filtered else [settings.k8s_namespace]

    async def _query_loki(self) -> List[LogEntry]:
        from .state import runtime

        # Use a dynamic query so the active namespace (switchable from the UI) and its
        # env label are always in sync — satisfies Loki's 2-label minimum.
        query = _error_query(runtime.namespace)
        end = time.time()
        start = end - settings.lookback_seconds
        params = {
            "query": query,
            "start": str(int(start * 1e9)),  # nanoseconds
            "end": str(int(end * 1e9)),
            "limit": str(settings.max_lines_per_poll),
            "direction": "backward",
        }
        url, headers = self._endpoint()
        async with httpx.AsyncClient(timeout=settings.loki_timeout_seconds) as client:
            resp = await self._get(client, url, headers=headers, params=params)
            data = resp.json()

        entries: List[LogEntry] = []
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                entries.append(
                    LogEntry(timestamp=int(ts_ns) / 1e9, line=line, labels=labels)
                )
        return entries

    async def search_key(self, key: str, namespaces: List[str], minutes: int) -> dict:
        """Search all log levels (not just errors) for a key string across namespaces.

        Works with both direct Loki and Grafana proxy sources.
        Long namespace lists are chunked into batches of 10 to avoid Loki's regex limit.
        Long time windows are chunked into 6-hour slices to stay within Loki's scan limit.
        Uses k8s_cluster as second label so it works for any namespace.
        """
        log.info("search_key: searching key=%r across %d namespaces: %s", key, len(namespaces), namespaces)

        # Split namespaces into batches of 10 to keep the Loki regex short enough (avoids 400).
        NS_BATCH_SIZE = 10
        ns_batches = [namespaces[i:i + NS_BATCH_SIZE] for i in range(0, len(namespaces), NS_BATCH_SIZE)]

        # Chunk into 6-hour slices to avoid Loki's per-query byte limit.
        CHUNK_MINUTES = 360
        end_ts = time.time()
        start_ts = end_ts - minutes * 60
        time_chunks: List[tuple] = []
        t = end_ts
        while t > start_ts:
            chunk_start = max(t - CHUNK_MINUTES * 60, start_ts)
            time_chunks.append((chunk_start, t))
            t = chunk_start

        # All combinations of namespace batch × time chunk
        chunks: List[tuple] = [
            (ns_batch, chunk_start, chunk_end)
            for ns_batch in ns_batches
            for chunk_start, chunk_end in time_chunks
        ]

        # Matches keyword-in-text for logs with no structured level (plain-text fallback).
        # Uses word boundaries to avoid false positives from class names like NullPointerException.
        _PROBLEM_RE = re.compile(r"(?i)\b(error|exception|fatal|panic|traceback|warn)\b")
        # Looser match for exception *class names* (e.g. NullPointerException, RuntimeException).
        # Only used when the log level is already known to be non-error to avoid false positives.
        _EXCEPTION_CLASS_RE = re.compile(r"(?i)(Exception|Error)\b")
        _ERROR_LEVELS = {"error", "warn", "warning", "fatal", "panic", "severe", "critical", "err", "crit"}
        _SAFE_LEVELS  = {"info", "debug", "trace"}
        groups: Dict[tuple, dict] = {}
        all_trace_ids: List[str] = []

        url, headers = self._endpoint()
        async with httpx.AsyncClient(timeout=settings.loki_timeout_seconds) as client:
            for ns_batch, chunk_start, chunk_end in chunks:
                ns_pattern = "|".join(ns_batch)
                query = f'{{namespace=~"{ns_pattern}", {_cluster_selector()}}} |= "{key}"'
                params = {
                    "query": query,
                    "start": str(int(chunk_start * 1e9)),
                    "end": str(int(chunk_end * 1e9)),
                    "limit": "2000",
                    "direction": "backward",
                }
                try:
                    resp = await self._get(client, url, headers=headers, params=params)
                    data = resp.json()
                    result_count = len(data.get("data", {}).get("result", []))
                    log.info("search_key: batch(%d ns) chunk query returned %d streams (status=%s)", len(ns_batch), result_count, resp.status_code)
                    if resp.status_code >= 400:
                        log.warning("search_key: loki error response: %s", resp.text[:500])
                except Exception as exc:
                    log.warning("search_key: chunk query failed: %s", exc)
                    continue  # skip failed chunks, don't abort the whole search

                for stream in data.get("data", {}).get("result", []):
                    lbl = stream.get("stream", {})
                    ns = lbl.get("namespace", namespaces[0] if namespaces else "")
                    svc = (
                        lbl.get("app")
                        or lbl.get("container")
                        or lbl.get("pod", "unknown").rsplit("-", 2)[0]
                    )
                    grp_key = (ns, svc)
                    if grp_key not in groups:
                        groups[grp_key] = {
                            "namespace": ns,
                            "service": svc,
                            "total": 0,
                            "problem_count": 0,
                            "problems": [],
                            "trace_ids": [],
                        }
                    grp = groups[grp_key]

                    for ts_ns, line in stream.get("values", []):
                        grp["total"] += 1
                        # Prefer the Loki stream label; fall back to scanning the line text.
                        level = (lbl.get("level") or lbl.get("detected_level") or "").lower()
                        if not level:
                            m = re.search(r"(?i)\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line)
                            level = m.group(1).lower() if m else ""

                        trace_id = ""
                        for pattern in (
                            # JSON: "traceId": "abc123"
                            r'"[Tt]race[_-]?[Ii]d"\s*:\s*"([^"]{8,})"',
                            # logfmt: traceId=abc123 or trace_id=abc123
                            r'[Tt]race[_-]?[Ii]d=([0-9a-fA-F\-]{8,})',
                            # W3C traceparent: 00-<traceId(32 hex)>-<spanId(16 hex)>-xx
                            r'traceparent[=: ]+\d{2}-([0-9a-fA-F]{32})-',
                            # plain 32-hex UUID-style or 16-hex trace id after "trace" keyword
                            r'(?i)trace["\s:=]+([0-9a-fA-F]{32})',
                            r'(?i)trace["\s:=]+([0-9a-fA-F]{16})',
                            # Grafana/OpenTelemetry: X-B3-TraceId or similar
                            r'[Xx]-[Bb]3-[Tt]race[Ii]d[=: ]+([0-9a-fA-F]{16,32})',
                        ):
                            tm = re.search(pattern, line)
                            if tm:
                                trace_id = tm.group(1)
                                break

                        if trace_id and trace_id not in grp["trace_ids"]:
                            grp["trace_ids"].append(trace_id)
                        if trace_id and trace_id not in all_trace_ids:
                            all_trace_ids.append(trace_id)

                        # A line is a problem if:
                        # 1. Its level label is a known error/warn level, OR
                        # 2. Level is unknown and the raw line contains problem keywords
                        is_problem = level in _ERROR_LEVELS or (
                            level not in _SAFE_LEVELS and bool(_PROBLEM_RE.search(line))
                        )
                        if is_problem:
                            grp["problem_count"] += 1
                            grp["problems"].append({
                                "ts": str(int(ts_ns) // int(1e6)),
                                "namespace": ns,
                                "service": svc,
                                "pod": lbl.get("pod", ""),
                                "level": level or "error",
                                "message": line[:500],
                                "trace_id": trace_id,
                            })

        services = sorted(groups.values(), key=lambda g: g["problem_count"], reverse=True)
        total_matches = sum(g["total"] for g in services)
        total_problems = sum(g["problem_count"] for g in services)

        # ── Phase 2: follow trace IDs into other services ──────────────────────
        log.info("search_key: key=%r found %d log lines, extracted %d trace IDs: %s",
                 key, total_matches, len(all_trace_ids), all_trace_ids[:5])

        # If Phase 1 found nothing at all, the key itself might be a trace ID
        # (e.g. user pastes a trace ID directly), OR the booking number doesn't
        # appear verbatim in log text but its trace ID is known.  Try the key as
        # a trace ID so the user gets results either way.
        trace_ids_to_follow = list(all_trace_ids)
        _TRACE_RE = re.compile(r'^[0-9a-fA-F]{16,32}$')
        if total_matches == 0 and _TRACE_RE.match(key.strip()):
            log.info("search_key: no log lines found for key=%r — treating as trace ID for phase-2", key)
            trace_ids_to_follow = [key.strip()]

        trace_issues: List[dict] = []
        if trace_ids_to_follow:
            trace_issues = await self._search_trace_ids(
                trace_ids_to_follow, namespaces, start_ts, end_ts
            )
            log.info("search_key: phase-2 trace search found %d service groups with errors", len(trace_issues))

        return {
            "key": key,
            "namespace": ", ".join(namespaces),
            "namespaces": namespaces,
            "minutes": minutes,
            "total_matches": total_matches,
            "problem_count": total_problems,
            "services": services,
            "trace_ids": all_trace_ids,
            "trace_issues": trace_issues,
        }

    async def _search_trace_ids(
        self,
        trace_ids: List[str],
        namespaces: List[str],
        start_ts: float,
        end_ts: float,
    ) -> List[dict]:
        """Phase 2: for each trace ID found in phase 1, search across ALL
        namespaces for error-level log lines that carry the same trace ID.
        This surfaces failures in downstream services that were triggered by
        the original key (e.g. a booking number calls offer-service which
        calls pricing-service which fails — the error shows up here).
        """
        if not trace_ids:
            return []

        _ERROR_LEVELS = {"error", "warn", "warning", "fatal", "panic", "severe", "critical", "err", "crit"}
        _SAFE_LEVELS  = {"info", "debug", "trace"}
        _PROBLEM_RE   = re.compile(r"(?i)\b(error|exception|fatal|panic|traceback|warn)\b")

        # Use the same namespaces as the original search — Maersk Loki requires
        # a namespace label in every query. We search the same namespace set
        # so we catch failures in different services within the same namespace.
        ns_pattern = "|".join(namespaces)
        groups: Dict[tuple, dict] = {}

        url, headers = self._endpoint()
        async with httpx.AsyncClient(timeout=settings.loki_timeout_seconds) as client:
            for trace_id in trace_ids[:10]:  # cap at 10 trace IDs to avoid excessive queries
                # Search for this trace ID across the given namespaces
                query = f'{{namespace=~"{ns_pattern}", {_cluster_selector()}}} |= "{trace_id}"'
                params = {
                    "query": query,
                    "start": str(int(start_ts * 1e9)),
                    "end":   str(int(end_ts   * 1e9)),
                    "limit": "500",
                    "direction": "backward",
                }
                log.debug("_search_trace_ids: querying trace_id=%r query=%r", trace_id, query)
                try:
                    resp = await self._get(client, url, headers=headers, params=params)
                    data = resp.json()
                except Exception as exc:
                    log.warning("_search_trace_ids: query failed for trace_id=%r: %s", trace_id, exc)
                    continue

                result_streams = data.get("data", {}).get("result", [])
                log.debug("_search_trace_ids: trace_id=%r → %d streams", trace_id, len(result_streams))

                for stream in result_streams:
                    lbl  = stream.get("stream", {})
                    ns   = lbl.get("namespace", "")
                    svc  = (
                        lbl.get("app")
                        or lbl.get("container")
                        or lbl.get("pod", "unknown").rsplit("-", 2)[0]
                    )
                    grp_key = (ns, svc)

                    for ts_ns, line in stream.get("values", []):
                        level = (lbl.get("level") or lbl.get("detected_level") or "").lower()
                        if not level:
                            m = re.search(r"(?i)\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line)
                            level = m.group(1).lower() if m else ""

                        is_problem = level in _ERROR_LEVELS or (
                            level not in _SAFE_LEVELS and bool(_PROBLEM_RE.search(line))
                        )
                        if not is_problem:
                            continue

                        if grp_key not in groups:
                            groups[grp_key] = {
                                "namespace": ns,
                                "service":   svc,
                                "total":     0,
                                "problem_count": 0,
                                "problems":  [],
                                "trace_ids": [trace_id],
                            }
                        grp = groups[grp_key]
                        if trace_id not in grp["trace_ids"]:
                            grp["trace_ids"].append(trace_id)
                        grp["total"] += 1
                        grp["problem_count"] += 1
                        grp["problems"].append({
                            "ts":        str(int(ts_ns) // int(1e6)),
                            "namespace": ns,
                            "service":   svc,
                            "pod":       lbl.get("pod", ""),
                            "level":     level or "error",
                            "message":   line[:500],
                            "trace_id":  trace_id,
                        })

        return sorted(groups.values(), key=lambda g: g["problem_count"], reverse=True)


    async def get_service_logs(
        self, service: str, namespace: str, minutes: int, level: str, max_lines: int
    ) -> dict:
        """On-demand log fetch for a named service via Loki/Grafana."""
        level_filter = f' |~ "(?i){level}"' if level else ""
        query = f'{{namespace="{namespace}", {_cluster_selector()}, app=~".*{re.escape(service)}.*"}}{level_filter}'
        end = time.time()
        start = end - minutes * 60
        params = {
            "query": query,
            "start": str(int(start * 1e9)),
            "end": str(int(end * 1e9)),
            "limit": str(max_lines),
            "direction": "backward",
        }
        url, headers = self._endpoint()
        async with httpx.AsyncClient(timeout=settings.loki_timeout_seconds) as client:
            resp = await self._get(client, url, headers=headers, params=params)
            data = resp.json()

        lines = []
        for stream in data.get("data", {}).get("result", []):
            lbl = stream.get("stream", {})
            pod = lbl.get("pod", "")
            for ts_ns, line in stream.get("values", []):
                lv = (lbl.get("level") or "").lower()
                if not lv:
                    m = re.search(r"(?i)\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line)
                    lv = m.group(1).lower() if m else "info"
                lines.append({"ts": str(int(ts_ns) // int(1e6)), "pod": pod, "level": lv, "message": line[:500]})

        lines.sort(key=lambda x: x["ts"])
        return {
            "service": service,
            "namespace": namespace,
            "minutes": minutes,
            "matched_pods": len({ln["pod"] for ln in lines}),
            "lines": lines[:max_lines],
        }


loki_client = LokiClient()
