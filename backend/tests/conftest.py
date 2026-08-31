import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import require_admin, require_user
from app.models import AnalyzeResponse, Issue, NamespaceSummaryResponse
from app.routes import analytics, issues, snow
from app.state import runtime


class DummyStore:
    def __init__(self):
        self.issues = []
        self.issue_map = {}
        self.cached_summary = None
        self.last_error = None
        self.last_poll_ts = 0.0
        self.cleared = False
        self.ingested = []
        self.search_cache = {}
        self.analysis_cache = {}

    async def list_issues(self):
        return self.issues

    async def get(self, issue_id):
        return self.issue_map.get(issue_id)

    async def clear(self):
        self.cleared = True
        self.issues = []
        self.issue_map = {}

    async def ingest(self, entries):
        self.ingested = list(entries)

    def cache_search(self, sig, result):
        self.search_cache[sig] = result

    def get_cached_search(self, sig):
        return self.search_cache.get(sig)

    def cache_summary(self, summary):
        self.cached_summary = summary

    def get_cached_summary(self, namespace):
        if self.cached_summary and self.cached_summary.namespace == namespace:
            return self.cached_summary
        return None

    def get_cached_analysis(self, issue_id):
        return self.analysis_cache.get(issue_id)

    def cache_analysis(self, analysis):
        self.analysis_cache[analysis.issue_id] = analysis

    def get_cached_context(self, issue_id):
        return None

    def cache_context(self, issue_id, ctx):
        pass

    def get_cached_code_match(self, issue_id):
        return None

    def cache_code_match(self, match):
        pass


@pytest.fixture
def sample_issue():
    return Issue(
        id='issue-1',
        title='Database error',
        service='orders',
        namespace='telikos-dev',
        count=2,
        first_seen=10.0,
        last_seen=20.0,
        sample_line='ERROR database timeout',
        samples=['ERROR database timeout'],
        labels={'namespace': 'telikos-dev', 'app': 'orders'},
    )


@pytest.fixture
def dummy_store(sample_issue):
    store = DummyStore()
    store.issues = [sample_issue]
    store.issue_map = {sample_issue.id: sample_issue}
    store.cached_summary = NamespaceSummaryResponse(
        namespace='telikos-dev',
        minutes=5,
        total_issues=1,
        affected_services=1,
        overall_health='degraded',
        headline='Cached summary',
        top_concern='Database timeout',
    )
    store.analysis_cache[sample_issue.id] = AnalyzeResponse(issue_id=sample_issue.id, summary='cached')
    return store


@pytest.fixture
def app_with_auth_overrides(monkeypatch, dummy_store):
    async def allow_user():
        return {'sub': 'tester'}

    async def allow_admin():
        return {'sub': 'admin', 'roles': ['TFR_Admin']}

    monkeypatch.setattr(issues, 'store', dummy_store)
    app = FastAPI()
    app.include_router(issues.router, dependencies=[Depends(require_user)])
    app.include_router(analytics.router, dependencies=[Depends(require_user)])
    app.include_router(snow.router, dependencies=[Depends(require_user)])
    app.dependency_overrides[require_user] = allow_user
    app.dependency_overrides[require_admin] = allow_admin
    runtime.namespace = 'telikos-dev'
    return app


@pytest.fixture
def client(app_with_auth_overrides):
    with TestClient(app_with_auth_overrides) as test_client:
        yield test_client


@pytest.fixture
def auth_failure_app(monkeypatch, dummy_store):
    monkeypatch.setattr(issues, 'store', dummy_store)
    app = FastAPI()
    app.include_router(issues.router, dependencies=[Depends(require_user)])
    app.include_router(analytics.router, dependencies=[Depends(require_user)])
    app.include_router(snow.router, dependencies=[Depends(require_user)])
    return app


@pytest.fixture
def user_unauthorized_client(auth_failure_app):
    async def deny_user():
        raise HTTPException(status_code=401, detail='Authorization header required')

    auth_failure_app.dependency_overrides[require_user] = deny_user
    with TestClient(auth_failure_app) as test_client:
        yield test_client


@pytest.fixture
def admin_forbidden_client(auth_failure_app):
    async def allow_user():
        return {'sub': 'tester'}

    async def deny_admin():
        raise HTTPException(status_code=403, detail='TFR_Admin role required')

    auth_failure_app.dependency_overrides[require_user] = allow_user
    auth_failure_app.dependency_overrides[require_admin] = deny_admin
    with TestClient(auth_failure_app) as test_client:
        yield test_client
