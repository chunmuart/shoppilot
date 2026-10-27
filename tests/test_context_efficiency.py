"""独立开关的状态恢复、参数归一化、分页完整性和整理滞回回归。"""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest
from agentscope.message import UserMsg, ToolResultBlock
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from app.application.tools.conversation_fact_lookup import build_conversation_fact_lookup
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import ContextAwareAgent, LayeredContextMiddleware, governance, update_working_state, blocks, read_output, share_identical_products
from app.infrastructure.context_products import token_estimate
from app.infrastructure.context_state import next_state_message, replay_state, projected_state, semantic_state, project_state_messages, snapshot_message
from app.infrastructure.context_usage import context_diagnostic_sink, context_usage_sink
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from tests.test_prompt_cache import client_model, completion
from tests.test_layered_context import fixture_agent, mark_consumed


@pytest.fixture(autouse=True)
def scope():
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    yield
    ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_unchanged_state_does_not_accumulate_snapshots_and_restore_has_current_truth(tmp_path):
    requests, samples = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion())
    model = await client_model(tmp_path, handler)
    mw = LayeredContextMiddleware(None, prompt_layout='stable_prefix', state_mode='delta')
    def build(state=None):
        return ContextAwareAgent('agent', '规则', model, toolkit=Toolkit(), middlewares=[mw], state=state)
    sink = context_usage_sink.set(samples.append)
    try:
        agent = build()
        await agent.reply(UserMsg('b', '本次预算300元，寄到中国，选中P1003-S1，不要P1002。'))
        for text in ['还有别的款吗？', '第一批第三个还有货吗？', '有哪些黑色的？']:
            await agent.reply(UserMsg('b', text))
        assert len([m for m in agent.state.context if m.name.startswith('shopping_state')]) == 1
        assert governance(agent)['working']['latest_request'] == '有哪些黑色的？'
        assert all(x['prompt_cache']['prefix_comparison'] == 'append' for x in samples[1:])
        await agent.reply(UserMsg('b', '预算改为180元。'))
        state, _, _ = replay_state(agent.state.context)
        assert state['constraints']['budget']['value'] == '180'
        assert state['selected'] == ['P1003-S1'] and state['excluded'] == ['P1002']
        restored = build(AgentState.model_validate_json(agent.state.model_dump_json()))
        prepared = await restored._prepare_model_input()
        assert replay_state(prepared['messages'])[0]['constraints']['budget']['value'] == '180'
        await restored.reply(UserMsg('b', '换个需求，现在想买耳机。'))
        state, _, _ = replay_state(restored.state.context)
        assert state['constraints'] == {} and state['selected'] == [] and 'exclusion_reasons' not in state
        assert len({json.dumps(r['messages'][0], sort_keys=True) for r in requests}) == 1
        assert samples[-1]['request_context']['reason'] == 'buyer_request'
        assert '预算' not in json.dumps(samples)
    finally:
        context_usage_sink.reset(sink)
        await model.client.close()


def test_delta_constraint_removal_and_broken_chain_restore_are_explicit():
    work = {'goal': '买包', 'constraints': {str(i): {'source': '防水轻便' * 30, 'message_id': 'm1'} for i in range(8)},
            'selected': ['P1-S1'], 'excluded': [], 'source_message_id': 'm1', 'latest_request': '原始请求'}
    history = [snapshot_message(work)]
    changed = deepcopy(work)
    del changed['constraints']['3']
    changed['constraints']['2'] = {'source': '允许较重', 'message_id': 'm2'}
    hint = next_state_message(history, changed)
    assert hint.name == 'shopping_state_delta'
    history.append(hint)
    assert semantic_state(replay_state(history)[0]) == semantic_state(projected_state(changed))
    assert replay_state(history[1:])[0] is None
    repaired = project_state_messages(history[1:], changed)
    assert len(repaired) == 1 and replay_state(repaired)[0]['constraints']['2']['source'] == '允许较重'
    assert 'latest_request' not in repaired[0].get_text_content()
    # 没有语义变化，只有审计来源的新消息ID，不制造新模型状态。
    changed['constraints']['2']['message_id'] = 'm3'
    assert next_state_message(history, changed) is None


def test_snapshot_rebases_after_bounded_number_of_deltas():
    work = {'goal': '买包', 'constraints': {str(i): {'source': '材料限制' * 60} for i in range(8)}}
    history = [snapshot_message(work)]
    for i in range(9):
        work['constraints']['budget'] = {'value': str(100 + i)}
        history.append(next_state_message(history, work))
    assert history[-1].name == 'shopping_state'
    assert replay_state(history)[2] == 0


def test_projection_does_not_move_snapshot_before_latest_user():
    user = UserMsg('b', '新预算180')
    work = {'constraints': {}, 'source_message_id': user.id}
    from agentscope.message import SystemMsg
    messages = [SystemMsg('system', '规则'), user]
    assert project_state_messages(messages, work)[:2] == messages


def test_compact_projection_is_lossless_and_does_not_mutate_first_read_or_evidence():
    agent = fixture_agent(4)
    original = agent.state.model_copy(deep=True)
    compact = share_identical_products(agent.state.context, compact_rules=True)
    assert agent.state == original
    assert [read_output(b) for _, b in blocks(compact)] == [read_output(b) for _, b in blocks(original.context)]
    def text_size(messages):
        # 比较实际发送文本，不能把 SDK 的 TextBlock 对象 repr 当作请求正文。
        return sum(len(b.output if isinstance(b.output, str) else ''.join(item.text for item in b.output))
                   for _, b in blocks(messages))
    assert text_size(compact) < text_size(original.context)
    # 固定规则去重不改变共享商品的完整字段、引用或顺序。
    repeated = [*agent.state.context, *deepcopy(agent.state.context)]
    inline = share_identical_products(repeated)
    reduced = share_identical_products(repeated, compact_rules=True)
    for (_, a), (_, b) in zip(blocks(inline), blocks(reduced)):
        value = read_output(a); value.pop('shared_fields_notice', None)
        assert read_output(b) == value


async def test_compact_rules_present_in_model_and_summary_system_prompt(tmp_path):
    model = await client_model(tmp_path, lambda _: httpx.Response(200, json=completion()))
    try:
        mw = LayeredContextMiddleware(None, prompt_layout='stable_prefix', state_mode='delta', compact_result_rules=True)
        agent = ContextAwareAgent('agent', '规则', model, toolkit=Toolkit(), middlewares=[mw])
        prepared = await agent._prepare_model_input()
        assert 'archived=true' in prepared['messages'][0].get_text_content()
        assert '本批顺序、查询条件、观察时间' in prepared['messages'][0].get_text_content()
    finally:
        await model.client.close()


async def test_summary_rebases_state_and_failed_summary_preserves_last_valid_context(tmp_path):
    agent = fixture_agent(8)
    update_working_state(agent, [UserMsg('b', '本次预算180元，选中P1003-S1。')])
    agent.state.context.insert(0, snapshot_message(governance(agent)['working']))
    mw = LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'), prompt_layout='stable_prefix',
                                  state_mode='delta', target_tokens=20000)
    async def accepted(**kwargs):
        agent.state.summary = '预算180元，选中P1003-S1，尚未授权交易。'
        agent.state.context = agent.state.context[-6:]
    result = await mw.run(agent, force=True, next_handler=accepted)
    assert result['summary_changed']
    assert agent.state.context[0].name == 'shopping_state'
    assert replay_state(agent.state.context)[0]['constraints']['budget']['value'] == '180'
    agent.state.context.extend(fixture_agent(12).state.context[-10:])
    before = agent.state.model_copy(deep=True)
    async def rejected(**kwargs):
        agent.state.summary = '不存在的SKU P99999-S9'
        agent.state.context = []
    with pytest.raises(ValueError):
        await mw.run(agent, force=True, next_handler=rejected)
    assert agent.state.context == before.context and agent.state.summary == before.summary


def test_diagnostic_html_keeps_unknowns_and_escapes_events(tmp_path):
    from tests.test_harness_evaluation import manifest, rows
    from scripts.eval.harness.report import render
    data = rows()
    data[0]['context_diagnostics'] = [{'type': 'lookup', 'status': 'error', 'error_code': '<script>bad</script>'}]
    report = render(tmp_path, manifest(), data)
    assert report['strategies']['current']['efficiency_diagnostics']['lookup_errors'] == 1
    html = (tmp_path/'report.html').read_text()
    assert '调用增量与治理归因' in html and '&lt;script&gt;bad' in html
    assert '<script>bad</script>' not in html


@pytest.mark.parametrize('mode,success', [('strict', False), ('bounded', True)])
async def test_lookup_repairs_only_unambiguous_parameters_and_keeps_all_pages(tmp_path, mode, success):
    store = ContextEvidenceStore(tmp_path / 'e.db')
    hits = [{'product_id': f'P{i}', 'title': '背包', 'skus': [{'sku_id': f'P{i}-S1', 'spec': '黑色', 'price_major': 10 + i, 'currency': 'CNY'}]} for i in range(7)]
    ref = await store.save('b', 's', 'display_batch', {'hits': hits})
    lookup = build_conversation_fact_lookup(store, mode=mode)
    events = []; token = context_diagnostic_sink.set(events.append)
    try:
        result = await lookup(result_ref=ref, fields='skus.sku_id,skus.spec,skus.price_major,skus.currency', limit=10)
        assert (str(result.state) == 'success') == success
        if not success:
            assert events[-1]['error_code'] == 'invalid_request'
            return
        payload = json.loads(result.content[0].text)
        page = payload['records'][0]['data']
        assert page['hits'] == hits[:5] and page['next_offset'] == 5 and page['total'] == 7
        assert not payload['page_contract']['complete_selection']
        assert events[-1]['limit_capped'] and events[-1]['fields_normalized']
        more = json.loads((await lookup(result_ref=ref, fields='skus', offset=5)).content[0].text)
        assert more['records'][0]['data']['hits'] == hits[5:]
        assert more['page_contract']['complete_selection']
        assert token_estimate(payload) <= 3000 and token_estimate(more) <= 3000
    finally:
        context_diagnostic_sink.reset(token)


async def test_lookup_rejects_unknown_fields_and_cross_buyer_reference(tmp_path):
    store = ContextEvidenceStore(tmp_path / 'e.db')
    ref = await store.save('other', 's', 'display_batch', {'hits': [{'product_id': 'P99'}]})
    lookup = build_conversation_fact_lookup(store, mode='bounded')
    assert str((await lookup(fields='skus.secret')).state) == 'error'
    assert str((await lookup(limit=0)).state) == 'error'
    result = json.loads((await lookup(result_ref=ref)).content[0].text)
    assert result['records'] == [] and not result['page_contract']['complete_selection']


async def test_synthetic_lookup_trace_preserves_actual_parameters_pages_and_errors(tmp_path):
    from app.infrastructure.context_usage import evaluation_evidence_sink
    store = ContextEvidenceStore(tmp_path/'e.db')
    ref = await store.save('b', 's', 'display_batch', {'hits': [
        {'product_id': f'P{i}', 'skus': [{'sku_id': f'P{i}-S1', 'spec': '黑色', 'price_major': i, 'currency': 'CNY'}]}
        for i in range(7)]})
    lookup = build_conversation_fact_lookup(store, mode='bounded')
    events = []; token = evaluation_evidence_sink.set(events.append)
    try:
        response = await lookup(result_ref=ref, fields='skus.price_major,skus.currency', limit=10)
        page = events[-1]
        assert page['kind'] == 'lookup_result'
        assert page['payload']['arguments']['limit'] == 10
        assert page['payload']['effective_limit'] == 5
        assert page['payload']['result'] == json.loads(response.content[0].text)
        assert page['payload']['result']['records'][0]['data']['next_offset'] == 5
        await lookup(result_ref=ref, fields='unknown-secret-field')
        assert events[-1]['payload']['state'] == 'error'
        assert events[-1]['payload']['arguments']['fields'] == 'unknown-secret-field'
        assert events[-1]['payload']['error_code'] == 'invalid_fields'
    finally:
        evaluation_evidence_sink.reset(token)


async def test_bounded_page_budget_includes_evidence_envelope(tmp_path):
    store = ContextEvidenceStore(tmp_path / 'e.db')
    hits = [{'product_id': f'P{i}', 'description': '限制' * 500, 'title': '包'} for i in range(6)]
    ref = await store.save('b', 's', 'display_batch', {'hits': hits, 'query_conditions': {'query': '旅行' * 70}})
    lookup = build_conversation_fact_lookup(store, mode='bounded')
    collected = []; offset = 0
    while True:
        chunk = await lookup(result_ref=ref, offset=offset)
        assert str(chunk.state) == 'success'
        payload = json.loads(chunk.content[0].text)
        assert token_estimate(payload) <= 3000
        page = payload['records'][0]['data']; collected.extend(page['hits'])
        if page['next_offset'] is None: break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert collected == hits


async def test_low_watermark_retains_protection_and_delays_rewrites(tmp_path):
    old = fixture_agent(10); new = deepcopy(old)
    mark_consumed(old); mark_consumed(new)
    a = LayeredContextMiddleware(ContextEvidenceStore(tmp_path / 'a.db'), product_tokens=1400)
    b = LayeredContextMiddleware(ContextEvidenceStore(tmp_path / 'b.db'), product_tokens=1400, prune_low_ratio=.65)
    assert await a.prune(old) > 0 and await b.prune(new) > 0
    assert all(read_output(block).get('hits') for _, block in list(blocks(new.state.context))[-3:])
    watermark = governance(new)['product_watermark']
    assert watermark['trigger_tokens'] > b.product_tokens
    # 新增一批已读候选；旧策略立即改写，低水位策略在有限缓冲中保持原前缀。
    additions = fixture_agent(11).state.context[-2:]
    for agent in (old, new):
        agent.state.context.extend(deepcopy(additions)); mark_consumed(agent)
    saved = new.state.model_dump_json()
    assert await a.prune(old) > 0
    assert await b.prune(new) == 0
    assert [read_output(v) for _, v in blocks(new.state.context)] == [read_output(v) for _, v in blocks(AgentState.model_validate_json(saved).context)]
    assert await b.prune(new, capacity_pressure=True) > 0


@pytest.mark.parametrize('kwargs', [{'state_mode':'delta'}, {'prune_low_ratio':0}, {'prune_low_ratio':1.1}, {'state_mode':'bad'}])
def test_invalid_policy_rejected(kwargs):
    with pytest.raises(ValueError): LayeredContextMiddleware(None, **kwargs)


def test_efficiency_suite_preserves_model_permissions_and_default_switches(tmp_path):
    from scripts.eval.harness.contracts import load_suite, ROOT
    from tests.test_retrieval import _settings
    suite = load_suite(ROOT / 'eval/harness/v1/efficiency-suite.json')
    assert suite['profiles']['efficiency']['repetitions'] == 3
    assert len(suite['profiles']['long_efficiency']['cases']) == 2
    settings = _settings(tmp_path)
    assert settings.context_state_mode == 'snapshot' and settings.context_lookup_mode == 'strict'
    assert settings.context_prune_low_ratio == 1.0
    assert suite['strategies']['candidate']['overrides']['context_prune_low_ratio'] == .65
