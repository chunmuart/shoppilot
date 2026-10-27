"""模型管理 API 的身份、主机、版本与凭据边界；只用隔离库和固定探针。"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.agents.model_selection import ModelSelectionService
from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.model_registry import ModelRegistry
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
from app.presentation.model_workspace import register_model_routes


@pytest.fixture
async def workspace(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, llm_api_key='environment-secret-test', llm_model='fixture-model',
        llm_base_url='https://fixture.invalid/v1', context_size=128000, llm_enable_thinking=None,
        llm_fallback_model='', llm_fallback_context_size=None, model_master_key='', model_allowed_local_hosts=(),
        identity_mode='demo', model_admin_buyers=('alice',), cors_origins=['http://localhost:5174'])
    calls = []
    async def probe(profile, key, **kwargs):
        calls.append((profile['model_id'], profile['base_url']))
        return {'streaming': True, 'tool_calling': True, 'tool_result': True,
                'usage_available': True, 'context_verified': False}
    registry = ModelRegistry(settings, probe=probe)
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'sessions.db'))
    store = SqlFencedSessionStore(engine)
    service = ModelSelectionService(registry, SimpleNamespace(), None, store)
    container = SimpleNamespace(model_service=service, settings=settings)
    app = FastAPI()
    app.state.identity_policy = IdentityPolicy()
    app.state.session_store = store
    app.state.session_owner_binding = True
    register_model_routes(app, lambda: container)
    try:
        yield SimpleNamespace(app=app, settings=settings, registry=registry, store=store, calls=calls)
    finally:
        await engine.dispose()


def client(workspace, *, host='localhost', peer='127.0.0.1', headers=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=workspace.app, client=(peer, 12345)),
                            base_url='http://' + host, headers=headers)


def draft(**extra):
    return {'buyer_id': 'alice', 'model_id': 'fixture-custom', 'base_url': 'https://fixture.invalid/v1',
            'api_key': 'private-key-never-return', 'context_max': 64000, **extra}


async def create_profile(c, value=None):
    value = value or draft()
    validation = await c.post('/commerce/model-connections/validate', json=value)
    assert validation.status_code == 200, validation.text
    saved = await c.post('/commerce/model-connections', json={**value,
        'validation_ticket': validation.json()['validation_ticket'], 'config_hash': validation.json()['config_hash']})
    assert saved.status_code == 200, saved.text
    return saved.json()


async def test_session_ownership_selection_revision_and_default_revision(workspace):
    async with client(workspace) as c:
        path = '/commerce/models?buyer_id=alice&session_id=owned'
        initial = await c.get(path)
        assert initial.status_code == 200 and initial.json()['management_allowed'] is True
        assert initial.json()['default_revision'] == 0
        selection = {'buyer_id': 'alice', 'profile_id': 'current-gateway', 'profile_revision': 1,
                     'window_id': 'budget-64000', 'thinking': 'default', 'expected_revision': 0}
        chosen = await c.patch('/commerce/sessions/owned/model', json=selection)
        assert chosen.status_code == 200
        assert chosen.json()['selection']['context_size'] == 64000
        assert (await c.patch('/commerce/sessions/owned/model', json=selection)).status_code == 409
        assert (await c.get('/commerce/models?buyer_id=bob&session_id=owned')).status_code == 403
        assert (await c.patch('/commerce/sessions/owned/model', json={**selection, 'buyer_id': 'bob'})).status_code == 403
        value = {'buyer_id': 'alice', 'profile_id': 'current-gateway', 'window_id': 'budget-32000',
                 'thinking': 'default', 'expected_revision': 0}
        assert (await c.post('/commerce/models/default', json=value)).status_code == 200
        assert (await c.post('/commerce/models/default', json=value)).status_code == 409
        old = (await c.get(path)).json()
        assert old['selection']['context_size'] == 64000
        new = (await c.get('/commerce/models?buyer_id=alice&session_id=new')).json()
        assert new['selection']['context_size'] == 32000
        assert new['default_revision'] == 1
        other = (await c.get('/commerce/models?buyer_id=bob&session_id=other')).json()
        assert other['default_revision'] == 0


@pytest.mark.parametrize('host,peer,origin,allowed', [
    ('localhost', '127.0.0.1', 'http://localhost:5174', True),
    ('127.0.0.1:8017', '127.0.0.1', 'http://127.0.0.1:8017', True),
    ('evil.invalid', '127.0.0.1', 'http://evil.invalid', False),
    ('evil.invalid', '127.0.0.1', None, False),
    ('localhost', '127.0.0.1', 'https://evil.invalid', False),
    ('localhost', '198.51.100.5', 'http://localhost:5174', False),
])
async def test_demo_management_requires_local_host_peer_and_trusted_origin(workspace, host, peer, origin, allowed):
    headers = {'Origin': origin} if origin else {}
    async with client(workspace, host=host, peer=peer, headers=headers) as c:
        response = await c.post('/commerce/model-connections/validate', json=draft())
        assert response.status_code == (200 if allowed else 403)
    assert bool(workspace.calls) == allowed


async def test_hmac_admin_needs_verified_identity_and_configured_permission(workspace):
    policy = IdentityPolicy(mode='hmac', secret='model-workspace-test-signing-secret-long-enough')
    workspace.app.state.identity_policy = policy
    workspace.settings.identity_mode = 'hmac'
    async with client(workspace, host='api.example', peer='198.51.100.5') as c:
        assert (await c.post('/commerce/model-connections/validate', json=draft())).status_code == 401
        alice = {'Authorization': 'Bearer ' + policy.issue('alice')}
        bob = {'Authorization': 'Bearer ' + policy.issue('bob')}
        assert (await c.post('/commerce/model-connections/validate', json=draft(), headers=bob)).status_code == 403
        assert (await c.post('/commerce/model-connections/validate', json=draft(buyer_id='bob'), headers=bob)).status_code == 403
        assert (await c.post('/commerce/model-connections/validate', json=draft(), headers={**alice, 'Origin': 'https://evil.invalid'})).status_code == 403
        assert (await c.post('/commerce/model-connections/validate', json=draft(), headers=alice)).status_code == 200
        assert (await c.get('/commerce/models?buyer_id=alice&session_id=a', headers=alice)).status_code == 200
        assert (await c.get('/commerce/models?buyer_id=bob&session_id=a', headers=bob)).status_code == 403


async def test_validate_save_edit_and_catalog_never_return_secrets(workspace):
    async with client(workspace) as c:
        profile = await create_profile(c)
        assert profile['key_configured'] and not profile['builtin']
        assert 'api_key' not in profile and 'secret_ref' not in profile
        path = '/commerce/models?buyer_id=alice&session_id=session'
        response = await c.get(path)
        assert 'private-key-never-return' not in response.text
        assert 'environment-secret-test' not in response.text
        assert len([x for x in response.json()['models'] if not x['builtin']]) == 1
        other = (await c.get('/commerce/models?buyer_id=bob&session_id=other')).json()
        assert not [x for x in other['models'] if not x['builtin']]
        # 空密钥编辑标签会复用旧密钥，但响应与新版本仍不回显。
        edited = await create_profile(c, draft(profile_id=profile['profile_id'], expected_revision=1, api_key='', label='更新名称'))
        assert edited['profile_revision'] == 2
        assert 'private-key-never-return' not in str(edited)
        assert (await c.post('/commerce/model-connections/validate', json=draft(
            profile_id=profile['profile_id'], expected_revision=1, api_key=''))).status_code == 409
        assert (await c.post('/commerce/model-connections/validate', json=draft(
            buyer_id='bob', profile_id=profile['profile_id'], expected_revision=2, api_key=''))).status_code == 422
        assert (await c.post('/commerce/model-connections/validate', json=draft(
            profile_id=profile['profile_id'], expected_revision=2, api_key='', base_url='https://evil.invalid/v1'))).status_code == 422


@pytest.mark.parametrize('extra', [
    {'secret_ref': 'env:llm'}, {'profile_id': []}, {'capabilities': {'context_max': '128000'}},
    {'reasoning': {'kind': [], 'options': ['default'], 'default': 'default'}},
    {'api_key': ['private-key-never-return']}, {'counter': {'kind': 'local_chat_template', 'directory': '/private'}},
])
async def test_custom_invalid_or_unknown_fields_rejected_without_probe_or_secret_echo(workspace, extra):
    async with client(workspace) as c:
        response = await c.post('/commerce/model-connections/validate', json=draft(**extra))
        assert response.status_code == 422
        assert 'private-key-never-return' not in response.text
        assert not workspace.calls


async def test_preferences_reject_unknown_fields_bad_types_and_stale_profile(workspace):
    async with client(workspace) as c:
        path = '/commerce/models/current-gateway/preferences'
        for extra in [{'script': 'anything'}, {'visible': 'false'}, {'expected_revision': '1'}]:
            assert (await c.patch(path, json={'buyer_id': 'alice', **extra})).status_code == 422
        assert (await c.patch(path, json={'buyer_id': 'alice', 'visible': False, 'expected_revision': 10})).status_code == 409
        assert (await c.patch(path, json={'buyer_id': 'alice', 'visible': False, 'expected_revision': 1})).status_code == 200
        assert (await c.post('/commerce/models/default', json={'buyer_id': 'alice', 'profile_id': 'current-gateway',
            'expected_revision': 0, 'unknown': 'field'})).status_code == 422


async def test_http_preset_and_credential_reference_use_existing_gateway_without_key_echo(workspace):
    async with client(workspace) as c:
        catalog = (await c.get('/commerce/models?buyer_id=alice&session_id=presets')).json()
        preset = catalog['presets'][0]
        current = next(item for item in catalog['models'] if item['profile_id'] == 'current-gateway')
        value = {key: preset[key] for key in ('preset_id', 'model_id', 'label', 'protocol', 'capabilities',
            'window_options', 'default_window', 'reasoning', 'counter')}
        value.update(buyer_id='alice', provider=current['provider'], base_url=current['base_url'], api_key='',
                     credential_profile_id=current['profile_id'], credential_profile_revision=current['profile_revision'])
        created = await create_profile(c, value)
        assert created['preset_id'] == preset['preset_id']
        assert created['key_configured'] and created['capabilities']['max_output'] == 8192
        assert 'environment-secret-test' not in str(created)
        assert created['official_limits']['max_output'] == 131072
        assert created['validation']['model_identity_matches'] is None
        assert (await c.post('/commerce/model-connections/validate', json={**value, 'credential_profile_revision': '1'})).status_code == 422
        assert (await c.post('/commerce/model-connections/validate', json={**value, 'base_url': 'https://other.example/v1'})).status_code == 422
        assert (await c.get('/commerce/models?buyer_id=alice&session_id=presets')).json()['selection']['profile_id'] == 'current-gateway'
