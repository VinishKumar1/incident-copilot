from __future__ import annotations

import hashlib
import json
import re
from typing import List

from .models import Issue, LogEntry

# Patterns that vary between otherwise-identical errors. Stripped before
# fingerprinting so "timeout after 1241ms" and "timeout after 87ms" group together.
_NUM = re.compile(r"\d+")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HEXLONG = re.compile(r"\b[0-9a-f]{16,}\b")
_LEVEL = re.compile(r"(?i)(fatal|panic|error|exception|traceback)")
# Matches "level":"DEBUG" or level=DEBUG or "level":"debug" etc. in log body
_LEVEL_FIELD = re.compile(r'(?i)["\']?level["\']?\s*[:=]\s*["\']?(\w+)["\']?')

# Levels to exclude from live issues — only errors, exceptions, and fatals are actionable
_EXCLUDED_LEVELS = {"warn", "warning", "debug", "info", "information", "trace", "verbose"}


def _normalize(line: str) -> str:
    s = line
    s = _TS.sub("<TS>", s)
    s = _UUID.sub("<UUID>", s)
    s = _IP.sub("<IP>", s)
    s = _HEX.sub("<HEX>", s)
    s = _HEXLONG.sub("<HEX>", s)
    s = _NUM.sub("<N>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fingerprint(line: str, service: str) -> str:
    norm = _normalize(line)
    h = hashlib.sha1(f"{service}|{norm}".encode("utf-8", "replace")).hexdigest()
    return h[:16]


def _level_from_body(line: str) -> str | None:
    """Try to extract a level field from the log line body (JSON or key=value)."""
    # Fast path: try JSON parse
    try:
        obj = json.loads(line)
        for key in ("level", "Level", "LEVEL", "severity", "log.level"):
            val = obj.get(key)
            if val:
                return str(val).lower()
    except Exception:
        pass
    # Fallback: regex for level=X or "level":"X"
    m = _LEVEL_FIELD.search(line)
    if m:
        return m.group(1).lower()
    return None


def detect_level(line: str) -> str:
    # First try to read level from the log body itself
    body_level = _level_from_body(line)
    if body_level:
        if body_level in ("fatal", "panic", "critical", "severe"):
            return "fatal"
        if body_level in _EXCLUDED_LEVELS:
            return body_level  # will be filtered out
    # Fall back to keyword scan
    m = _LEVEL.search(line)
    if not m:
        # No level field and no error keywords — skip (treat as non-actionable)
        return "info"
    word = m.group(1).lower()
    if word in ("fatal", "panic"):
        return "fatal"
    return "error"


def _title(line: str) -> str:
    # First meaningful chunk of the message, trimmed for display.
    norm = _normalize(line)
    return (norm[:160] + "…") if len(norm) > 160 else norm


def merge_entries(existing: List[Issue], entries: List[LogEntry]) -> List[Issue]:
    """Fold a batch of log entries into the issue list, returning the updated list.

    Only errors, exceptions, and fatals are included. Warnings are excluded
    — they are noise in the live issues view.
    """
    index = {i.id: i for i in existing}
    for e in entries:
        svc = e.service
        label_level = (e.labels.get("level") or "").lower()
        level = "fatal" if label_level in ("fatal", "panic", "critical", "severe") else (
            label_level if label_level in ("error", "warn", "warning") else detect_level(e.line)
        )
        # Skip warnings — live issues should only show errors and exceptions
        if level in _EXCLUDED_LEVELS:
            continue
        fp = fingerprint(e.line, svc)
        issue = index.get(fp)
        if issue is None:
            issue = Issue(
                id=fp,
                title=_title(e.line),
                level=level,
                service=svc,
                namespace=e.labels.get("namespace", ""),
                count=0,
                first_seen=e.timestamp,
                last_seen=e.timestamp,
                sample_line=e.line,
                labels=e.labels,
            )
            index[fp] = issue
        issue.count += 1
        issue.first_seen = min(issue.first_seen, e.timestamp)
        issue.last_seen = max(issue.last_seen, e.timestamp)
        if e.line not in issue.samples:
            issue.samples.insert(0, e.line)
            del issue.samples[5:]  # keep last 5 distinct samples
    return list(index.values())
