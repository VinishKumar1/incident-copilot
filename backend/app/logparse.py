from __future__ import annotations

import json
import re
from typing import Optional, Tuple

# Common field names across logback/logstash/zap/bunyan/etc.
_MSG_KEYS = ("message", "msg", "log", "@message", "event", "text")
_LEVEL_KEYS = ("level", "severity", "lvl", "log_level", "loglevel", "@level")
_TRACE_KEYS = ("stack_trace", "stackTrace", "exception", "throwable", "error", "err")

ERROR_LEVELS = {"error", "fatal", "severe", "critical", "err", "crit", "emerg", "alert"}


def parse_line(raw: str) -> Tuple[str, Optional[str]]:
    """Return (display_message, level) for a log line.

    Handles structured JSON logs (extracts the human message + a stack-trace head)
    and falls back to the raw line for plain-text logs. level is lowercased or None.
    """
    s = raw.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return s, None
    try:
        obj = json.loads(s)
    except Exception:
        return s, None
    if not isinstance(obj, dict):
        return s, None

    msg = next((str(obj[k]) for k in _MSG_KEYS if obj.get(k)), None) or s

    # Append the first line of any stack trace / exception for context.
    for tk in _TRACE_KEYS:
        val = obj.get(tk)
        if isinstance(val, str) and val.strip():
            first = val.strip().splitlines()[0]
            if first and first not in msg:
                msg = f"{msg} | {first}"
            break

    level = next((str(obj[k]).lower() for k in _LEVEL_KEYS if obj.get(k)), None)
    return msg, level


_TRACE_KEYS = ("traceId", "trace_id", "traceID", "traceid", "dd.trace_id", "X-B3-TraceId", "x-b3-traceid")
_TRACE_RE = re.compile(r"trace[_-]?id[=:\s\"\]]+([0-9a-fA-F]{8,})", re.I)


def extract_trace(raw: str) -> str:
    """Best-effort trace id from a log line (JSON field or 'traceId=...' text)."""
    s = raw.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            for k in _TRACE_KEYS:
                if obj.get(k):
                    return str(obj[k])
            mdc = obj.get("mdc")
            if isinstance(mdc, dict):
                for k in ("traceId", "trace_id", "traceID"):
                    if mdc.get(k):
                        return str(mdc[k])
    m = _TRACE_RE.search(raw)
    return m.group(1) if m else ""
