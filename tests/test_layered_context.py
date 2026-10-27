"""真实 SDK 消息/状态与 SQLite 的上下文治理回归。"""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from agentscope.state import AgentState
from agentscope.message import UserMsg, Msg, ToolResultBlock, ToolCallBlock, ToolResultState, TextBlock
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import LayeredContextMiddleware, governance, protected_start, read_output, update_working_state
from app.infrastructure.context_products import business_view, product_page, token_estimate, result_identity
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore, ContextCheckpointRow, ContextOperationRow
from app.domain.session.ports.session_store import StaleSessionWrite, SessionOwnerMismatch
from app.application.tools.conversation_fact_lookup import build_conversation_fact_lookup

@pytest.fixture(autouse=True)
def scope():
    token=ShoppingContext.set(ShoppingContextSnapshot('s','b','zh-CN','CNY'))
    yield
    ShoppingContext.reset(token)


def fixture_agent(count=8):
    state=AgentState()
    for i in range(count):
        state.context.append(UserMsg('b',f'第{i+1}次搜索背包'))
        call=ToolCallBlock(id=f'call{i}',name='product_search_tool',input='{}')
        block=ToolResultBlock(id=f'call{i}',name='product_search_tool',state=ToolResultState.SUCCESS,
             output=json.dumps({'hits':[{'product_id':f'P{i}','title':'包','description':'商品限制'*100,'skus':[{'sku_id':f'P{i}-S1','stock':4}]}]},ensure_ascii=False))
        state.context.append(Msg(name='agent',content=[call,block],role='assistant'))
    async def count_tokens(messages,tools=None):return sum(token_estimate(m.model_dump()) for m in messages)//2
    agent=SimpleNamespace(state=state,name='agent',model=SimpleNamespace(model='test',context_size=128000,count_tokens=count_tokens))
    agent._prepare_model_input=AsyncMock(side_effect=lambda:{'messages':agent.state.context,'tools':[]})
    from app.application.agents.context_policy import build_context_config
    agent.context_config=build_context_config(128000,20000)
    return agent


def mark_consumed(agent):
    governance(agent)['consumed']={b.id:hashlib.sha256(str(b.output).encode()).hexdigest() for m in agent.state.context for b in m.get_content_blocks() if isinstance(b,ToolResultBlock)}


def test_entry_keeps_business_description_skus_and_tariff():
    raw={'hits':[{'product_id':'P1','description':'不可用于食品','image_url':'https://image','skus':[{'sku_id':'S1'},{'sku_id':'S2'}],'landed_price':{'tax':3}}]}
    view=business_view(raw)
    assert view['hits'][0]['description']=='不可用于食品'
    assert len(view['hits'][0]['skus'])==2 and view['hits'][0]['landed_price']['tax']==3
    assert 'image_url' not in view['hits'][0] and 'image_url' in raw['hits'][0]


def test_pagination_preserves_order_and_large_field_can_be_reassembled():
    data={'hits':[{'product_id':f'P{i}','description':'材料'*15000} for i in range(7)]}
    fragments=[];offset=0
    while True:
        page=product_page(data,limit=1,token_limit=3000,field_offset=offset)
        assert token_estimate(page)<3000
        fragments.append(page['fragment'])
        if page['next_field_offset'] is None:break
        offset=page['next_field_offset']
    assert json.loads(''.join(fragments))==data['hits'][0]
    assert page['next_offset']==1


async def test_never_prune_unread_or_latest_two_batches(tmp_path):
    agent=fixture_agent();mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),product_tokens=1)
    assert await mw.prune(agent)==0
    mark_consumed(agent)
    assert await mw.prune(agent)==5
    results=[b for m in agent.state.context for b in m.get_content_blocks() if isinstance(b,ToolResultBlock)]
    assert read_output(results[-1])['hits']
    assert read_output(results[0])['archived']
    assert results[0].id=='call0'


async def test_no_pressure_means_no_loss_and_selected_stays(tmp_path):
    agent=fixture_agent();mark_consumed(agent)
    mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),product_tokens=999999)
    assert await mw.prune(agent)==0
    governance(agent)['working']={'selected':['P0-S1']}
    mw.product_tokens=1
    await mw.prune(agent)
    assert read_output(agent.state.context[1].get_content_blocks()[1])['hits'][0]['product_id']=='P0'


async def test_failed_evidence_write_does_not_change_output(tmp_path):
    agent=fixture_agent();mark_consumed(agent);before=agent.state.model_dump_json()
    store=ContextEvidenceStore(tmp_path/'e.db');store.save=AsyncMock(side_effect=OSError('disk'))
    mw=LayeredContextMiddleware(store,product_tokens=1)
    report=await mw.run(agent)
    assert report['status']=='failed'
    assert 'context_archive' not in agent.state.model_dump_json()
    assert len(agent.state.context)==16


async def test_failed_model_response_never_marks_tool_read(tmp_path):
    agent=fixture_agent();mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'))
    async def source():
        yield SimpleNamespace(usage={},finished_reason='')
        raise RuntimeError('disconnect')
    response=await mw.on_model_call(agent,{'messages':agent.state.context,'tools':[]},AsyncMock(return_value=source()))
    with pytest.raises(RuntimeError):
        async for _ in response:pass
    assert not governance(agent).get('consumed')


async def test_successful_model_response_marks_exact_output_and_usage(tmp_path):
    agent=fixture_agent();mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'))
    response=SimpleNamespace(usage={'input_tokens':1234,'output_tokens':10},finished_reason='stop')
    await mw.on_model_call(agent,{'messages':agent.state.context,'tools':[]},AsyncMock(return_value=response))
    assert len(governance(agent)['consumed'])==8
    assert governance(agent)['last_model']['input_tokens']==1234


def test_tail_is_whole_turn_and_working_constraints_have_source():
    agent=fixture_agent()
    assert protected_start(agent)==10
    update_working_state(agent,[UserMsg('b','预算300元，寄到中国。必须防水，比较 P1001-S2 与 P1003-S1')])
    work=governance(agent)['working']
    assert work['constraints']['budget']['value']=='300'
    assert work['constraints']['destination']['value']=='CN'
    assert work['constraints']['budget']['message_id']
    assert work['comparisons']==['P1001-S2','P1003-S1']
    update_working_state(agent,[UserMsg('b','换个需求，买厨房餐盒')])
    assert governance(agent)['working']['constraints']=={}


async def test_summary_invalid_id_rolls_back_and_circuit_breaks(tmp_path):
    agent=fixture_agent();mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),target_tokens=10)
    async def wrong(**kwargs):
        agent.state.summary='选中P999999-S1'
        agent.state.context=agent.state.context[-2:]
    for _ in range(3):
        report=await mw.run(agent,next_handler=wrong)
        assert report['status']=='failed'
        assert len(agent.state.context)==16
    assert governance(agent)['failures']==3
    next_call=AsyncMock()
    await mw.run(agent,next_handler=next_call)
    next_call.assert_not_awaited()


async def test_batch_lookup_reopens_and_scopes_by_buyer(tmp_path):
    store=ContextEvidenceStore(tmp_path/'e.db')
    for ids in [['P1','P2'],['P3','P4']]:
        await store.save('b','s','display_batch',{'hits':[{'product_id':x} for x in ids]})
    fn=build_conversation_fact_lookup(ContextEvidenceStore(store.path))
    result=await fn(batch=1,position=2)
    assert json.loads(result.content[0].text)['records'][0]['data']['hits']==[{'product_id':'P2'}]
    other=ShoppingContext.set(ShoppingContextSnapshot('s','other','zh-CN','CNY'))
    try:
        result=await fn(batch=1)
        assert json.loads(result.content[0].text)['records']==[]
    finally:ShoppingContext.reset(other)


async def test_checkpoint_and_state_atomic_fenced_and_operation_idempotent(tmp_path):
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'db'))
    store=SqlFencedSessionStore(engine)
    try:
        claim=await store.claim('s',buyer_id='b')
        state=AgentState(middle_context={'shoppilot_context':{'checkpoint_id':'cp1','working':{'goal':'背包'}}})
        newer=await store.claim('s',buyer_id='b')
        with pytest.raises(StaleSessionWrite):await store.save_claim(claim,state.model_dump_json())
        async with engine.connect() as db:assert (await db.execute(select(ContextCheckpointRow))).first() is None
        saved=await store.save_claim(newer,state.model_dump_json())
        assert (await store.context_view('s','b'))['checkpoint_id']=='cp1'
        op,created=await store.create_context_operation('s','b','request',saved.revision)
        assert created
        assert (await store.create_context_operation('s','b','request',saved.revision))[1] is False
        with pytest.raises(StaleSessionWrite):await store.create_context_operation('s','b','request',saved.revision+1)
        with pytest.raises(SessionOwnerMismatch):await store.context_operation(op['operation_id'],'other')
        from sqlalchemy import update
        async with engine.begin() as db:await db.execute(update(ContextOperationRow).values(deadline=0))
        await store.recover_context_operations()
        assert (await store.context_operation(op['operation_id'],'b'))['status']=='interrupted'
    finally:await engine.dispose()

async def test_real_agentscope_summary_keeps_whole_tail_and_persists_checkpoint(tmp_path):
    from tests.test_native_memory_confirmation import Model
    from app.infrastructure.context_governance import ContextAwareAgent
    from agentscope.model import FinishedReason
    from app.application.agents.context_policy import build_context_config
    model=Model();model.context_size=128000
    source=fixture_agent().state
    model.generate_structured_output=AsyncMock(return_value=SimpleNamespace(content={
        'task_overview':'查询背包','current_state':'待比较','important_discoveries':'P0-S1',
        'next_steps':'核对最新库存','context_to_preserve':'历史不是授权'},finished_reason="stop",usage=None))
    agent=ContextAwareAgent('agent','测试',model,state=source,context_config=build_context_config(128000,20000))
    governance(agent)['calibration']={'identity':model.model+'|'+str(model.client.base_url),'factor':3,'ratios':[3]}
    mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),target_tokens=100)
    try:
        report=await mw.run(agent,force=True)
        assert report['summary_changed']
        assert len(agent.state.context)==6
        assert governance(agent)['checkpoint_id']
        assert model.generate_structured_output.await_count==1
        assert 'P0-S1' in agent.state.summary
    finally:await model.client.close()

@pytest.mark.parametrize('window',[128000,4096,64000])
async def test_manual_api_operation_survives_request_and_updates_state(tmp_path,window):
    import httpx
    from fastapi import FastAPI
    from app.presentation.context_workspace import register_context_routes
    from app.application.agents.context_service import ContextService
    from app.infrastructure.context_governance import ContextAwareAgent
    from tests.test_native_memory_confirmation import Model
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'manual.db'));store=SqlFencedSessionStore(engine)
    model=Model();model.context_size=window
    agent=ContextAwareAgent('agent','测试',model,state=fixture_agent(count=1).state)
    claim=await store.claim('s',buyer_id='b');await store.save_claim(claim,agent.state.model_dump_json())
    class Registry:
        async def invalidate(self,session):pass
        async def get_or_create(self,session):
            self.claim=await store.claim(session,buyer_id='b')
            if window==64000:
                from app.infrastructure.prompt_registry import PromptContractChanged
                raise PromptContractChanged('old contract')
            return agent
        async def persist(self,session):
            await store.save_claim(self.claim,agent.state.model_dump_json());return True
    orch=SimpleNamespace(_session_locks={},_sessions=Registry(),_session_lease_factory=None)
    service=ContextService(orch,store,ContextEvidenceStore(tmp_path/'e.db'))
    api=FastAPI();api.state.session_store=store;register_context_routes(api,lambda:service)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as client:
            original=(await client.get('/commerce/context?buyer_id=b&session_id=s')).json()
            response=await client.post('/commerce/context/compact',json={'buyer_id':'b','session_id':'s','request_id':'op','expected_revision':original['revision']})
            assert response.status_code==202,response.text
            op=response.json()['operation_id']
            await asyncio.gather(*list(service.tasks.values()))
            result=(await client.get(f'/commerce/context/operations/{op}?buyer_id=b')).json()
            assert result['status']==('noop' if window==128000 else 'failed'),result
            if window==4096:
                assert '安全容量' in result['message'] and '原始记录已保留' in result['message']
                assert result['error_code']=='ContextCapacityError'
            if window==64000:
                assert '继续选购' in result['message']
                assert result['error_code']=='PromptContractChanged'
            assert (await client.get(f'/commerce/context/operations/{op}?buyer_id=other')).status_code==403
            repeated=await client.post('/commerce/context/compact',json={'buyer_id':'b','session_id':'s','request_id':'op','expected_revision':original['revision']})
            assert repeated.json()['operation_id']==op
    finally:await service.shutdown();await model.client.close();await engine.dispose()

def test_skill_body_is_pinned_without_locking_entire_history():
    from app.infrastructure.context_governance import compression_parts
    agent=fixture_agent()
    call=ToolCallBlock(id='skill-call',name='load_capability',input='{}')
    result=ToolResultBlock(id='skill-call',name='load_capability',output='必须核验SKU，交易需要确认',state=ToolResultState.SUCCESS)
    agent.state.context.insert(1,Msg(name='agent',content=[call,result],role='assistant'))
    head,tail=compression_parts(agent)
    assert len(head)>=4
    assert all('必须核验SKU' not in (m.get_text_content() or '') for m in head)
    assert any(any(isinstance(b,ToolResultBlock) and b.id=='skill-call' for b in m.get_content_blocks()) for m in tail)
    assert any(m.name=='b' and '第6次' in m.get_text_content() for m in tail)

@pytest.mark.parametrize('change',[{'ship_to':'JP'},{'currency':'USD'},{'quantity':2}])
def test_quote_conditions_cannot_be_deduplicated_across_scopes(change):
    from copy import deepcopy
    first={'product_id':'P1','skus':[{'sku_id':'P1-S1'}],'landed_price':{'ship_to':'CN','currency':'CNY','quantity':1}}
    second=deepcopy(first);second['landed_price'].update(change)
    assert result_identity(first,{})!=result_identity(second,{})


def test_destination_change_keeps_literal_source():
    agent=fixture_agent()
    update_working_state(agent,[UserMsg('b','寄到中国'),UserMsg('b','收货国家改为日本JP')])
    field=governance(agent)['working']['constraints']['destination']
    assert field['value']=='JP' and field['source']=='收货国家改为日本'

def test_selection_and_exclusion_in_same_message_are_not_mixed():
    agent=fixture_agent()
    update_working_state(agent,[UserMsg('b','选中P1003-S1，不要下单。P1002太贵，淘汰。')])
    work=governance(agent)['working']
    assert work['selected']==['P1003-S1']
    assert work['excluded']==['P1002']
    assert '太贵' in work['exclusion_reasons']['P1002']['source']
    update_working_state(agent,[UserMsg('b','只是核对P1001-S1，不要下单')])
    assert governance(agent)['working']['selected']==['P1003-S1']

def test_working_view_keeps_material_capacity_and_latest_country_without_old_budget_clause():
    agent=fixture_agent()
    latest=UserMsg('b','预算改为180元，收货国家改为日本JP。',id='message-300-latest')
    update_working_state(agent,[UserMsg('b','预算300元，寄到中国，想买轻便背包。'),UserMsg('b','本次希望20到25升，排除真皮。'),latest])
    c=governance(agent)['working']['constraints']
    assert c['capacity']['source']=='本次希望20到25升'
    assert c['material_leather']['source']=='排除真皮'
    assert c['budget']['value']=='180' and c['destination']['value']=='JP'
    assert c['budget']['message_id']==latest.id
    # 只检查业务内容；来源 ID 可能包含“300”，不能被误判为旧预算残留。
    business_fields={name:{key:value for key,value in field.items() if key!='message_id'} for name,field in c.items()}
    assert '300' not in json.dumps(business_fields,ensure_ascii=False)


async def test_multiple_searches_in_recent_whole_turn_are_protected(tmp_path):
    agent=fixture_agent();mark_consumed(agent)
    # 最后3个买家轮次内有多批搜索，不只保护最后两批。
    agent.state.context=[m for i,m in enumerate(agent.state.context) if i not in (12,14)]
    mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),product_tokens=1)
    await mw.prune(agent)
    from app.infrastructure.context_governance import blocks
    outputs={b.id:read_output(b) for _,b in blocks(agent.state.context)}
    assert all('hits' in outputs[f'call{i}'] for i in range(3,8))
    assert outputs['call0']['archived']


async def test_selected_duplicate_observations_archive_but_latest_remains(tmp_path):
    agent=fixture_agent()
    from app.infrastructure.context_governance import blocks, set_output
    for _,block in blocks(agent.state.context):
        set_output(block,{'hits':[{'product_id':'P1','skus':[{'sku_id':'P1-S1'}]}],'query_conditions':{'ship_to':'JP','currency':'CNY','quantity':1}})
    mark_consumed(agent);governance(agent)['working']={'selected':['P1-S1']}
    mw=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),product_tokens=1)
    assert await mw.prune(agent)==5
    assert 'hits' in read_output(list(blocks(agent.state.context))[-1][1])


def test_quantity_and_capacity_limits_do_not_overwrite_money_budget():
    agent=fixture_agent()
    update_working_state(agent,[UserMsg('b','预算180元，寄到日本')])
    update_working_state(agent,[UserMsg('b','最多2件，容量不超过25升')])
    assert governance(agent)['working']['constraints']['budget']['value']=='180'


def test_query_currency_and_direct_sku_are_part_of_quote_key():
    a={'product_id':'P1','sku_id':'P1-S1'}
    assert result_identity(a,{'currency':'CNY'})!=result_identity(a,{'currency':'USD'})
    assert result_identity(a,{})!=result_identity({**a,'sku_id':'P1-S2'}, {})

@pytest.mark.parametrize('sentence', [
    '选中P1003-S1，排除P1002',
    '选中P1003-S1, 不选P1002',
    '排除P1002，选择P1003-S1',
])
def test_comma_separates_opposite_product_decisions(sentence):
    agent=fixture_agent()
    update_working_state(agent,[UserMsg('b',sentence)])
    work=governance(agent)['working']
    assert work['selected']==['P1003-S1']
    assert work['excluded']==['P1002']


def test_comparison_comma_list_remains_complete():
    agent=fixture_agent()
    update_working_state(agent,[UserMsg('b','比较P1001-S1，P1002-S1和P1003-S2，排除P1004')])
    work=governance(agent)['working']
    assert work['comparisons']==['P1001-S1','P1002-S1','P1003-S2']
    assert work['excluded']==['P1004']


def test_explicit_reselection_replaces_previous_selection():
    agent=fixture_agent()
    update_working_state(agent,[UserMsg('b','选中P1001-S1'),UserMsg('b','改选P1003-S2')])
    assert governance(agent)['working']['selected']==['P1003-S2']


@pytest.mark.parametrize('text,expected', [
    ('第一批 P1003 的到手价多少？', 1), ('第12轮第三件的历史价格？', 12),
    ('第十一批的运费', 11), ('第二十批的报价', 20),
    ('第一批和第二批对比', None), ('不要第一批，查最新的', None),
    ('第一批的当前库存', None), ('第0批', None), ('查这个商品', None),
])
def test_explicit_lookup_batch_is_conservative(text, expected):
    from app.application.tools.conversation_fact_lookup import explicit_lookup_batch
    assert explicit_lookup_batch(text) == expected


async def test_reply_scopes_omitted_lookup_batch_without_changing_history(tmp_path):
    from app.application.tools.conversation_fact_lookup import lookup_batch_scope
    store = ContextEvidenceStore(tmp_path/'batch.db')
    refs = []
    for price in (222, 237):
        refs.append(await store.save('b', 's', 'display_batch', {'hits': [{
            'product_id': 'P1003', 'price_major': price, 'currency': 'CNY'}]}))
    lookup = build_conversation_fact_lookup(store)
    agent = fixture_agent(0)
    middleware = LayeredContextMiddleware(store)
    user = UserMsg('b', content=[TextBlock(text='第一批 P1003 的报价？'),
                                TextBlock(text='服务端附件中的第二批不应参与解析')])
    before = user.model_dump_json()
    async def record(**kwargs):
        inferred = json.loads((await lookup(product_id='P1003')).content[0].text)['records'][0]
        assert inferred['data']['hits'][0]['price_major'] == 222
        assert inferred['observation_scope']['effective_batch'] == 1
        assert inferred['observation_scope']['batch_inferred_from_user']
        # 显式工具参数和证据引用优先，不能偷改参数含义。
        for arguments in ({'batch': 2}, {'result_ref': refs[1]}):
            explicit = json.loads((await lookup(**arguments)).content[0].text)['records'][0]
            assert explicit['data']['hits'][0]['price_major'] == 237
            assert not explicit['observation_scope']['batch_inferred_from_user']
        yield 'done'
    assert [x async for x in middleware.on_reply(agent, {'inputs': user}, record)] == ['done']
    assert user.model_dump_json() == before
    assert lookup_batch_scope.get() is None
    latest = json.loads((await lookup(product_id='P1003')).content[0].text)['records'][0]
    assert latest['data']['hits'][0]['price_major'] == 237


async def test_batch_scope_rejects_other_owner_and_resets_after_failure(tmp_path):
    from app.application.tools.conversation_fact_lookup import lookup_batch_scope
    store = ContextEvidenceStore(tmp_path/'owner.db')
    for buyer, session in [('b', 's'), ('other', 's'), ('b', 'other-session')]:
        for price in (10, 20):
            await store.save(buyer, session, 'display_batch', {'hits': [{'product_id': 'P1', 'price_major': price}]})
    lookup = build_conversation_fact_lookup(store)
    async def interrupted(**kwargs):
        for buyer, session in [('other', 's'), ('b', 'other-session')]:
            token = ShoppingContext.set(ShoppingContextSnapshot(session, buyer, 'zh-CN', 'CNY'))
            try:
                row = json.loads((await lookup(product_id='P1')).content[0].text)['records'][0]
                assert row['data']['hits'][0]['price_major'] == 20
                assert not row['observation_scope']['batch_inferred_from_user']
            finally:
                ShoppingContext.reset(token)
        raise RuntimeError('模拟工具链中断')
        yield
    with pytest.raises(RuntimeError, match='模拟工具链中断'):
        async for _ in LayeredContextMiddleware(store).on_reply(
                fixture_agent(0), {'inputs': UserMsg('b', '第一批的报价')}, interrupted):
            pass
    assert lookup_batch_scope.get() is None
