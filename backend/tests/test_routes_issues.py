from types import SimpleNamespace

from app.routes import issues


def test_get_issues_returns_store_data(client, dummy_store, sample_issue):
    response = client.get('/api/issues')
    assert response.status_code == 200
    assert response.json()[0]['id'] == sample_issue.id


def test_post_namespace_switches_runtime_and_ingests_entries(client, monkeypatch, dummy_store):
    entry = SimpleNamespace(timestamp=1.0, line='ERROR hello', labels={'app': 'svc'})

    async def fake_fetch_recent_errors():
        return [entry]

    monkeypatch.setattr('app.source.fetch_recent_errors', fake_fetch_recent_errors)
    response = client.post('/api/namespace', json={'namespace': 'iom-preprod'})

    assert response.status_code == 200
    assert response.json()['namespace'] == 'iom-preprod'
    assert dummy_store.cleared is True
    assert dummy_store.ingested == [entry]
    assert dummy_store.last_error is None


def test_get_namespaces_uses_mock_runtime_when_mock_enabled(client, monkeypatch):
    monkeypatch.setattr(issues.settings, 'use_mock', True)
    response = client.get('/api/namespaces')
    assert response.status_code == 200
    assert response.json() == ['telikos-dev']


def test_get_namespaces_uses_loki_client_when_configured(client, monkeypatch):
    monkeypatch.setattr(issues.settings, 'use_mock', False)
    monkeypatch.setattr(issues.settings, 'log_source', 'grafana')

    class FakeLokiClient:
        async def list_namespaces(self):
            return ['iom-preprod', 'telikos-preprod']

    monkeypatch.setattr('app.loki.loki_client', FakeLokiClient())
    response = client.get('/api/namespaces')
    assert response.status_code == 200
    assert response.json() == ['iom-preprod', 'telikos-preprod']


def test_get_search_calls_source_search(client, monkeypatch, dummy_store):
    async def fake_search_key(key, minutes):
        assert key == 'booking-123'
        assert minutes == 60
        return {
            'key': key,
            'namespace': 'telikos-dev',
            'namespaces': ['telikos-dev'],
            'minutes': minutes,
            'total_matches': 1,
            'problem_count': 1,
            'services': [
                {
                    'service': 'orders',
                    'namespace': 'telikos-dev',
                    'total': 1,
                    'problem_count': 1,
                    'problems': [
                        {'ts': '1', 'namespace': 'telikos-dev', 'service': 'orders', 'pod': 'orders-1', 'level': 'error', 'message': 'boom', 'trace_id': 'abc12345'}
                    ],
                    'trace_ids': ['abc12345'],
                }
            ],
            'trace_ids': ['abc12345'],
            'trace_issues': [],
        }

    monkeypatch.setattr(issues, 'search_key', fake_search_key)
    response = client.get('/api/search', params={'key': 'booking-123', 'minutes': 60})

    assert response.status_code == 200
    assert response.json()['problem_count'] == 1
    assert 'booking-123|60' in dummy_store.search_cache


def test_get_summary_uses_llm_and_cache(client, monkeypatch, dummy_store):
    monkeypatch.setattr(issues.settings, 'lookback_seconds', 300)

    class FakeLLMClient:
        async def summarize_namespace(self, namespace, minutes, issues_list):
            assert namespace == 'telikos-dev'
            assert minutes == 5
            assert len(issues_list) == 1
            return {
                'overall_health': 'critical',
                'headline': 'Orders are failing',
                'issues': [{'service': 'orders', 'level': 'error', 'count': 2, 'text': 'Database timeout'}],
                'top_concern': 'Database timeout',
            }

    monkeypatch.setattr(issues, 'llm_client', FakeLLMClient())
    response = client.get('/api/summary', params={'refresh': 'true'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['overall_health'] == 'critical'
    assert payload['issues'][0]['service'] == 'orders'
    assert dummy_store.cached_summary.top_concern == 'Database timeout'


def test_unauthenticated_issue_requests_return_401(user_unauthorized_client):
    response = user_unauthorized_client.get('/api/issues')
    assert response.status_code == 401


def test_admin_routes_require_admin_role(admin_forbidden_client):
    response = admin_forbidden_client.get('/api/analytics')
    assert response.status_code == 403
