import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import token_refresh


@pytest.fixture(autouse=True)
def restore_env(monkeypatch):
    monkeypatch.delenv('FROM_ENV', raising=False)
    monkeypatch.delenv('GRAFANA_TOKEN', raising=False)
    token_refresh._live_token = ''
    yield
    token_refresh._live_token = ''


def test_read_env_var_falls_back_to_os_environ(monkeypatch):
    monkeypatch.setattr(token_refresh, '_ENV_FILE', Path('/path/does/not/exist/.env'))
    monkeypatch.setenv('FROM_ENV', 'expected-value')
    assert token_refresh._read_env_var('FROM_ENV') == 'expected-value'


def test_read_env_var_supports_legacy_tenant_name(monkeypatch):
    monkeypatch.setattr(token_refresh, '_ENV_FILE', Path('/path/does/not/exist/.env'))
    monkeypatch.setenv('ARM_TENENT_ID', 'legacy-tenant-id')
    assert token_refresh._read_env_var('ARM_TENANT_ID') == 'legacy-tenant-id'


def test_read_env_secret_aliases_supports_azure_names(monkeypatch):
    monkeypatch.setattr(token_refresh, '_ENV_FILE', Path('/path/does/not/exist/.env'))
    monkeypatch.setenv('AZURE_CLIENT_ID', 'azure-client-id')
    assert token_refresh._read_env_secret_aliases(('ARM_CLIENT_ID', 'AZURE_CLIENT_ID')) == 'azure-client-id'


def test_update_env_token_updates_process_environment(monkeypatch):
    fake_env = Path('/path/does/not/exist/.env')
    monkeypatch.setattr(token_refresh, '_ENV_FILE', fake_env)
    token_refresh._update_env_token('fresh-token')
    assert os.environ['GRAFANA_TOKEN'] == 'fresh-token'


@pytest.mark.asyncio
async def test_refresh_once_runs_azure_flow_and_updates_live_token(monkeypatch):
    monkeypatch.setattr(token_refresh, '_read_env_var', lambda name: {
        'ARM_CLIENT_ID': 'client-id',
        'ARM_CLIENT_SECRET': 'secret',
        'ARM_TENANT_ID': 'tenant-id',
        'ARM_TENENT_ID': 'tenant-id',
        'AZURE_CLIENT_ID': '',
        'AZURE_CLIENT_SECRET': '',
        'AZURE_TENANT_ID': '',
    }[name])

    calls = []

    class FakeProc:
        def __init__(self, returncode=0, stdout=b''):
            self.returncode = returncode
            self._stdout = stdout

        async def wait(self):
            return None

        async def communicate(self):
            return self._stdout, b''

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        if args[1] == 'login':
            return FakeProc(returncode=0)
        return FakeProc(returncode=0, stdout=b'azure-bearer-token\n')

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers):
            assert headers['Authorization'] == 'Bearer azure-bearer-token'
            return SimpleNamespace(
                json=lambda: {'data': {'key': 'grafana-key'}},
                raise_for_status=lambda: None,
            )

    captured_tokens = []
    monkeypatch.setattr(token_refresh.asyncio, 'create_subprocess_exec', fake_create_subprocess_exec)
    monkeypatch.setattr(token_refresh.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(token_refresh, '_update_env_token', lambda token: captured_tokens.append(token))

    assert await token_refresh._refresh_once() is True
    assert calls[0][1] == 'login'
    assert calls[1][1:4] == ('account', 'get-access-token', '--query')
    assert token_refresh.get_live_token() == 'grafana-key'
    assert captured_tokens == ['grafana-key']
