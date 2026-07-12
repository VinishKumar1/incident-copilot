from app.routes import analytics


def test_get_analytics_returns_stats(client, monkeypatch):
    async def fake_get_stats(since_hours):
        assert since_hours == 48
        return {'total_events': 10, 'since_hours': since_hours}

    monkeypatch.setattr(analytics, 'get_stats', fake_get_stats)
    response = client.get('/api/analytics', params={'hours': 48})
    assert response.status_code == 200
    assert response.json() == {'total_events': 10, 'since_hours': 48}


def test_get_vibe_usage_returns_trimmed_payload(client, monkeypatch):
    monkeypatch.setattr(analytics.settings, 'openai_base_url', 'https://proxy.example')
    monkeypatch.setattr(analytics.settings, 'openai_api_key', 'secret')

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                'info': {
                    'key_alias': 'team-key',
                    'spend': 12.34567,
                    'max_budget': 100,
                    'budget_duration': 'monthly',
                    'budget_reset_at': '2026-07-31',
                    'expires': 'never',
                    'models': ['gpt-4o'],
                    'last_active': '2026-07-12T10:00:00Z',
                }
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            assert url == 'https://proxy.example/key/info'
            assert headers['Authorization'] == 'Bearer secret'
            return FakeResponse()

    monkeypatch.setattr(analytics.httpx, 'AsyncClient', FakeAsyncClient)
    response = client.get('/api/analytics/vibe-usage')
    assert response.status_code == 200
    assert response.json()['spend'] == 12.3457
