"""模型卡运行时、真实 HTTP 请求边界与计数合同；全部使用隔离假网关。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import UserMsg
from agentscope.model import ChatResponse, ChatUsage
from agentscope.state import AgentState
from pydantic import BaseModel

from app.infrastructure.context import ShoppingContext
from app.infrastructure.context_governance import LayeredContextMiddleware, ContextCapacityError, governance
from app.infrastructure.context_usage import context_usage_sink, context_call_kind
from app.infrastructure.llm import create_chat_model
from app.infrastructure.model_counting import (count_model_input, input_limits, model_identity,
    execution_identity, _load_local_tokenizer, CounterUnavailable)
from app.infrastructure.throttle import GatewayThrottle
from tests.test_retrieval import _settings


def profile(name='primary', **overrides):
    return {'profile_id': name, 'profile_revision': 1, 'model_id': name, 'label': name,
            'base_url': 'https://' + name + '.invalid/v1', 'api_key': name + '-fixture-key',
            'context_size': 128000, 'max_input_tokens': 110000, 'max_output_tokens': 4096,
            'reasoning_parameters': {'extra_body': {'enable_thinking': True}},
            'counter': {'kind': 'calibrated_estimate'}, 'quota_group': name,
            'fingerprint': name + '-fingerprint', 'fallback': None, **overrides}


def build(tmp_path, runtime, *, stream=False, throttle_factory=None):
    token = ShoppingContext.set(SimpleNamespace(model_runtime=runtime))
    try:
        return create_chat_model(replace(_settings(tmp_path), llm_max_retries=0), stream=stream,
                                 throttle=GatewayThrottle(1, 0), throttle_factory=throttle_factory)
    finally:
        ShoppingContext.reset(token)


async def mock_client(model, handler):
    await model.client.close()
    model.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def response(payload, *, returned_model=None, structured=False):
    message = {'role': 'assistant', 'content': '完成'}
    if structured:
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'summary', 'type': 'function', 'function': {
                'name': 'generate_structured_output', 'arguments': '{"goal":"背包"}'}}]}
    return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
        'model': returned_model or payload['model'],
        'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if structured else 'stop'}],
        'usage': {'prompt_tokens': 80, 'completion_tokens': 10, 'total_tokens': 90}})


async def test_profile_fallback_uses_own_credentials_window_reasoning_and_usage(tmp_path):
    fallback = profile('backup', context_size=64000, max_input_tokens=50000,
                       reasoning_parameters={'reasoning_effort': 'low'})
    runtime = profile(fallback=fallback)
    original = deepcopy(runtime)
    groups, requests, samples = {}, [], []
    def throttle(group):
        return groups.setdefault(group, GatewayThrottle(1, 0))
    def handler(request):
        data = json.loads(request.content)
        requests.append((data, request.url.host, request.headers['authorization']))
        if data['model'] == 'primary':
            return httpx.Response(429, json={'error': {'message': 'rate limit', 'type': 'rate_limit'}})
        assert groups['primary']._semaphore.locked() and groups['backup']._semaphore.locked()
        return response(data)
    model = build(tmp_path, runtime, throttle_factory=throttle)
    token = context_usage_sink.set(samples.append)
    try:
        for candidate in (model, model._fallback):
            await mock_client(candidate, handler)
        result = await model([UserMsg('buyer', '挑选轻便背包')])
        assert model.context_size == 128000 and model._fallback.context_size == 64000
        assert requests[0][1:] == ('primary.invalid', 'Bearer primary-fixture-key')
        assert requests[1][1:] == ('backup.invalid', 'Bearer backup-fixture-key')
        assert requests[0][0]['enable_thinking'] is True
        assert 'extra_body' not in requests[0][0]
        assert requests[1][0]['reasoning_effort'] == 'low' and 'enable_thinking' not in requests[1][0]
        assert result.metadata['model_execution']['profile_id'] == 'backup'
        assert [s['model_execution']['requested_model'] for s in samples] == ['primary', 'backup']
        assert samples[-1]['model_execution']['response_model_matches'] is True
        assert not any(g._semaphore.locked() for g in groups.values())
        assert runtime == original
    finally:
        context_usage_sink.reset(token)
        await model.client.close()
        await model._fallback.client.close()


async def test_small_fallback_capacity_rejected_before_network(tmp_path):
    runtime = profile(fallback=profile('backup', context_size=16000, max_input_tokens=16000))
    model = build(tmp_path, runtime)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content)['model'])
        return httpx.Response(429, json={'error': {'message': 'rate limit', 'type': 'rate_limit'}})
    try:
        for candidate in (model, model._fallback):
            await mock_client(candidate, handler)
        with pytest.raises(ContextCapacityError, match='备用模型窗口不足'):
            await model([UserMsg('buyer', '商品' * 8000)])
        assert requests == ['primary']
        assert not model._throttle._semaphore.locked()
    finally:
        await model.client.close()
        await model._fallback.client.close()


@pytest.mark.parametrize('case', ['same', 'fallback', 'drift', 'unknown', 'continuation'])
async def test_calibration_requires_exact_attempt_identity(tmp_path, case):
    model = build(tmp_path, profile())
    try:
        metadata = execution_identity(model, ['primary'])
        if case == 'fallback':
            metadata['identity'] = 'another-profile'
        elif case == 'drift':
            metadata['response_models'] = ['routed-other-model']
            metadata['response_model_matches'] = False
        elif case == 'unknown':
            metadata['response_model_matches'] = None
        result = ChatResponse(content=[], is_last=True, usage=ChatUsage(input_tokens=100, output_tokens=5, time=.1),
                              metadata={'model_execution': metadata, **({'continuations': 1} if case == 'continuation' else {})})
        agent = SimpleNamespace(model=model, state=AgentState(), name='agent')
        async def next_handler(**_):
            return result
        await LayeredContextMiddleware(None).on_model_call(agent,
            {'messages': [UserMsg('buyer', '背包')], 'tools': []}, next_handler)
        assert ('calibration' in governance(agent)) == (case == 'same')
        assert governance(agent)['last_model']['model_execution'] == metadata
    finally:
        await model.client.close()


async def test_structured_output_obeys_profile_and_records_attempt(tmp_path):
    class Summary(BaseModel):
        goal: str
    model = build(tmp_path, profile(reasoning_parameters={'extra_body': {'enable_thinking': False}}))
    requests, samples = [], []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return response(payload, structured=True)
    usage = context_usage_sink.set(samples.append)
    kind = context_call_kind.set('summary')
    try:
        await mock_client(model, handler)
        result = await model.generate_structured_output([UserMsg('buyer', '整理选购目标')], Summary)
        assert result.content == {'goal': '背包'}
        assert requests[0]['tools'][0]['function']['name'] == 'generate_structured_output'
        assert requests[0]['max_completion_tokens'] <= 4096 and requests[0]['enable_thinking'] is False
        assert len(samples) == 1 and samples[0]['kind'] == 'summary'
        assert samples[0]['model_execution']['profile_id'] == 'primary'
        assert samples[0]['input_tokens'] == 80
        assert not model._throttle._semaphore.locked()
    finally:
        context_usage_sink.reset(usage)
        context_call_kind.reset(kind)
        await model.client.close()


async def test_no_matching_tokenizer_remains_sdk_estimate_with_single_factor(tmp_path):
    model = build(tmp_path, profile(counter={'kind': 'local_chat_template', 'model_ids': ['other']}))
    try:
        count = await count_model_input(model, [UserMsg('buyer', '轻便背包')])
        assert count['method'] == 'agentscope_estimate'
        assert count['fallback_reason'] == 'model_mismatch'
        assert count['calibration_factor'] == 1.5
        assert count['estimated_input_tokens'] == __import__('math').ceil(count['raw_tokens'] * 1.5)
    finally:
        await model.client.close()


async def test_explicit_local_template_counts_sdk_formatted_messages_and_tools(tmp_path, monkeypatch):
    captured = []
    class LocalTokenizer:
        def apply_chat_template(self, messages, **kwargs):
            captured.append((messages, kwargs))
            return list(range(42))
    monkeypatch.setattr('app.infrastructure.model_counting._load_local_tokenizer', lambda *_: LocalTokenizer())
    model = build(tmp_path, profile(counter={'kind': 'local_chat_template', 'directory': str(tmp_path),
        'model_ids': ['primary'], 'files': {}, 'reasoning_parameters': profile()['reasoning_parameters'],
        'template_parameters': {'enable_thinking': True}}))
    tool = {'type': 'function', 'function': {'name': 'search', 'description': '搜索商品',
                                          'parameters': {'type': 'object', 'properties': {}}}}
    try:
        count = await count_model_input(model, [UserMsg('buyer', '背包')], [tool])
        assert count['raw_tokens'] == count['estimated_input_tokens'] == 42
        assert count['method'] == 'local_chat_template' and count['calibration_factor'] == 1
        assert captured[0][0][0]['role'] == 'user'
        assert captured[0][1]['tools'][0]['function']['name'] == 'search'
        assert captured[0][1]['enable_thinking'] is True
    finally:
        await model.client.close()


def test_local_tokenizer_manifest_is_fixed_and_remote_code_disabled(tmp_path, monkeypatch):
    import sys
    for name in ('tokenizer_config.json', 'tokenizer.json'):
        (tmp_path / name).write_text('{}')
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    calls = []
    def load(path, **kwargs):
        calls.append((path, kwargs))
        return SimpleNamespace(chat_template='fixed-template')
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=load)))
    _load_local_tokenizer.cache_clear()
    assert _load_local_tokenizer(str(tmp_path), json.dumps(hashes)).chat_template == 'fixed-template'
    assert calls[0][1] == {'local_files_only': True, 'trust_remote_code': False}
    _load_local_tokenizer.cache_clear()
    (tmp_path / 'tokenizer.json').write_text('changed')
    with pytest.raises(CounterUnavailable, match='manifest_mismatch'):
        _load_local_tokenizer(str(tmp_path), json.dumps(hashes))


async def test_counting_identity_tracks_reasoning_template_and_profile_revision(tmp_path):
    model = build(tmp_path, profile())
    try:
        identities = [model_identity(model)]
        model._shoppilot_runtime['reasoning_parameters'] = {'reasoning_effort': 'high'}
        identities.append(model_identity(model))
        model._shoppilot_runtime['counter'] = {'kind': 'local_chat_template', 'files': {'tokenizer.json': 'changed'}}
        identities.append(model_identity(model))
        model._shoppilot_runtime['profile_revision'] = 2
        identities.append(model_identity(model))
        assert len(set(identities)) == 4
        assert input_limits(model)['output_reserve'] == 4096
        assert input_limits(model)['input_budget'] == 110000
    finally:
        await model.client.close()


async def test_custom_model_uses_validated_transport_and_no_default_fallback(tmp_path):
    from app.infrastructure.model_connection import ValidatedModelTransport
    model = build(tmp_path, profile(name='custom-fixture'))
    try:
        assert isinstance(model.client._client._transport, ValidatedModelTransport)
        assert model.client._client.follow_redirects is False
        assert model._fallback is None
    finally:
        await model.client.close()


@pytest.mark.parametrize('same_group', [False, True])
async def test_fallback_stream_holds_group_until_close_before_first_chunk(tmp_path, same_group):
    fallback = profile('backup', quota_group='primary' if same_group else 'backup')
    groups = {}
    def throttle(group):
        return groups.setdefault(group, GatewayThrottle(1, 0))
    model = build(tmp_path, profile(fallback=fallback), stream=True, throttle_factory=throttle)
    def handler(request):
        payload = json.loads(request.content)
        if payload['model'] == 'primary':
            return httpx.Response(429, json={'error': {'message': 'rate limit', 'type': 'rate_limit'}})
        events = [
            {'id': 's', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'backup',
             'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '完成'}, 'finish_reason': None}]},
            {'id': 's', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'backup',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        ]
        body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, content=body, headers={'content-type': 'text/event-stream'})
    try:
        for candidate in (model, model._fallback):
            await mock_client(candidate, handler)
        result = await asyncio.wait_for(model([UserMsg('buyer', '背包')]), 2)
        assert all(group._semaphore.locked() for group in groups.values())
        await result.aclose()
        assert not any(group._semaphore.locked() for group in groups.values())
    finally:
        await model.client.close()
        await model._fallback.client.close()


async def test_streaming_chat_uses_complete_summary_without_mutating_model(tmp_path):
    class Summary(BaseModel):
        goal: str
    model = build(tmp_path, profile(), stream=True)
    requests, samples = [], []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        # 兼容网关的函数流结束标记不可靠；摘要应直接使用完整响应协议。
        assert payload['stream'] is False
        return response(payload, structured=True)
    usage = context_usage_sink.set(samples.append)
    kind = context_call_kind.set('summary')
    try:
        await mock_client(model, handler)
        result = await model.generate_structured_output([UserMsg('buyer', '保留当前预算')], Summary)
        assert result.content == {'goal': '背包'}
        assert model.stream is True
        assert len(requests) == 1 and len(samples) == 1
        assert samples[0]['kind'] == 'summary' and samples[0]['input_tokens'] == 80
        assert not model._throttle._semaphore.locked()
    finally:
        context_usage_sink.reset(usage)
        context_call_kind.reset(kind)
        await model.client.close()
