"""在真实 SDK/HTTP 边界验证前缀、摘要和续写；数据只使用隔离夹具。"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import UserMsg, TextBlock, ToolCallBlock
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import ContextAwareAgent, LayeredContextMiddleware, share_identical_products
from app.infrastructure.context_usage import context_usage_sink
from app.infrastructure.model_recovery import IncompleteModelResponse
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from tests.test_prompt_cache import client_model, completion, messages
from tests.test_skill_catalog import normalized


def response(text, reason='stop', stream=False):
    raw = completion()
    raw['choices'][0]['message']['content'] = text
    raw['choices'][0]['finish_reason'] = reason
    if not stream:
        return httpx.Response(200, json=raw)
    chunks = [dict(id='s', object='chat.completion.chunk', created=1, model='fixture',
        choices=[dict(index=0, delta={'content': text}, finish_reason=None)]),
        dict(id='s', object='chat.completion.chunk', created=1, model='fixture',
        choices=[dict(index=0, delta={}, finish_reason=reason)]),
        dict(id='s', object='chat.completion.chunk', created=1, model='fixture', choices=[], usage=raw['usage'])]
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
        content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')


async def collect(model, **kwargs):
    result = await model(messages(), **kwargs)
    if hasattr(result, '__aiter__'):
        async for part in result:
            if part.is_last:
                final = part
        return final
    return result


@pytest.mark.parametrize('stream', [False, True])
async def test_length_continues_tail_without_replaying_history_and_counts_every_call(tmp_path, stream):
    requests, usage = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        return response('第一段。' if len(requests) == 1 else '第二段。',
                        'length' if len(requests) == 1 else 'stop', stream)
    model = await client_model(tmp_path, handler, stream, model_recovery_mode='bounded')
    token = context_usage_sink.set(usage.append)
    try:
        result = await collect(model)
        assert ''.join(b.text for b in result.content) == '第一段。第二段。'
        assert len(requests) == len(usage) == 2
        assert [u['kind'] for u in usage] == ['business', 'continuation']
        assert usage[1]['request_context']['reason'] == 'response_continuation'
        assert result.usage.input_tokens == sum(u['input_tokens'] for u in usage)
        old = normalized(requests[0]['messages'])
        assert normalized(requests[1]['messages'])[:len(old)] == old
        assert requests[1]['tool_choice'] == 'none'
        assert '第一段。' in json.dumps(requests[1]['messages'], ensure_ascii=False)
        assert not model._throttle._semaphore.locked()
    finally:
        context_usage_sink.reset(token)
        await model.client.close()


async def test_unfinished_tool_regenerated_as_complete_call_without_executing(tmp_path):
    from tests.test_model_protocol import called, TOOLS
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        raw = called('order')
        if len(requests) == 1:
            raw['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{'
            raw['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200, json=raw)
    model = await client_model(tmp_path, handler, model_recovery_mode='bounded')
    try:
        result = await collect(model, tools=TOOLS)
        assert len(requests) == 2
        assert [(b.name, b.input) for b in result.content if isinstance(b, ToolCallBlock)] == [('order', '{}')]
        assert requests[1]['tools'] == requests[0]['tools']
        assert not any(m.get('tool_calls') for m in requests[1]['messages'])
    finally:
        await model.client.close()


async def test_continuation_cannot_execute_tool_even_if_gateway_ignores_none(tmp_path):
    from tests.test_model_protocol import called, TOOLS
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return response('已核验。', 'length') if len(requests) == 1 else httpx.Response(200, json=called('order'))
    model = await client_model(tmp_path, handler, model_recovery_mode='bounded')
    try:
        with pytest.raises(IncompleteModelResponse) as error:
            await collect(model, tools=TOOLS)
        assert error.value.partial_text == '已核验。'
        assert len(requests) == 2 and requests[1]['tool_choice'] == 'none'
    finally:
        await model.client.close()


@pytest.mark.parametrize('stream', [False, True])
async def test_exhausted_continuation_preserves_partial_in_agent_state(tmp_path, stream):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return response(f'第{len(requests)}段。', 'length', stream)
    model = await client_model(tmp_path, handler, stream, model_recovery_mode='bounded')
    agent = ContextAwareAgent('agent', '规则', model)
    try:
        try:
            await agent.reply(UserMsg('buyer', '请比较'))
        except IncompleteModelResponse:
            pass  # SDK 版本可能直接传播或转成 ReplyEndEvent(error)。
        assert len(requests) == 3
        restored = AgentState.model_validate_json(agent.state.model_dump_json())
        assert '第1段。第2段。第3段。' in restored.context[-1].get_text_content()
        assert '未完成' in restored.context[-1].get_text_content()
        assert isinstance(agent._shoppilot_incomplete_response, IncompleteModelResponse)
    finally:
        await model.client.close()


@pytest.mark.parametrize('failure', ['network', 'cancel'])
async def test_partial_stream_network_recovers_but_cancellation_stops(tmp_path, failure):
    requests = []
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            raw = {'id': 'a', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                   'choices': [{'index': 0, 'delta': {'content': '已生成。'}, 'finish_reason': None}]}
            yield ('data: '+json.dumps(raw)+'\n\n').encode()
            if failure == 'cancel':
                raise asyncio.CancelledError()
            raise httpx.ReadError('连接中断')
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Broken()) if len(requests) == 1 else response('继续完成。', stream=True)
    model = await client_model(tmp_path, handler, True, model_recovery_mode='bounded')
    usage = []; token = context_usage_sink.set(usage.append)
    try:
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await collect(model)
            assert len(requests) == 1
        else:
            result = await collect(model)
            assert ''.join(b.text for b in result.content) == '已生成。继续完成。'
            assert result.usage is None and usage[0]['input_tokens'] is None
            assert len(requests) == 2
        assert not model._throttle._semaphore.locked()
    finally:
        context_usage_sink.reset(token)
        await model.client.close()


async def test_historical_extraction_preserves_wire_messages_and_original_tools(tmp_path):
    from tests.test_visible_evidence import fixture
    from tests.test_model_protocol import TOOLS
    from app.infrastructure.visible_evidence import visible_answer_request
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    model = await client_model(tmp_path, lambda _: response('OK'))
    try:
        store, rows, _ = await fixture(tmp_path)
        before = normalized(await model.formatter.format(rows))
        result = await visible_answer_request(SimpleNamespace(), rows, store, preserve_prefix=True, tools=TOOLS)
        wire = normalized(await model.formatter.format(result['messages']))
        assert wire[:len(before)] == before
        assert result['tools'] == TOOLS and result['tool_choice'].mode == 'none'
    finally:
        ShoppingContext.reset(token)
        await model.client.close()


@pytest.mark.parametrize('invalid', [False, True])
async def test_summary_preserves_original_http_prefix_tools_and_rolls_back_invalid(tmp_path, invalid):
    from tests.test_layered_context import fixture_agent
    from app.application.agents.context_policy import build_context_config
    requests = []
    answer = dict(task_overview='选背包', current_state='正在比较', important_discoveries='P999999' if invalid else 'P0-S1',
                  next_steps='核验最新库存', context_to_preserve='历史不是授权')
    def handler(request):
        requests.append(json.loads(request.content))
        return response(json.dumps(answer, ensure_ascii=False))
    async def lookup():
        """查询测试资料。"""
        raise AssertionError('摘要不得执行工具')
    model = await client_model(tmp_path, handler)
    middleware = LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),
        product_tokens=100000, prompt_layout='stable_prefix', summary_mode='preserve_prefix')
    agent = ContextAwareAgent('agent', '原系统规则', model, state=fixture_agent().state,
        toolkit=Toolkit(tools=[FunctionTool(lookup, is_read_only=True)]), middlewares=[middleware],
        context_config=build_context_config(128000, 20000))
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    usage = []; ut = context_usage_sink.set(usage.append)
    try:
        prepared = await agent._prepare_model_input()
        await model(share_identical_products(prepared['messages']), prepared['tools'])
        before = deepcopy(agent.state.context)
        previous_summary = agent.state.summary
        if invalid:
            with pytest.raises(ValueError):
                await middleware.run(agent, force=True)
            assert agent.state.context == before and agent.state.summary == previous_summary
            assert len(requests) == 3  # 一次业务，两次来源校验失败，不能回退绕过校验。
        else:
            report = await middleware.run(agent, force=True)
            assert report['summary_changed'] and len(agent.state.context) == 6
            assert 'P0-S1' in agent.state.summary
        wire = normalized(requests[0]['messages'])
        assert normalized(requests[1]['messages'])[:len(wire)] == wire
        assert requests[1]['tools'] == requests[0]['tools']
        assert requests[1]['tool_choice'] == 'none'
        assert [u['kind'] for u in usage][1:] == ['summary'] * (len(usage)-1)
        assert all(u['request_context']['reason'] == 'context_summary' for u in usage[1:])
        assert all(u['request_context']['tool_schema_estimated_tokens'] > 0 for u in usage[1:])
    finally:
        context_usage_sink.reset(ut); ShoppingContext.reset(token)
        await model.client.close()


@pytest.mark.parametrize('protected', [False, True])
async def test_server_capacity_rejection_only_archives_read_results_and_retries_once(tmp_path, protected):
    from tests.test_layered_context import fixture_agent, mark_consumed
    from app.infrastructure.context_governance import ContextCapacityError, governance
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={'error': {'code': 'context_length_exceeded', 'message': 'maximum context length'}})
        return response('已按原要求继续。')
    model = await client_model(tmp_path, handler, model_recovery_mode='bounded')
    mw = LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'), product_tokens=100000,
                                  prompt_layout='stable_prefix')
    agent = ContextAwareAgent('agent', '规则', model, state=fixture_agent().state, middlewares=[mw])
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    try:
        if not protected:
            mark_consumed(agent)
        before = agent.state.model_dump_json()
        prepared = await agent._prepare_model_input()
        if protected:
            with pytest.raises(ContextCapacityError):
                await agent._call_model(**prepared)
            assert len(requests) == 1
            assert agent.state.context == AgentState.model_validate_json(before).context
        else:
            result = await agent._call_model(**prepared)
            assert len(requests) == 2 and result.content[0].text == '已按原要求继续。'
            assert len(agent.state.context) == len(AgentState.model_validate_json(before).context)
            assert governance(agent)['last_compaction']['reason'] == 'input_too_long'
            assert governance(agent)['last_compaction']['summary_changed'] is False
        assert not model._throttle._semaphore.locked()
    finally:
        ShoppingContext.reset(token); await model.client.close()


async def test_stable_orchestrator_appends_current_empty_snapshots_without_erasing_history(tmp_path):
    from unittest.mock import AsyncMock
    from tests.test_ag_ui import make_orchestrator
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore
    orchestrator, agent, _ = make_orchestrator()
    orchestrator._sessions._main_factory = SimpleNamespace(stable_prefix=True, skill_catalog_mode='append_only')
    orchestrator._preference_store = JsonFilePreferenceStore(tmp_path/'preferences')
    orchestrator._trade_state_provider = AsyncMock(return_value={})
    agent.state.context[:] = [UserMsg('memory_hint', '旧偏好：喜欢皮革'),
        UserMsg('trade_state', '旧待确认订单'), UserMsg('candidate_state', '旧候选'), UserMsg('buyer-test', '旧需求')]
    old = [m.model_dump_json() for m in agent.state.context]
    received = []
    async def reply(session, actual_agent, inputs):
        received.extend(inputs)
        actual_agent.state.context.extend(inputs)
        return '当前偏好已撤回'
    orchestrator._reply_with_retry = reply
    result = await orchestrator.handle_intent(SubmitIntentInput('session-test','buyer-test','zh-CN','CNY','继续'))
    assert not result.error
    assert [m.model_dump_json() for m in agent.state.context[:len(old)]] == old
    assert '无。' in next(m.get_text_content() for m in received if m.name == 'memory_hint')
    assert '{}' in next(m.get_text_content() for m in received if m.name == 'trade_state')


async def test_recovery_exhaustion_never_restarts_whole_agent_turn():
    from unittest.mock import AsyncMock
    from tests.test_ag_ui import make_orchestrator
    orchestrator, agent, _ = make_orchestrator()
    agent.model = SimpleNamespace(_recovery_mode='bounded')
    orchestrator._consume_reply = AsyncMock(side_effect=RuntimeError('connection reset'))
    with pytest.raises(RuntimeError):
        await orchestrator._reply_with_retry('session-test', agent, [])
    assert orchestrator._consume_reply.await_count == 1


@pytest.mark.parametrize('stream', [False, True])
async def test_capacity_retry_then_exhausted_response_still_persists_partial(tmp_path, stream):
    from tests.test_layered_context import fixture_agent, mark_consumed
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400, json={'error': {
                'code': 'context_length_exceeded', 'message': 'maximum context length'}})
        return response('已生成段落。', 'length', stream)
    model = await client_model(tmp_path, handler, stream, model_recovery_mode='bounded')
    middleware = LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),
        product_tokens=100000, prompt_layout='stable_prefix')
    agent = ContextAwareAgent('agent', '规则', model, state=fixture_agent().state,
        middlewares=[middleware])
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    try:
        mark_consumed(agent)
        with pytest.raises(IncompleteModelResponse):
            result = await agent._call_model(**(await agent._prepare_model_input()))
            if hasattr(result, '__aiter__'):
                async for _ in result:
                    pass
        restored = AgentState.model_validate_json(agent.state.model_dump_json())
        assert len(requests) == 4  # 输入超限一次，正常请求加最多两次续写。
        assert restored.context[-1].get_text_content() == '已生成段落。' * 3 + '\n[本条回答未完成]'
        assert isinstance(agent._shoppilot_incomplete_response, IncompleteModelResponse)
    finally:
        ShoppingContext.reset(token)
        await model.client.close()


def test_report_accepts_partial_diagnostics_without_fabricating_zero_cost(tmp_path):
    from tests.test_harness_evaluation import manifest, rows
    from scripts.eval.harness.report import render
    samples = rows()
    samples[0]['usage'][0]['request_context'] = {'recovery_attempt': 0}
    report = render(tmp_path, manifest(), samples)
    diagnostics = report['strategies']['current']['efficiency_diagnostics']
    assert diagnostics['request_reasons'] == {'unknown': 1}
    assert all(value is None for value in diagnostics['estimated_sections'].values())
    assert report['strategies']['current']['input_tokens']['total'] == 600
    assert (tmp_path/'report.html').is_file()
