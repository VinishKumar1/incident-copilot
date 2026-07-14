from __future__ import annotations

import asyncio
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

# Apps to exclude from all live-issues and search queries (self-monitoring noise).
_EXCLUDED_APPS = r"tfr-backend|tfr-frontend"
_APP_EXCLUSION = f'app!~"{_EXCLUDED_APPS}"'


def _error_query(namespace: str) -> str:
    """Build a Loki LogQL query for the given namespace.
    Uses k8s_cluster as the second label so the query works for ANY namespace
    and satisfies Maersk Loki's 2-label minimum.
    Excludes lines whose level label OR JSON body level field is debug/info/trace."""
    return (
        f'{{namespace="{namespace}", {_cluster_selector()}, {_APP_EXCLUSION}}} '
        r'|~ "(?i)\\b(error|exception|fatal|panic|traceback)\\b"'
        r' | level!~"(?i)^(debug|info|information|trace|verbose)$"'
        r' != "\"level\":\"DEBUG\""'
        r' != "\"level\":\"INFO\""'
        r' != "\"level\":\"TRACE\""'
        r' != "level=DEBUG"'
        r' != "level=INFO"'
        r' != "level=TRACE"'
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
        # Track successful Grafana API calls
        if resp.status_code == 200 and settings.log_source == "grafana":
            import asyncio
            from .analytics import record_api_usage
            asyncio.ensure_future(record_api_usage("grafana", "loki"))
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
                params={},  # no cluster filter — return all namespaces across all clusters
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
        """Search for key across namespaces using per-namespace exact-match queries in parallel.

        Grafana Loki proxy returns 400 for long pipe-separated regex namespace patterns.
        This queries each namespace individually with exact match and runs them in parallel.
        """
        log.info("search_key: key=%r minutes=%d across %d namespaces", key, minutes, len(namespaces))

        CHUNK_MINUTES = 360
        end_ts = time.time()
        start_ts = end_ts - minutes * 60
        time_chunks: List[tuple] = []
        t = end_ts
        while t > start_ts:
            chunk_start = max(t - CHUNK_MINUTES * 60, start_ts)
            time_chunks.append((chunk_start, t))
            t = chunk_start

        _PROBLEM_RE = re.compile(r"(?i)\b(error|exception|fatal|panic|traceback|warn)\b")
        _EXCEPTION_CLASS_RE = re.compile(r"(?i)(Exception|Error)\b")
        _ERROR_LEVELS = {"error", "warn", "warning", "fatal", "panic", "severe", "critical", "err", "crit"}
        _SAFE_LEVELS  = {"info", "debug", "trace"}
        groups: Dict[tuple, dict] = {}
        all_trace_ids: List[str] = []

        url, headers = self._endpoint()

        async def _query_ns(client, ns: str, t_start: float, t_end: float):
            query = f'{{namespace="{ns}", {_cluster_selector()}, {_APP_EXCLUSION}}} |= "{key}"'
            params = {
                "query": query,
                "start": str(int(t_start * 1e9)),
                "end":   str(int(t_end * 1e9)),
                "limit": "500",
                "direction": "backward",
            }
            try:
                resp = await self._get(client, url, headers=headers, params=params)
                if resp.status_code < 400:
                    data = resp.json()
                    streams = data.get("data", {}).get("result", [])
                    if streams:
                        # Log first raw line to help debug trace ID extraction
                        first_line = streams[0].get("values", [[None, ""]])[0][1]
                        log.info("search_key raw sample ns=%s: %r", ns, first_line[:300])
                    return data
                log.warning("search_key: ns=%s status=%s: %s", ns, resp.status_code, resp.text[:100])
            except Exception as exc:
                log.warning("search_key: ns=%s error: %s", ns, exc)
            return None

        async with httpx.AsyncClient(timeout=settings.loki_timeout_seconds) as client:
            for chunk_start, chunk_end in time_chunks:
                tasks = [_query_ns(client, ns, chunk_start, chunk_end) for ns in namespaces]
                results = await asyncio.gather(*tasks)

                for data in results:
                    if not data:
                        continue
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
                            level = (lbl.get("level") or lbl.get("detected_level") or "").lower()
                            if not level:
                                m = re.search(r"(?i)\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line)
                                level = m.group(1).lower() if m else ""

                            trace_id = ""
                            # Check stream labels first (Loki may carry traceId as a label)
                            for label_key in ("traceId", "trace_id", "traceID", "traceid", "trace"):
                                if lbl.get(label_key):
                                    trace_id = lbl[label_key]
                                    break
                            # Fall back to scanning the log line text
                            if not trace_id:
                                for pattern in (
                                    r'"[Tt]race[_\-]?[Ii][Dd]"\s*:\s*"([0-9a-fA-F\-]{16,})"',
                                    r'[Tt]race[_\-]?[Ii][Dd]["\s:=,\]]+([0-9a-fA-F\-]{16,})',
                                    r'traceparent[=: ]+\d{2}-([0-9a-fA-F]{32})-',
                                    r'[Xx]-[Bb]3-[Tt]race[Ii][Dd][=: ]+([0-9a-fA-F]{16,32})',
                                    # bare 32-hex trace ID anywhere in line
                                    r'\b([0-9a-fA-F]{32})\b',
                                ):
                                    tm = re.search(pattern, line)
                                    if tm:
                                        trace_id = tm.group(1).replace("-", "")
                                        break

                            if trace_id and trace_id not in grp["trace_ids"]:
                                grp["trace_ids"].append(trace_id)
                            if trace_id and trace_id not in all_trace_ids:
                                all_trace_ids.append(trace_id)

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

        log.info("search_key: key=%r found %d log lines, extracted %d trace IDs: %s",
                 key, total_matches, len(all_trace_ids), all_trace_ids[:5])

        trace_ids_to_follow = list(all_trace_ids)
        _TRACE_RE = re.compile(r'^[0-9a-fA-F]{16,32}$')
        if total_matches == 0 and _TRACE_RE.match(key.strip()):
            log.info("search_key: no log lines found — treating key as trace ID for phase-2")
            trace_ids_to_follow = [key.strip()]

        # Services that matched the key — passed to _search_trace_ids for future use
        services_with_hits = {(g["namespace"], g["service"]) for g in services}

        trace_issues: List[dict] = []
        if trace_ids_to_follow:
            trace_issues = await self._search_trace_ids(
                trace_ids_to_follow, namespaces, start_ts, end_ts,
                hit_groups=list(groups.values()),
            )

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
        hit_groups: List[dict] = None,
    ) -> List[dict]:
        """Phase 2: search for errors sharing trace IDs found in phase 1.
        Only searches namespaces where the key was matched. Uses 6h chunks
        to stay within Grafana's max query range. Sequential per chunk to avoid 429.
        """
        if not trace_ids:
            return []

        # Search ALL available namespaces for trace errors — the error could be in a
        # different service/namespace than where the key was found (e.g. key found in
        # iom-offer-service but error is in iom-order-service or vice versa).
        search_namespaces = list({g["namespace"] for g in hit_groups}) if hit_groups else namespaces[:3]

        # Use same 6h chunks as phase 1 to stay within Grafana's max range
        CHUNK_MINUTES = 360
        time_chunks: List[tuple] = []
        t = end_ts
        while t > start_ts:
            chunk_start = max(t - CHUNK_MINUTES * 60, start_ts)
            time_chunks.append((chunk_start, t))
            t = chunk_start

        # Use up to 5 trace IDs but filter to errors only in each query — much more
        # efficient than fetching all lines per trace (avoids 429 from large result sets).
        search_trace_ids = trace_ids[:5]

        _ERROR_LEVELS = {"error", "warn", "warning", "fatal", "panic", "severe", "critical", "err", "crit"}
        _SAFE_LEVELS  = {"info", "debug", "trace"}
        _PROBLEM_RE   = re.compile(r"(?i)\b(error|exception|fatal|panic|traceback|warn)\b")

        groups: Dict[tuple, dict] = {}
        url, headers = self._endpoint()

        async def _query_trace_ns(client, trace_id: str, ns: str, chunk_start: float, chunk_end: float):
            # Only fetch log lines that contain error/exception keywords for this trace.
            # This lets us check many more trace IDs without blasting Grafana.
            query = (
                f'{{namespace="{ns}", {_cluster_selector()}, {_APP_EXCLUSION}}} '
                f'|= "{trace_id}" '
                f'|~ "(?i)\\b(error|exception|fatal|panic|traceback|EXCEPTION|ERROR)\\b"'
            )
            params = {
                "query": query,
                "start": str(int(chunk_start * 1e9)),
                "end":   str(int(chunk_end   * 1e9)),
                "limit": "200",
                "direction": "backward",
            }
            for attempt in range(3):
                try:
                    resp = await self._get(client, url, headers=headers, params=params)
                    return trace_id, resp.json()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status == 429:
                        wait = 2 ** attempt
                        log.warning("_search_trace_ids: 429 ns=%s trace=%s, retry in %ds", ns, trace_id[:8], wait)
                        await asyncio.sleep(wait)
                        continue
                    log.warning("_search_trace_ids: ns=%s trace=%s status=%s: %s", ns, trace_id[:8], status, exc.response.text[:80])
                    break
                except Exception as exc:
                    log.warning("_search_trace_ids: ns=%s trace=%s error: %s", ns, trace_id[:8], exc)
                    break
            return trace_id, None

        # Brief pause after phase 1's burst of parallel queries
        await asyncio.sleep(1)
        log.info("_search_trace_ids: checking %d trace IDs across %d namespaces",
                 len(search_trace_ids), len(search_namespaces))

        async with httpx.AsyncClient(timeout=settings.loki_timeout_seconds) as client:
            semaphore = asyncio.Semaphore(2)

            async def _bounded_query(tid, ns, chunk_start, chunk_end):
                async with semaphore:
                    return await _query_trace_ns(client, tid, ns, chunk_start, chunk_end)

            all_results = []
            for chunk_start, chunk_end in time_chunks:
                tasks = [
                    _bounded_query(tid, ns, chunk_start, chunk_end)
                    for tid in search_trace_ids
                    for ns in search_namespaces
                ]
                chunk_results = await asyncio.gather(*tasks)
                all_results.extend(chunk_results)
                if any(r[1] for r in chunk_results):
                    break  # found results — no need to go further back
            results = all_results

        for trace_id, data in results:
            if not data:
                continue
            for stream in data.get("data", {}).get("result", []):
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

                    # For trace-matched lines: trust the log text content over the
                    # Loki level label. Java/Spring apps often emit ERROR text but
                    # Loki labels them "info" due to misconfigured log parsers.
                    text_level_match = re.search(r"(?i)\b(ERROR|WARN|EXCEPTION|FATAL|PANIC)\b", line)
                    effective_level = level if level in _ERROR_LEVELS else (
                        text_level_match.group(1).lower() if text_level_match else level
                    )

                    is_problem = (
                        effective_level in _ERROR_LEVELS
                        or bool(_PROBLEM_RE.search(line))
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
                        "level":     effective_level or "error",
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
