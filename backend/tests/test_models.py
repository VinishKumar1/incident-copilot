import pytest
from pydantic import ValidationError

from app.models import (
    AnalyzeResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CodeMatchFile,
    CodeMatchResponse,
    FixEdit,
    FixResponse,
    Issue,
    LogEntry,
    NamespaceSummaryIssue,
    NamespaceSummaryResponse,
    SearchMatch,
    SearchResponse,
    SearchServiceGroup,
    SearchSummaryIssue,
    SearchSummaryResponse,
)


def test_log_entry_service_falls_back_through_labels():
    assert LogEntry(timestamp=1, line='x', labels={'app': 'svc'}).service == 'svc'
    assert LogEntry(timestamp=1, line='x', labels={'container': 'ctr'}).service == 'ctr'
    assert LogEntry(timestamp=1, line='x', labels={'pod': 'pod-1'}).service == 'pod-1'
    assert LogEntry(timestamp=1, line='x', labels={}).service == 'unknown'


def test_models_construct_with_defaults():
    issue = Issue(id='1', title='boom')
    assert issue.level == 'error'
    assert issue.service == 'unknown'
    assert issue.samples == []

    analysis = AnalyzeResponse(issue_id='1', summary='summary')
    assert analysis.cached is False
    assert analysis.likely_causes == []

    code_match = CodeMatchResponse(issue_id='1')
    assert code_match.located is False
    assert code_match.files == []
    assert code_match.reason == ''

    fix = FixResponse(issue_id='1')
    assert fix.created is False
    assert fix.base == 'develop'
    assert fix.edits == []

    service_group = SearchServiceGroup(service='svc')
    assert service_group.problem_count == 0
    assert service_group.trace_ids == []

    search = SearchResponse(key='booking-1')
    assert search.total_matches == 0
    assert search.trace_issues == []

    summary = SearchSummaryResponse(key='booking-1')
    assert summary.found is False
    assert summary.issues == []

    namespace_summary = NamespaceSummaryResponse(namespace='telikos', minutes=5)
    assert namespace_summary.overall_health == 'unknown'
    assert namespace_summary.cached is False

    assert CodeMatchFile(path='a.py').reason == ''
    assert FixEdit(path='a.py').explanation == ''
    assert SearchMatch().message == ''
    assert SearchSummaryIssue().text == ''
    assert NamespaceSummaryIssue().count == 0
    assert ChatResponse(reply='ok').reply == 'ok'


def test_models_validate_required_fields_and_nested_types():
    with pytest.raises(ValidationError):
        LogEntry(line='missing timestamp', labels={})
    with pytest.raises(ValidationError):
        Issue(title='missing id')
    with pytest.raises(ValidationError):
        SearchServiceGroup(total=1)
    with pytest.raises(ValidationError):
        ChatMessage(content='missing role')
    with pytest.raises(ValidationError):
        ChatRequest(messages='not-a-list')

    req = ChatRequest(messages=[{'role': 'user', 'content': 'hello'}])
    assert req.messages[0].role == 'user'
