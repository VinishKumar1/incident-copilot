from app.grouping import _level_from_body, detect_level, fingerprint, merge_entries
from app.models import LogEntry


def test_detect_level_prefers_json_level_fields():
    assert detect_level('{"level":"DEBUG","message":"hello"}') == 'debug'
    assert detect_level('{"level":"ERROR","message":"boom"}') == 'error'
    assert detect_level('{"level":"INFO","message":"ok"}') == 'info'


def test_detect_level_falls_back_to_keyword_and_handles_reactor_netty_case():
    assert detect_level('plain text error happened') == 'error'
    assert detect_level('{"level":"DEBUG","error":null,"message":"reactor.netty"}') == 'debug'


def test_level_from_body_supports_json_regex_and_key_value_formats():
    assert _level_from_body('{"severity":"WARN"}') == 'warn'
    assert _level_from_body('{bad json level="ERROR"}') == 'error'
    assert _level_from_body('ts=1 level=INFO msg=hello') == 'info'


def test_fingerprint_normalizes_numbers_but_distinguishes_messages():
    first = fingerprint('timeout after 1241ms for request 99', 'svc-a')
    second = fingerprint('timeout after 87ms for request 12', 'svc-a')
    third = fingerprint('connection refused for request 12', 'svc-a')
    assert first == second
    assert first != third


def test_merge_entries_keeps_errors_and_merges_duplicates():
    existing = []
    entries = [
        LogEntry(timestamp=10, line='{"level":"DEBUG","message":"skip me"}', labels={'app': 'svc'}),
        LogEntry(timestamp=11, line='ERROR timeout after 100ms', labels={'app': 'svc'}),
        LogEntry(timestamp=12, line='ERROR timeout after 999ms', labels={'app': 'svc'}),
        LogEntry(timestamp=13, line='{"level":"INFO","message":"skip info"}', labels={'app': 'svc'}),
        LogEntry(timestamp=14, line='{"level":"WARN","message":"skip warning"}', labels={'app': 'svc', 'level': 'warning'}),
    ]

    merged = merge_entries(existing, entries)

    assert len(merged) == 1
    issue = merged[0]
    assert issue.level == 'error'
    assert issue.count == 2
    assert issue.first_seen == 11
    assert issue.last_seen == 12
    assert len(issue.samples) == 2
