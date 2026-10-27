"""历史事实归属、无损共享与字段回查的缺陷回归。"""
import copy
import json
import pytest
from agentscope.message import Msg, ToolResultBlock, ToolResultState
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_products import business_view, product_page
from app.infrastructure.context_governance import share_identical_products, blocks, read_output
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.application.tools.conversation_fact_lookup import build_conversation_fact_lookup


def product():
    return {'product_id': 'P887', 'description': '完整材质与限制：真皮部分不可水洗；合成面料允许擦拭；运费另计。' * 8,
            'skus': [{'sku_id': 'P887-S1', 'spec': '黑', 'price_major': 213, 'currency': 'CNY', 'stock': 37},
                     {'sku_id': 'P887-S2', 'spec': '蓝', 'price_major': 229, 'currency': 'CNY', 'stock': 19}]}


def test_description_only_exact_whole_long_repeats_removed():
    text = '完整材质与限制：真皮部分不可水洗；合成面料允许擦拭；运费另计，退货需原包装。'
    assert business_view({'description': text * 8})['description'] == text
    changed = text * 7 + text.replace('允许擦拭', '禁止擦拭')
    assert business_view({'description': changed})['description'] == changed
    assert business_view({'description': '哈哈' * 100})['description'] == '哈哈' * 100


@pytest.mark.parametrize('fields', ['skus', 'stock', 'sku_id,stock', 'price_major,currency', 'price',
                                   'price,stock', 'price_major,stock_quantity', 'unit_price,inventory'])
def test_lookup_fields_retain_sku_value_association(fields):
    page = product_page({'hits': [product()], 'observed_at': '2026-08-01'}, sku_id='P887-S2', fields=fields)
    assert page['hits'][0]['skus'] == [product()['skus'][1]]
    assert page['historical'] and page['observed_at'] == '2026-08-01'


def test_nondefault_sku_does_not_inherit_default_quote():
    hit = {**product(), 'default_sku_id': 'P887-S1', 'price_major': 213, 'currency': 'CNY',
           'landed_price': {'total_major': 238, 'shipping_major': 25}}
    page = product_page({'hits': [hit]}, sku_id='P887-S2', fields='price')
    quote = page['hits'][0]
    assert quote['sku_id'] == 'P887-S2' and quote['price_major'] == 229
    assert 'landed_price' not in quote
    original = product_page({'hits': [hit]}, sku_id='P887-S1', fields='price')['hits'][0]
    assert original['landed_price']['total_major'] == 238
    full = product_page({'hits': [hit]}, sku_id='P887-S2')['hits'][0]
    assert full['other_or_unspecified_sku_quote']['sku_id'] == 'P887-S1'
    assert hit['price_major'] == 213


def test_shared_objects_round_trip_without_changing_order_or_quote_scope():
    messages = []
    for i, country in enumerate(['CN', 'CN', 'JP', 'CN']):
        hit = product()
        if i == 3:
            hit['skus'][0]['price_major'] = 217
        payload = {'hits': [hit], 'query_conditions': {'ship_to': country, 'currency': 'CNY', 'quantity': 1},
                   'result_ref': f'ctx_{i}', 'observed_at': f'2026-08-0{i+1}'}
        messages.append(Msg(name='agent', role='assistant', content=[ToolResultBlock(
            id=f'call{i}', name='product_search_tool', output=json.dumps(payload), state=ToolResultState.SUCCESS)]))
    original = copy.deepcopy(messages)
    shared = share_identical_products(messages)
    originals = {b.id: read_output(b) for _, b in blocks(original)}
    outputs = {b.id: read_output(b) for _, b in blocks(shared)}
    assert 'same_business_fields_as' in outputs['call1']['hits'][0]
    assert 'same_business_fields_as' not in outputs['call2']['hits'][0]
    assert 'same_business_fields_as' not in outputs['call3']['hits'][0]
    for call, payload in outputs.items():
        restored = copy.deepcopy(payload)
        restored.pop('shared_fields_notice', None)
        for i, hit in enumerate(restored['hits']):
            if 'same_business_fields_as' in hit:
                ref = hit['same_business_fields_as']
                restored['hits'][i] = outputs[ref['tool_call_id']]['hits'][ref['position'] - 1]
        assert restored == originals[call]
    assert [m.model_dump() for m in messages] == [m.model_dump() for m in original]
    # 去掉旧消息重新准备时不能产生悬空引用。
    assert 'same_business_fields_as' not in read_output(next(blocks(share_identical_products(messages[1:])))[1])['hits'][0]


async def test_lookup_filters_before_latest_and_never_crosses_buyer(tmp_path):
    store = ContextEvidenceStore(tmp_path / 'e.db')
    await store.save('buyer', 'session', 'display_batch', {'hits': [product()]})
    await store.save('buyer', 'session', 'display_batch', {'hits': [{'product_id': 'P999'}]})
    await store.save('other', 'session', 'display_batch', {'hits': [{**product(), 'secret': 'other'}]})
    token = ShoppingContext.set(ShoppingContextSnapshot('session', 'buyer', 'zh-CN', 'CNY'))
    try:
        result = await build_conversation_fact_lookup(store)(sku_id='P887-S2', fields='sku_id,stock')
        payload = json.loads(result.content[0].text)
        assert payload['records'][0]['data']['hits'][0]['skus'][0]['stock'] == 19
        assert payload['records'][0]['observation_scope']['time_basis'] == 'historical'
        assert 'secret' not in json.dumps(payload)
        missing = await build_conversation_fact_lookup(store)(batch=2, sku_id='P887-S2')
        assert json.loads(missing.content[0].text)['records'][0]['data']['hits'] == []
    finally:
        ShoppingContext.reset(token)


async def test_summary_input_shares_locally_without_dangling_tail_reference():
    from tests.test_layered_context import fixture_agent
    from tests.test_native_memory_confirmation import Model
    from app.infrastructure.context_governance import ContextAwareAgent, set_output
    model = Model()
    source = fixture_agent().state
    for _, block in blocks(source.context):
        set_output(block, {'hits': [product()]})
    agent = ContextAwareAgent('agent', '测试', model, state=source)
    agent._shoppilot_layered_split = True
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    try:
        head, tail = await agent._split_context_for_compression(0, [])
        ids = {b.id for _, b in blocks(head)}
        references = [read_output(b)['hits'][0]['same_business_fields_as'] for _, b in blocks(head)
                      if 'same_business_fields_as' in read_output(b)['hits'][0]]
        assert references and all(ref['tool_call_id'] in ids for ref in references)
        assert all('same_business_fields_as' not in read_output(b)['hits'][0] for _, b in blocks(tail))
        assert all('same_business_fields_as' not in read_output(b)['hits'][0] for _, b in blocks(source.context))
    finally:
        ShoppingContext.reset(token)
        await model.client.close()


async def test_summary_repair_restarts_from_valid_source_and_rejected_text_cannot_be_recalled(tmp_path):
    from tests.test_layered_context import fixture_agent
    from app.infrastructure.context_governance import LayeredContextMiddleware, governance
    agent = fixture_agent()
    store = ContextEvidenceStore(tmp_path / 'e.db')
    middleware = LayeredContextMiddleware(store, target_tokens=10)
    calls = []
    async def summarize(**kwargs):
        calls.append(len(agent.state.context))
        assert len(agent.state.context) == 16
        agent.state.summary = '错误 P999999-S1' if len(calls) == 1 else '历史已记录，待核验 P0-S1'
        agent.state.context = agent.state.context[-6:]
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    try:
        result = await middleware.run(agent, force=True, next_handler=summarize)
        assert result['summary_changed'] and calls == [16,16]
        assert 'P999999' not in agent.state.summary and '历史证据归档引用：ctx_' in agent.state.summary
        assert governance(agent)['summary_attempts'] == [{'attempt':1,'status':'rejected','reason':'ValueError'},{'attempt':2,'status':'accepted'}]
        assert not await store.search('b','s',query='P999999')
        ref = await store.save('b','s','rejected_summary',{'candidate':'P999999'})
        result = await build_conversation_fact_lookup(store)(result_ref=ref)
        assert result.state == ToolResultState.ERROR
    finally:
        ShoppingContext.reset(token)


async def test_archived_product_and_current_working_are_valid_summary_sources(tmp_path):
    from tests.test_layered_context import fixture_agent, mark_consumed
    from app.infrastructure.context_governance import LayeredContextMiddleware, governance, set_output
    agent = fixture_agent()
    store = ContextEvidenceStore(tmp_path / 'e.db')
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh-CN', 'CNY'))
    try:
        ref = await store.save('b','s','products',{'hits':[{'product_id':'P8483','skus':[{'sku_id':'P8483-S1','stock':72}]}]})
        block = next(blocks(agent.state.context))[1]
        set_output(block, {'archived':True,'result_ref':ref})
        block.metadata['context_archive'] = ref
        governance(agent)['working'] = {'selected':['P8483-S1'],'constraints':{'budget':{'value':'352'}}}
        async def summarize(**kwargs):
            agent.state.summary = '已选择 P8483-S1，历史库存72；本次预算352元，当前仍待核验。'
            agent.state.context = agent.state.context[-6:]
        result = await LayeredContextMiddleware(store,target_tokens=10).run(agent,force=True,next_handler=summarize)
        assert result['summary_changed']
        assert governance(agent)['summary_attempts'] == [{'attempt':1,'status':'accepted'}]
        assert 'P8483-S1' in agent.state.summary
    finally:
        ShoppingContext.reset(token)
