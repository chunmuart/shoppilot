"""模型目录与连接校验用隔离数据库及固定探针，避免访问真实凭据。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.infrastructure.model_connection import (
    ModelConfigurationError, ValidatedModelTransport, probe_connection, validate_address,
)
from app.infrastructure.model_registry import ModelRegistry, ModelRevisionConflict


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, llm_api_key="initial-secret-do-not-print", llm_model="test-main",
        llm_base_url="https://example.com/v1", context_size=128000, llm_enable_thinking=None,
        llm_fallback_model="test-fallback", llm_fallback_context_size=32000, model_master_key="",
        model_allowed_local_hosts=())


async def good_probe(profile, api_key, **kwargs):
    return {"streaming": True, "tool_calling": True, "tool_result": True,
            "usage_available": True, "context_verified": False}


def draft(**overrides):
    return {"model_id": "custom-model", "base_url": "https://example.com/v1", "api_key": "a-different-secret",
            "context_max": 64000, **overrides}


async def save(registry, buyer="alice", value=None, **kwargs):
    value = value or draft()
    validated = await registry.validate_connection(value, buyer, **kwargs)
    return await registry.save_connection(value, buyer, validation_ticket=validated["validation_ticket"],
        config_hash=validated["config_hash"], **kwargs)


@pytest.mark.asyncio
async def test_settings_migration_is_persistent_and_fallback_independent(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    await registry.initialize()
    snapshot = await registry.resolve(buyer_id="alice")
    assert snapshot["context_size"] == 128000
    assert snapshot["fallback"]["context_size"] == 32000
    assert snapshot["reasoning_parameters"] == {}
    assert "api_key" not in json.dumps(snapshot)
    assert registry.runtime(snapshot)["api_key"] == settings.llm_api_key
    assert settings.llm_api_key not in repr(registry.runtime(snapshot))
    settings.context_size = 256000
    second = ModelRegistry(settings)
    assert (await second.resolve())["context_size"] == 256000
    assert (await second.resolve(revision=1))["context_size"] == 128000
    assert snapshot["context_size"] == 128000
    assert next(item for item in (await second.catalog("alice"))["models"] if item["profile_id"] == "current-gateway")["key_configured"]


@pytest.mark.asyncio
async def test_modelcard_windows_reasoning_and_snapshot_tamper(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    value = draft(reasoning={"kind": "boolean", "options": ["default", "off", "on"], "default": "on"})
    profile = await save(registry, value=value)
    snap = await registry.resolve(profile["profile_id"], thinking="off", buyer_id="alice")
    assert snap["reasoning_parameters"] == {"extra_body": {"enable_thinking": False}}
    with pytest.raises(ModelConfigurationError, match="窗口"):
        await registry.resolve(profile["profile_id"], window_id="not-existing", buyer_id="alice")
    with pytest.raises(ModelConfigurationError, match="思考"):
        await registry.resolve(profile["profile_id"], thinking="high", buyer_id="alice")
    snap["context_size"] = 4000000
    with pytest.raises(ModelConfigurationError, match="校验"):
        registry.runtime(snap)


@pytest.mark.asyncio
async def test_secret_encryption_ownership_and_restart(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    profile = await save(registry)
    assert profile["key_configured"]
    assert "secret_ref" not in profile
    assert "a-different-secret" not in registry.path.read_bytes().decode(errors="ignore")
    assert os.stat(settings.data_dir / ".secrets/model_registry.key").st_mode & 0o777 == 0o600
    with pytest.raises(ModelConfigurationError):
        await registry.resolve(profile["profile_id"], buyer_id="bob")
    assert all(x["profile_id"] != profile["profile_id"] for x in (await registry.catalog("bob"))["models"])
    restarted = ModelRegistry(settings, probe=good_probe)
    snapshot = await restarted.resolve(profile["profile_id"], buyer_id="alice")
    assert restarted.runtime(snapshot)["api_key"] == "a-different-secret"


@pytest.mark.asyncio
async def test_ticket_is_bound_to_buyer_fields_secret_and_single_use(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    value = draft()
    validated = await registry.validate_connection(value, "alice")
    kwargs = {key: validated[key] for key in ("validation_ticket", "config_hash")}
    for changed, buyer in [(value, "bob"), ({**value, "context_max": 128000}, "alice"), ({**value, "api_key": "new"}, "alice")]:
        with pytest.raises(ModelConfigurationError):
            await registry.save_connection(changed, buyer, **kwargs)
    await registry.save_connection(value, "alice", **kwargs)
    with pytest.raises(ModelConfigurationError):
        await registry.save_connection(value, "alice", **kwargs)


@pytest.mark.asyncio
async def test_revision_cas_keeps_frozen_secret_and_empty_key_retains(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    first = await save(registry)
    frozen = await registry.resolve(first["profile_id"], buyer_id="alice")
    second = await save(registry, value=draft(api_key="", label="第二版"),
        profile_id=first["profile_id"], expected_revision=1)
    assert second["profile_revision"] == 2
    assert registry.runtime(await registry.resolve(first["profile_id"], buyer_id="alice"))["api_key"] == "a-different-secret"
    with pytest.raises(ModelRevisionConflict):
        await save(registry, profile_id=first["profile_id"], expected_revision=1)
    await save(registry, value=draft(api_key="rotated-key"), profile_id=first["profile_id"], expected_revision=2)
    assert registry.runtime(frozen)["api_key"] == "a-different-secret"
    assert (await registry.resolve(first["profile_id"], buyer_id="alice", revision=1))["fingerprint"] == frozen["fingerprint"]


@pytest.mark.asyncio
async def test_preferences_hide_without_changing_existing_profile(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    pref = await registry.set_preferences("current-gateway", "alice", visible=False, default_window="budget-64000")
    assert pref["visible"] is False
    assert pref["preferred_window"] == "budget-64000"
    assert (await registry.resolve(buyer_id="alice"))["context_size"] == 64000
    assert (await registry.resolve(buyer_id="alice", revision=1))["context_size"] == 128000
    migrated = await registry.resolve_imported_default("alice")
    assert migrated["context_size"] == 128000
    assert migrated["source"]["binding"] == "legacy_migration_inferred"
    assert registry.runtime(migrated)["model_id"] == "test-main"
    with pytest.raises(ModelRevisionConflict):
        await registry.set_preferences("current-gateway", "alice", visible=True, expected_revision=99)
    assert next(x for x in (await registry.catalog("bob"))["models"] if x["profile_id"] == "current-gateway")["visible"]


@pytest.mark.parametrize("extra", [
    {"protocol": "anthropic"}, {"context_max": 8192}, {"max_output_tokens": 64000},
    {"counter": {"kind": "fake_tokenizer"}},
    {"window_options": [{"id": "bad", "tokens": 1000000}]},
    {"reasoning": {"kind": "boolean", "options": ["high"], "default": "high"}},
])
@pytest.mark.asyncio
async def test_invalid_capabilities_fail_before_probe(settings, extra):
    async def no_probe(*args, **kwargs):
        pytest.fail("invalid configuration must not make a network request")
    registry = ModelRegistry(settings, probe=no_probe)
    with pytest.raises(ModelConfigurationError):
        await registry.validate_connection(draft(**extra), "alice")


@pytest.mark.parametrize("url", ["https://127.0.0.1/v1", "http://example.com", "https://user:pass@example.com",
                                  "https://169.254.169.254/latest", "https://100.100.100.200", "https://[::ffff:169.254.169.254]", "https://example.com?key=a"])
@pytest.mark.asyncio
async def test_ssrf_and_unsafe_address_rejected(url):
    with pytest.raises(ModelConfigurationError):
        await validate_address(url)


@pytest.mark.asyncio
async def test_allowlisted_local_probe_checks_tools_and_text_without_real_data():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["Host"] == "127.0.0.1:8000"
        if len(requests) == 1:
            content = {"tool_calls": [{"index": 0, "id": "call-test", "type": "function",
                                      "function": {"name": "connection_probe", "arguments": '{"value":"ok"}'}}]}
        else:
            assert body["messages"][2]["role"] == "tool"
            content = {"content": "ok"}
        events = [{"choices": [{"delta": content, "finish_reason": None}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10}}]
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n")
    result = await probe_connection({"model_id": "test", "base_url": "http://127.0.0.1:8000/v1"}, "test-key",
        allowed_local_hosts=("127.0.0.1",), transport=httpx.MockTransport(handler))
    assert result["usage_available"] and result["tool_result"]
    assert result["context_verified"] is False
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_probe_does_not_follow_redirect_and_hides_body():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254"}, text="secret-response")
    with pytest.raises(ModelConfigurationError) as error:
        await probe_connection({"model_id": "test", "base_url": "http://127.0.0.1/v1"}, "test-key",
            allowed_local_hosts=("127.0.0.1",), transport=httpx.MockTransport(handler))
    assert "302" in str(error.value)
    assert "secret-response" not in str(error.value)


@pytest.mark.asyncio
async def test_runtime_transport_checks_destination_again():
    visited = []
    def handler(request):
        visited.append(str(request.url))
        assert request.extensions["sni_hostname"] == "127.0.0.1"
        return httpx.Response(200, json={"ok": True})
    async with httpx.AsyncClient(transport=ValidatedModelTransport(allowed_local_hosts=("127.0.0.1",),
            transport=httpx.MockTransport(handler))) as client:
        assert (await client.post("http://127.0.0.1/v1/chat/completions")).status_code == 200
        with pytest.raises(ModelConfigurationError):
            await client.post("http://169.254.169.254/latest")
    assert len(visited) == 1


@pytest.mark.asyncio
async def test_malformed_stream_does_not_admit_configuration():
    def handler(request):
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n')
    with pytest.raises(ModelConfigurationError, match="结束"):
        await probe_connection({"model_id": "test", "base_url": "http://127.0.0.1/v1"}, "test-key",
            allowed_local_hosts=("127.0.0.1",), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_deployment_update_preserves_historical_fallback_version(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    old = await registry.resolve()
    settings.llm_fallback_model = "replacement-fallback"
    settings.llm_fallback_context_size = 64000
    restarted = ModelRegistry(settings, probe=good_probe)
    current = await restarted.resolve()
    historical = await restarted.resolve(revision=1)
    assert current["fallback"]["model_id"] == "replacement-fallback"
    assert historical["fallback"]["model_id"] == "test-fallback"
    assert historical["fingerprint"] == old["fingerprint"]



@pytest.mark.asyncio
async def test_new_session_default_is_buyer_scoped_independent_cas(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    profile = await save(registry)
    original = await registry.resolve(buyer_id="alice")
    assert (await registry.catalog("alice"))["default_revision"] == 0
    result = await registry.set_default("alice", profile["profile_id"], "budget-64000", "default", 0)
    assert result["default_revision"] == 1
    assert (await registry.catalog("alice"))["default_selection"]["profile_id"] == profile["profile_id"]
    assert (await registry.catalog("bob"))["default_selection"]["profile_id"] == "current-gateway"
    assert original["profile_id"] == "current-gateway"
    assert (await registry.resolve_default("alice"))["profile_id"] == profile["profile_id"]
    assert (await registry.resolve_default("bob"))["profile_id"] == "current-gateway"
    with pytest.raises(ModelRevisionConflict):
        await registry.set_default("alice", "current-gateway", expected_revision=0)
    # 模型版本仍是1，买家的默认版本可以独立递增为2、3。
    assert (await registry.set_default("alice", "current-gateway", expected_revision=1))["default_revision"] == 2
    assert (await registry.set_default("alice", profile["profile_id"], expected_revision=2))["default_revision"] == 3
    with pytest.raises(ModelConfigurationError):
        await registry.set_default("bob", profile["profile_id"], expected_revision=0)
    restarted = ModelRegistry(settings, probe=good_probe)
    assert (await restarted.catalog("alice"))["default_revision"] == 3
    assert (await restarted.catalog("alice"))["default_profile_id"] == profile["profile_id"]



@pytest.mark.asyncio
async def test_long_lived_worker_refreshes_new_secret_by_owned_revision(settings):
    api = ModelRegistry(settings, probe=good_probe)
    worker = ModelRegistry(settings, probe=good_probe)
    await worker.initialize()
    profile = await save(api)
    assert next(x for x in (await worker.catalog("alice"))["models"] if x["profile_id"] == profile["profile_id"])["key_configured"]
    resolved = await worker.resolve(profile["profile_id"], buyer_id="alice")
    assert worker.runtime(resolved)["api_key"] == "a-different-secret"
    await save(api, value=draft(api_key="new-worker-key"), profile_id=profile["profile_id"], expected_revision=1)
    latest = await worker.resolve(profile["profile_id"], buyer_id="alice")
    old = await worker.resolve(profile["profile_id"], buyer_id="alice", revision=1)
    assert worker.runtime(latest)["api_key"] == "new-worker-key"
    assert worker.runtime(old)["api_key"] == "a-different-secret"



@pytest.mark.asyncio
async def test_old_secret_never_sent_to_replacement_provider_without_reentry(settings):
    calls = []
    async def observed_probe(profile, api_key, **kwargs):
        calls.append(profile["base_url"])
        return await good_probe(profile, api_key, **kwargs)
    registry = ModelRegistry(settings, probe=observed_probe)
    profile = await save(registry)
    for changed in ({"base_url": "https://another.example/v1"}, {"provider": "其他供应商"}):
        with pytest.raises(ModelConfigurationError, match="重新输入"):
            await registry.validate_connection(draft(api_key="", **changed), "alice",
                profile_id=profile["profile_id"], expected_revision=1)
    assert calls == ["https://example.com/v1"]



@pytest.mark.asyncio
async def test_builtin_credentials_are_versioned_and_old_run_does_not_borrow_new_key(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    old = await registry.resolve()
    old_key, old_base = settings.llm_api_key, settings.llm_base_url
    assert old["secret_ref"].startswith("builtin:")
    settings.llm_api_key = "new-provider-secret"
    settings.llm_base_url = "https://new.example/v1"
    restarted = ModelRegistry(settings, probe=good_probe)
    new = await restarted.resolve()
    assert new["profile_revision"] == old["profile_revision"] + 1
    assert restarted.runtime(new)["api_key"] == "new-provider-secret"
    restored = restarted.runtime(old)
    assert restored["api_key"] == old_key and restored["base_url"] == old_base
    assert old_key.encode() not in restarted.path.read_bytes()
    assert b"new-provider-secret" not in restarted.path.read_bytes()
    # 早期 env:llm 快照无法还原旧凭据时拒绝，而不是拼上新网关的密钥。
    from app.infrastructure.model_registry import _digest
    legacy = {**old, "secret_ref": "env:llm"}
    legacy.pop("fingerprint")
    legacy["fingerprint"] = _digest(legacy)
    with pytest.raises(ModelConfigurationError, match="网关已变更"):
        restarted.runtime(legacy)



@pytest.mark.parametrize("streamed", [True, False])
@pytest.mark.asyncio
async def test_upstream_errors_keep_safe_code_without_echoing_request_or_key(streamed):
    secret = "secret-upstream-key"
    payload = {"error": {"code": "rate_limit_exceeded", "message": "request denied with " + secret}}
    def handler(request):
        if streamed:
            return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)
    with pytest.raises(ModelConfigurationError) as error:
        await probe_connection({"model_id": "test", "base_url": "http://127.0.0.1/v1"}, secret,
            allowed_local_hosts=("127.0.0.1",), transport=httpx.MockTransport(handler))
    assert "rate_limit_exceeded" in str(error.value)
    assert secret not in str(error.value)
    assert "有效结束" not in str(error.value)



@pytest.mark.asyncio
async def test_thinking_probe_uses_auto_tool_choice_and_preserves_reasoning_protocol():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["enable_thinking"] is True
        if len(calls) == 1:
            assert body["tool_choice"] == "auto"
            delta = {"reasoning_content": "fixed probe reasoning", "tool_calls": [{"index": 0, "id": "probe-call",
                "function": {"name": "connection_probe", "arguments": '{"value":"ok"}'}}]}
        else:
            assert body["messages"][1]["reasoning_content"] == "fixed probe reasoning"
            delta = {"content": "ok"}
        event = {"choices": [{"delta": delta, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
    result = await probe_connection({"model_id": "test", "base_url": "http://127.0.0.1/v1",
        "probe_reasoning_parameters": {"extra_body": {"enable_thinking": True}}}, "test-key",
        allowed_local_hosts=("127.0.0.1",), transport=httpx.MockTransport(handler))
    assert result["tool_result"] and len(calls) == 2


@pytest.mark.asyncio
async def test_frozen_runtime_initializes_new_process_and_keeps_original_credentials(settings):
    api = ModelRegistry(settings, probe=good_probe)
    frozen = await api.resolve(buyer_id="alice")
    original_key = settings.llm_api_key
    settings.llm_api_key = "replacement-startup-key"
    settings.llm_base_url = "https://replacement.example/v1"
    restarted = ModelRegistry(settings, probe=good_probe)
    assert not restarted._ready
    runtime = await restarted.runtime_for_snapshot(frozen, buyer_id="alice")
    assert restarted._ready
    assert runtime["api_key"] == original_key
    assert runtime["fallback"]["api_key"] == original_key
    assert runtime["base_url"] == frozen["base_url"]


@pytest.mark.asyncio
async def test_frozen_runtime_loads_cross_worker_secret_and_checks_owner_and_version(settings):
    api, worker = ModelRegistry(settings, probe=good_probe), ModelRegistry(settings, probe=good_probe)
    await worker.initialize()
    profile = await save(api)
    frozen = await api.resolve(profile["profile_id"], buyer_id="alice")
    assert frozen["secret_ref"] not in worker._secrets
    with pytest.raises(ModelConfigurationError, match="不属于"):
        await worker.runtime_for_snapshot(frozen, buyer_id="bob")
    assert frozen["secret_ref"] not in worker._secrets
    runtime = await worker.runtime_for_snapshot(frozen, buyer_id="alice")
    assert runtime["api_key"] == "a-different-secret"
    assert runtime["context_size"] == 64000
    from app.infrastructure.model_registry import _digest
    forged = {**frozen, "base_url": "https://other.example/v1"}
    forged.pop("fingerprint")
    forged["fingerprint"] = _digest(forged)
    with pytest.raises(ModelConfigurationError, match="不一致"):
        await worker.runtime_for_snapshot(forged, buyer_id="alice")


def preset_draft(registry, **changes):
    preset = registry.presets()[0]
    value = {key: preset[key] for key in ('preset_id', 'model_id', 'label', 'protocol', 'capabilities',
        'window_options', 'default_window', 'reasoning', 'counter')}
    return {**value, 'provider': '当前网关', 'base_url': registry.settings.llm_base_url,
            'credential_profile_id': 'current-gateway', 'credential_profile_revision': 1, 'api_key': '', **changes}


@pytest.mark.asyncio
async def test_official_presets_are_reference_only_and_keep_runtime_output_reserve(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    before = await registry.resolve()
    catalog = await registry.catalog('alice')
    assert {item['model_id'] for item in catalog['presets']} == {
        'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus', 'qwen3.7-flash'}
    assert not any(item['model_id'].startswith('qwen') for item in catalog['models'] if not item.get('is_virtual'))
    for item in catalog['presets']:
        assert item['capabilities']['context_max'] == 1000000
        assert item['capabilities']['max_output'] == 8192
        assert item['official_limits']['max_output'] == 131072
        assert item['capabilities']['max_input'] == 983616
        assert [window['tokens'] for window in item['window_options']] == [128000, 200000, 400000, 1000000]
        assert item['source']['checked_at'] == '2026-10-08'
        assert item['source']['gateway_context_verified'] is False
    profile = await save(registry, value=preset_draft(registry))
    snapshot = await registry.resolve(profile['profile_id'], buyer_id='alice')
    assert snapshot['context_size'] == 200000 and snapshot['max_output_tokens'] == 8192
    assert snapshot['max_input_tokens'] == 983616 and snapshot['thinking'] == 'off'
    assert snapshot['quota_group'] == before['quota_group']
    assert registry.runtime(snapshot)['api_key'] == settings.llm_api_key
    assert (await registry.resolve())['fingerprint'] == before['fingerprint']
    assert (await registry.resolve_default('alice'))['profile_id'] == 'current-gateway'
    assert profile['source']['kind'] == 'official_reference'
    assert profile['validation']['model_identity_matches'] is None


@pytest.mark.asyncio
async def test_preset_does_not_mislabel_models_or_exceed_reference_limits(settings):
    async def no_probe(*args, **kwargs):
        pytest.fail('invalid preset must fail before sending a credential')
    registry = ModelRegistry(settings, probe=no_probe)
    for change in [
        {'model_id': 'another-model'}, {'preset_id': 'unknown'},
        {'capabilities': {'context_max': 2000000, 'max_output': 8192}},
        {'capabilities': {'context_max': 1000000, 'max_output': 150000}},
        {'capabilities': {'context_max': 1000000, 'max_output': 8192, 'max_input': 991808}},
    ]:
        with pytest.raises(ModelConfigurationError):
            await registry.validate_connection(preset_draft(registry, **change), 'alice')


@pytest.mark.asyncio
async def test_inherited_credential_is_versioned_scoped_and_address_locked(settings):
    calls = []
    async def observing(profile, key, **kwargs):
        calls.append((profile['model_id'], key))
        return await good_probe(profile, key, **kwargs)
    registry = ModelRegistry(settings, probe=observing)
    private = await save(registry)
    inherited = draft(model_id='inherited-model', api_key='', provider=private['provider'], credential_profile_id=private['profile_id'],
                      credential_profile_revision=private['profile_revision'])
    created = await save(registry, value=inherited)
    frozen = await registry.resolve(created['profile_id'], buyer_id='alice')
    assert registry.runtime(frozen)['api_key'] == 'a-different-secret'
    baseline = len(calls)
    for changed, buyer in [
        ({'base_url': 'https://other.example/v1'}, 'alice'), ({'provider': 'different'}, 'alice'),
        ({}, 'bob'), ({'credential_profile_revision': None}, 'alice'), ({'api_key': 'new-key'}, 'alice'),
    ]:
        with pytest.raises(ModelConfigurationError):
            await registry.validate_connection({**inherited, **changed}, buyer)
    assert len(calls) == baseline
    # 连接源更新不改变已经冻结的源版本。
    await save(registry, value=draft(api_key='rotated-source'), profile_id=private['profile_id'], expected_revision=1)
    new = await save(registry, value=inherited)
    assert registry.runtime(await registry.resolve(new['profile_id'], buyer_id='alice'))['api_key'] == 'a-different-secret'


@pytest.mark.asyncio
async def test_preset_rejects_gateway_identity_mismatch_but_does_not_invent_missing_identity(settings):
    async def wrong_model(profile, key, **kwargs):
        return {**await good_probe(profile, key), 'requested_model': profile['model_id'],
                'returned_models': ['other-model'], 'model_identity_matches': False}
    registry = ModelRegistry(settings, probe=wrong_model)
    with pytest.raises(ModelConfigurationError, match='标识'):
        await registry.validate_connection(preset_draft(registry), 'alice')
    # 手填自定义仍可校验，但明确报告别名映射，不能伪称一致。
    plain = await registry.validate_connection(draft(), 'alice')
    assert plain['validation']['model_identity_matches'] is False
    assert plain['validation']['returned_models'] == ['other-model']


@pytest.mark.asyncio
async def test_inherited_source_reference_is_bound_to_validation_ticket(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    value = preset_draft(registry)
    ticket = await registry.validate_connection(value, 'alice')
    changed = {**value, 'credential_profile_id': 'current-fallback'}
    with pytest.raises(ModelConfigurationError, match='凭证'):
        await registry.save_connection(changed, 'alice', validation_ticket=ticket['validation_ticket'], config_hash=ticket['config_hash'])



@pytest.mark.asyncio
async def test_preset_probe_aliases_only_come_from_server_catalog(settings):
    received = []
    async def probe(profile, key, **kwargs):
        received.append(profile["probe_model_aliases"])
        return {**await good_probe(profile, key), "model_identity_matches": True,
                "returned_models": ["qwen3.8-max-0902"]}
    registry = ModelRegistry(settings, probe=probe)
    await registry.validate_connection(preset_draft(registry, probe_model_aliases=["arbitrary-model"]), "alice")
    assert received and all(item == ["qwen3.8-max-0902", "qwen3.8-max-2026-09-02"] for item in received)


@pytest.mark.asyncio
async def test_edit_inherited_connection_without_source_keeps_frozen_origin_and_quota(settings):
    keys = []
    async def probe(profile, key, **kwargs):
        keys.append(key)
        return await good_probe(profile, key, **kwargs)
    registry = ModelRegistry(settings, probe=probe)
    original = await save(registry, value=preset_draft(registry))
    before = await registry.resolve(original['profile_id'], buyer_id='alice')
    plain_edit = preset_draft(registry, label='仅修改显示名称')
    plain_edit.pop('credential_profile_id')
    plain_edit.pop('credential_profile_revision')
    edited = await save(registry, value=plain_edit, profile_id=original['profile_id'], expected_revision=1)
    after = await registry.resolve(edited['profile_id'], buyer_id='alice')
    assert edited['credential_source'] == original['credential_source']
    assert after['quota_group'] == before['quota_group'] == 'current-gateway'
    assert registry.runtime(after)['api_key'] == settings.llm_api_key
    assert set(keys) == {settings.llm_api_key}
    assert (await registry.resolve(original['profile_id'], buyer_id='alice', revision=1))['fingerprint'] == before['fingerprint']


@pytest.mark.asyncio
async def test_inherited_edit_retains_original_source_version_after_source_rotation(settings):
    keys = []
    async def probe(profile, key, **kwargs):
        keys.append(key)
        return await good_probe(profile, key, **kwargs)
    registry = ModelRegistry(settings, probe=probe)
    source = await save(registry)
    inherited = draft(model_id='inherited-model', api_key='', provider=source['provider'], credential_profile_id=source['profile_id'],
        credential_profile_revision=source['profile_revision'])
    copied = await save(registry, value=inherited)
    before = await registry.resolve(copied['profile_id'], buyer_id='alice')
    await save(registry, value=draft(api_key='rotated-source-key'), profile_id=source['profile_id'], expected_revision=1)
    keys.clear()
    # 标签修改不会偷换成源连接的新凭据；仍验证冻结的源版本 1。
    edited = await save(registry, value=draft(model_id='inherited-model', api_key='', label='编辑副本'),
        profile_id=copied['profile_id'], expected_revision=1)
    assert edited['credential_source']['profile_revision'] == 1
    assert keys == ['a-different-secret']
    assert (await registry.resolve(copied['profile_id'], buyer_id='alice'))['quota_group'] == before['quota_group']


@pytest.mark.asyncio
async def test_inherited_edit_requires_new_key_for_new_endpoint_and_recomputes_on_rotation(settings):
    calls = []
    async def probe(profile, key, **kwargs):
        calls.append((profile['base_url'], key))
        return await good_probe(profile, key, **kwargs)
    registry = ModelRegistry(settings, probe=probe)
    original = await save(registry, value=preset_draft(registry))
    plain = preset_draft(registry)
    plain.pop('credential_profile_id')
    plain.pop('credential_profile_revision')
    count = len(calls)
    for change in ({'base_url': 'https://other.example/v1'}, {'provider': '另一供应商'}):
        with pytest.raises(ModelConfigurationError, match='重新输入'):
            await registry.validate_connection({**plain, **change}, 'alice',
                profile_id=original['profile_id'], expected_revision=1)
    assert len(calls) == count
    updated = await save(registry, value={**plain, 'api_key': 'explicit-new-key', 'base_url': 'https://other.example/v1'},
        profile_id=original['profile_id'], expected_revision=1)
    assert 'credential_source' not in updated
    resolved = await registry.resolve(updated['profile_id'], buyer_id='alice')
    assert resolved['quota_group'] != 'current-gateway'
    assert registry.runtime(resolved)['api_key'] == 'explicit-new-key'
    assert all(key == 'explicit-new-key' for base, key in calls if base == 'https://other.example/v1')


@pytest.mark.asyncio
async def test_inherited_edit_fails_when_original_source_version_is_missing(settings):
    import aiosqlite
    registry = ModelRegistry(settings, probe=good_probe)
    source = await save(registry)
    inherited = draft(model_id='inherited-model', api_key='', provider=source['provider'], credential_profile_id=source['profile_id'],
        credential_profile_revision=source['profile_revision'])
    copied = await save(registry, value=inherited)
    async with aiosqlite.connect(registry.path) as db:
        await db.execute('DELETE FROM model_profiles WHERE profile_id=? AND revision=1', (source['profile_id'],))
        await db.commit()
    with pytest.raises(ModelConfigurationError, match='不存在'):
        await registry.validate_connection(draft(model_id='inherited-model', api_key='', label='不会覆盖旧版本'), 'alice',
            profile_id=copied['profile_id'], expected_revision=1)
    assert (await registry.resolve(copied['profile_id'], buyer_id='alice'))['profile_revision'] == 1


@pytest.mark.asyncio
async def test_catalog_virtual_presets_are_not_runtime_models(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    before = await registry.resolve_default('alice')
    catalog = await registry.catalog('alice')
    virtual = [item for item in catalog['models'] if item.get('is_virtual')]
    assert len(virtual) == 4
    assert {item['label'] for item in virtual} == {'Qwen3.8-Max', 'Qwen3.8-Flash', 'Qwen3.7-Plus', 'Qwen3.7-Flash'}
    for item in virtual:
        assert item['status'] == 'unconfigured' and item['catalog_group'] == 'default'
        assert item['connection_profile_id'] == 'current-gateway'
        assert item['connection_profile_revision'] == 1
        assert item['base_url'] == settings.llm_base_url
        assert not item['key_configured'] and not item['catalog_hidden']
        with pytest.raises(ModelConfigurationError):
            await registry.resolve(item['profile_id'], buyer_id='alice')
        with pytest.raises(ModelConfigurationError):
            await registry.set_default('alice', item['profile_id'])
    assert (await registry.resolve_default('alice'))['fingerprint'] == before['fingerprint']
    assert settings.llm_api_key not in json.dumps(catalog)
    assert 'secret_ref' not in json.dumps(catalog)


@pytest.mark.asyncio
async def test_verified_preset_is_canonical_without_changing_old_snapshot(settings):
    settings.llm_model = 'qwen3.8-max'
    registry = ModelRegistry(settings, probe=good_probe)
    before = await registry.resolve_default('alice')
    # 已配置同型号时不再增加同名虚拟行，但允许从预设入口显式升级窗口。
    assert not any(item['profile_id'] == 'preset-qwen3.8-max' for item in (await registry.catalog('alice'))['models'])
    upgraded = await save(registry, value=preset_draft(registry))
    catalog = await registry.catalog('alice')
    visible = [item for item in catalog['models'] if item['model_id'] == 'qwen3.8-max' and not item['catalog_hidden']]
    assert len(visible) == 1 and visible[0]['profile_id'] == upgraded['profile_id']
    assert visible[0]['label'] == 'Qwen3.8-Max' and visible[0]['catalog_group'] == 'default'
    old = next(item for item in catalog['models'] if item['profile_id'] == 'current-gateway')
    assert old['catalog_hidden'] and old['canonical_profile_id'] == upgraded['profile_id']
    assert catalog['default_selection']['profile_id'] == 'current-gateway'
    assert (await registry.resolve_default('alice'))['fingerprint'] == before['fingerprint']
    assert before['label'] == 'qwen3.8-max'
    restarted = ModelRegistry(settings, probe=good_probe)
    assert (await restarted.runtime_for_snapshot(before, 'alice'))['label'] == 'qwen3.8-max'
    # 模型 label 是目录投影，实际历史版本不改写。
    assert (await restarted._profile('current-gateway', 'alice', 1))['label'] == 'qwen3.8-max'


@pytest.mark.asyncio
async def test_same_connection_and_runtime_reuses_profile_and_consumes_ticket(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    original = await save(registry)
    before = await registry.resolve(original['profile_id'], buyer_id='alice')
    changed_label = draft(label='另一个显示名称')
    ticket = await registry.validate_connection(changed_label, 'alice')
    args = {key: ticket[key] for key in ('validation_ticket', 'config_hash')}
    again = await registry.save_connection(changed_label, 'alice', **args)
    assert again['reused'] is True and again['profile_id'] == original['profile_id']
    assert (await registry.resolve(original['profile_id'], buyer_id='alice'))['fingerprint'] == before['fingerprint']
    with pytest.raises(ModelConfigurationError, match='凭证'):
        await registry.save_connection(changed_label, 'alice', **args)
    with pytest.raises(ModelConfigurationError, match='编辑现有'):
        await save(registry, value=draft(context_max=128000))


@pytest.mark.asyncio
async def test_concurrent_duplicate_save_reuses_across_workers(settings):
    import asyncio
    first, second = ModelRegistry(settings, probe=good_probe), ModelRegistry(settings, probe=good_probe)
    await first.initialize()
    await second.initialize()
    value = draft()
    tickets = await asyncio.gather(first.validate_connection(value, 'alice'), second.validate_connection(value, 'alice'))
    results = await asyncio.gather(*(worker.save_connection(value, 'alice', **{key: ticket[key] for key in ('validation_ticket', 'config_hash')})
        for worker, ticket in zip((first, second), tickets)))
    assert results[0]['profile_id'] == results[1]['profile_id']
    assert sorted(item['reused'] for item in results) == [False, True]
    assert len([item for item in (await second.catalog('alice'))['models'] if item['profile_id'].startswith('custom-')]) == 1


@pytest.mark.asyncio
async def test_different_credentials_endpoints_and_buyers_are_not_deduplicated(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    one = await save(registry)
    two = await save(registry, value=draft(api_key='other-key'))
    three = await save(registry, value=draft(base_url='https://other.example/v1'))
    bob = await save(registry, buyer='bob')
    assert len({item['profile_id'] for item in (one, two, three, bob)}) == 4
    models = [item for item in (await registry.catalog('alice'))['models'] if item['model_id'] == 'custom-model']
    assert len(models) == 3 and all(not item['catalog_hidden'] for item in models)
    assert all(item['catalog_group'] == 'custom' for item in models)


@pytest.mark.asyncio
async def test_preset_upgrade_is_explicit_and_allowed_only_once(settings):
    settings.llm_model = 'qwen3.8-max'
    registry = ModelRegistry(settings, probe=good_probe)
    original = await registry.resolve_default('alice')
    upgraded = await save(registry, value=preset_draft(registry))
    repeated = await save(registry, value=preset_draft(registry, label='重复预设'))
    assert repeated['reused'] and repeated['profile_id'] == upgraded['profile_id']
    with pytest.raises(ModelConfigurationError, match='编辑现有'):
        await save(registry, value=preset_draft(registry, default_window='budget-400000'))
    # 明确编辑原模型仍支持，旧会话保留 128K 与原回退配置。
    edited = await save(registry, value=preset_draft(registry, default_window='budget-400000'),
        profile_id=upgraded['profile_id'], expected_revision=1)
    assert edited['profile_revision'] == 2
    assert (await registry.resolve_default('alice'))['fingerprint'] == original['fingerprint']


@pytest.mark.asyncio
async def test_archive_catalog_is_buyer_scoped_and_keeps_history_and_default(settings):
    registry = ModelRegistry(settings, probe=good_probe)
    before = await registry.resolve_default('alice')
    archived = await registry.archive_from_catalog('current-gateway', 'alice', 1)
    assert archived['catalog_hidden'] and archived['catalog_archived']
    bob = next(item for item in (await registry.catalog('bob'))['models'] if item['profile_id'] == 'current-gateway')
    assert not bob['catalog_hidden']
    assert (await registry.runtime_for_snapshot(before, 'alice'))['api_key'] == settings.llm_api_key
    assert (await registry.resolve_default('alice'))['fingerprint'] == before['fingerprint']
    with pytest.raises(ModelRevisionConflict):
        await registry.archive_from_catalog('current-gateway', 'alice', 99)
    custom = await save(registry)
    with pytest.raises(ModelConfigurationError):
        await registry.archive_from_catalog(custom['profile_id'], 'bob', 1)
    await registry.archive_from_catalog(custom['profile_id'], 'alice', 1)
    restored = await save(registry)
    assert restored['reused'] and not restored['catalog_hidden']


@pytest.mark.asyncio
async def test_existing_duplicate_projection_and_archive_preserve_old_runtime(settings):
    import aiosqlite
    from copy import deepcopy
    registry = ModelRegistry(settings, probe=good_probe)
    first = await save(registry)
    raw = await registry._profile(first['profile_id'], 'alice')
    legacy = {**deepcopy(raw), 'profile_id': 'custom-legacy-duplicate', 'label': '历史验收副本'}
    # 模拟升级前数据库已存在的重复连接，避免通过新保存规则制造假重复。
    async with aiosqlite.connect(registry.path) as db:
        await db.execute('INSERT INTO model_profiles VALUES(?,?,?,?)',
            (legacy['profile_id'], 1, 'alice', json.dumps(legacy)))
        await db.commit()
    historical = await registry.resolve(legacy['profile_id'], buyer_id='alice')
    catalog = await registry.catalog('alice')
    same_model = [item for item in catalog['models'] if item['model_id'] == 'custom-model']
    assert len(same_model) == 2 and sum(not item['catalog_hidden'] for item in same_model) == 1
    winner = next(item for item in same_model if not item['catalog_hidden'])
    assert all(item['canonical_profile_id'] == winner['profile_id'] for item in same_model)
    await registry.archive_from_catalog(legacy['profile_id'], 'alice', 1)
    catalog = await registry.catalog('alice')
    assert next(item for item in catalog['models'] if item['profile_id'] == legacy['profile_id'])['catalog_archived']
    assert not next(item for item in catalog['models'] if item['profile_id'] == first['profile_id'])['catalog_hidden']
    restarted = ModelRegistry(settings, probe=good_probe)
    assert (await restarted.runtime_for_snapshot(historical, 'alice'))['label'] == '历史验收副本'


@pytest.mark.asyncio
async def test_hidden_canonical_retains_credential_source_without_restoring_duplicate(settings):
    settings.llm_model = 'qwen3.8-max'
    registry = ModelRegistry(settings, probe=good_probe)
    upgraded = await save(registry, value=preset_draft(registry))
    pref = await registry.set_preferences(upgraded['profile_id'], 'alice', visible=False, expected_revision=1)
    assert not pref['catalog_hidden'] and not pref['visible']
    catalog = await registry.catalog('alice')
    old = next(item for item in catalog['models'] if item['profile_id'] == 'current-gateway')
    assert old['catalog_hidden'] and old['key_configured']
    # 目录隐藏不是权限回收，其他型号仍可显式复用这个已授权网关。
    other = preset_draft(registry, preset_id='qwen3.7-plus', model_id='qwen3.7-plus')
    added = await save(registry, value=other)
    assert added['quota_group'] == 'current-gateway' and added['catalog_group'] == 'default'


@pytest.mark.asyncio
async def test_legacy_display_names_only_change_catalog_projection(settings):
    settings.llm_model, settings.llm_fallback_model = 'qwen3-max', 'qwen-plus'
    registry = ModelRegistry(settings, probe=good_probe)
    snapshot = await registry.resolve(buyer_id='alice')
    catalog = await registry.catalog('alice')
    assert next(item for item in catalog['models'] if item['profile_id'] == 'current-gateway')['label'] == 'Qwen3-Max'
    assert next(item for item in catalog['models'] if item['profile_id'] == 'current-fallback')['label'] == 'Qwen-Plus'
    assert snapshot['label'] == 'qwen3-max' and snapshot['fallback']['label'] == 'qwen-plus'
    assert (await registry.runtime_for_snapshot(snapshot, 'alice'))['label'] == 'qwen3-max'


@pytest.mark.parametrize('label,expected', [
    ('经济', '经济'),
    ('qwen3.8-flash', 'Qwen3.8-Flash'),
    ('Qwen3.8 Flash', 'Qwen3.8-Flash'),
    ('Qwen3.8-Flash', 'Qwen3.8-Flash'),
])
@pytest.mark.parametrize('preset', [False, True])
@pytest.mark.asyncio
async def test_catalog_preserves_custom_display_name_and_only_normalizes_model_names(settings, label, expected, preset):
    registry = ModelRegistry(settings, probe=good_probe)
    value = (preset_draft(registry, preset_id='qwen3.8-flash', model_id='qwen3.8-flash', label=label)
             if preset else draft(model_id='qwen3.8-flash', label=label))
    saved = await save(registry, value=value)
    assert saved['label'] == expected
    displayed = next(item for item in (await registry.catalog('alice'))['models'] if item['profile_id'] == saved['profile_id'])
    assert displayed['label'] == expected
    snapshot = await registry.resolve(saved['profile_id'], buyer_id='alice')
    runtime = await registry.runtime_for_snapshot(snapshot, 'alice')
    assert runtime['model_id'] == 'qwen3.8-flash'
    assert runtime['label'] == label  # 展示投影不能修改原版本或运行身份。
