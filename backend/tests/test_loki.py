import httpx
import pytest

from app import loki
from app.loki import LokiClient, _cluster_selector, _error_query


class DummyResponse:
    def __init__(self, payload, status_code=200, text=''):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError('bad response', request=None, response=None)


@pytest.fixture(autouse=True)
def restore_settings():
    original_clusters = loki.settings.k8s_clusters
    original_namespace = loki.settings.k8s_namespace
    original_source = loki.settings.log_source
    yield
    loki.settings.k8s_clusters = original_clusters
    loki.settings.k8s_namespace = original_namespace
    loki.settings.log_source = original_source


def test_cluster_selector_supports_single_and_multiple_clusters():
    loki.settings.k8s_clusters = 'cluster-a'
    assert _cluster_selector() == 'k8s_cluster="cluster-a"'

    loki.settings.k8s_clusters = 'cluster-a,cluster-b'
    assert _cluster_selector() == 'k8s_cluster=~"cluster-a|cluster-b"'


def test_error_query_contains_namespace_selector_and_level_filters():
    loki.settings.k8s_clusters = 'cluster-a,cluster-b'
    query = _error_query('telikos-dev')
    assert 'namespace="telikos-dev"' in query
    assert 'k8s_cluster=~"cluster-a|cluster-b"' in query
    assert 'level!~"(?i)^(debug|info|information|trace|verbose)$"' in query
    assert '\\"level\\":\\"DEBUG\\"' in query
    assert '!= "level=INFO"' in query


@pytest.mark.asyncio
async def test_search_key_batches_namespaces_in_groups_of_ten(monkeypatch):
    client = LokiClient()
    namespaces = [f'ns-{i}' for i in range(82)]
    calls = []

    def fake_endpoint(self):
        return 'http://example/query', {}

    async def fake_get(self, _client, url, headers, **kwargs):
        query = kwargs['params']['query']
        ns_fragment = query.split('namespace=~"', 1)[1].split('"', 1)[0]
        batch = [ns for ns in ns_fragment.split('|') if ns]
        calls.append(batch)
        return DummyResponse({'data': {'result': []}})

    monkeypatch.setattr(LokiClient, '_endpoint', fake_endpoint)
    monkeypatch.setattr(LokiClient, '_get', fake_get)

    result = await client.search_key('booking-123', namespaces, minutes=30)

    assert result['total_matches'] == 0
    assert len(calls) == 9
    assert max(len(batch) for batch in calls) <= 10
    assert calls[0] == namespaces[:10]
    assert calls[-1] == namespaces[80:]


@pytest.mark.asyncio
async def test_list_namespaces_filters_system_namespaces(monkeypatch):
    client = LokiClient()
    loki.settings.k8s_clusters = 'cluster-a,cluster-b'
    loki.settings.k8s_namespace = 'fallback-ns'

    def fake_labels_endpoint(self, label):
        assert label == 'namespace'
        return 'http://example/loki/api/v1/label/namespace/values', {}

    async def fake_get(self, _client, url, headers, **kwargs):
        assert kwargs['params']['query'] == '{k8s_cluster=~"cluster-a|cluster-b"}'
        return DummyResponse({'data': ['kube-system', 'falcon-agent', 'telikos-prod', 'iom-preprod']})

    monkeypatch.setattr(LokiClient, '_labels_endpoint', fake_labels_endpoint)
    monkeypatch.setattr(LokiClient, '_get', fake_get)

    assert await client.list_namespaces() == ['iom-preprod', 'telikos-prod']
