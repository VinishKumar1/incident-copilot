from app.logparse import extract_trace, parse_line


def test_parse_line_returns_plain_text_unchanged():
    line = 'simple log line'
    assert parse_line(line) == ('simple log line', None)


def test_parse_line_extracts_message_level_and_stack_head():
    raw = '{"message":"failed request","level":"ERROR","stack_trace":"ValueError: nope\\n  at x"}'
    message, level = parse_line(raw)
    assert message == 'failed request | ValueError: nope'
    assert level == 'error'


def test_parse_line_uses_alternate_message_and_level_keys():
    raw = '{"msg":"worker crashed","severity":"CRITICAL","error":"RuntimeError: bad\\ntrace"}'
    message, level = parse_line(raw)
    assert message == 'worker crashed | RuntimeError: bad'
    assert level == 'critical'


def test_parse_line_handles_invalid_json_and_non_dict_json():
    bad = '{not valid json'
    arr = '[1,2,3]'
    assert parse_line(bad) == (bad, None)
    assert parse_line(arr) == (arr, None)


def test_extract_trace_reads_json_text_and_mdc_values():
    assert extract_trace('{"traceId":"abcdef12"}') == 'abcdef12'
    assert extract_trace('{"mdc":{"trace_id":"feedbeef"}}') == 'feedbeef'
    assert extract_trace('message traceId=deadbeef1234 more') == 'deadbeef1234'
    assert extract_trace('no trace here') == ''
