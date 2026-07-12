import pytest

from app.models import AnalyzeResponse, CodeMatchResponse, Issue, LogEntry, NamespaceSummaryResponse
from app.store import IssueStore


@pytest.mark.asyncio
async def test_issue_store_ingest_and_clear():
    store = IssueStore()
    entries = [
        LogEntry(timestamp=1, line='ERROR timeout after 100ms', labels={'app': 'svc', 'namespace': 'ns'}),
        LogEntry(timestamp=2, line='ERROR timeout after 200ms', labels={'app': 'svc', 'namespace': 'ns'}),
    ]

    await store.ingest(entries)
    issues = await store.list_issues()

    assert len(issues) == 1
    assert issues[0].count == 2

    await store.clear()
    assert await store.list_issues() == []
    assert await store.get(issues[0].id) is None


@pytest.mark.asyncio
async def test_issue_store_summary_cache_and_adhoc_issue():
    store = IssueStore()
    summary = NamespaceSummaryResponse(namespace='ns', minutes=5, headline='All good')
    store.cache_summary(summary)
    assert store.get_cached_summary('ns') == summary

    issue = Issue(id='adhoc', title='Manual issue', service='svc')
    await store.add_adhoc(issue)
    assert await store.get('adhoc') == issue


def test_issue_store_other_caches():
    store = IssueStore()
    analysis = AnalyzeResponse(issue_id='i1', summary='done')
    match = CodeMatchResponse(issue_id='i1', located=True)

    store.cache_search('sig', {'ok': True})
    store.cache_analysis(analysis)
    store.cache_context('i1', {'repo': 'svc'})
    store.cache_code_match(match)

    assert store.get_cached_search('sig') == {'ok': True}
    assert store.get_cached_analysis('i1') == analysis
    assert store.get_cached_context('i1') == {'repo': 'svc'}
    assert store.get_cached_code_match('i1') == match
