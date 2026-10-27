"""历史证据提取在真实SQLite、SDK消息和原生模型中间件上验证。"""
import json
from copy import deepcopy
from types import SimpleNamespace
import pytest
from agentscope.message import Msg, UserMsg, SystemMsg, ToolCallBlock, ToolResultBlock, ToolCallState, ToolResultState
from agentscope.state import AgentState
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.infrastructure.visible_evidence import visible_answer_request, historical_batch_query
from app.infrastructure.context_governance import LayeredContextMiddleware

QUERY = '请从第一批列出所有蓝色规格的SKU、历史单价和原报价币种。'

async def fixture(tmp_path):
    store = ContextEvidenceStore(tmp_path/'context_evidence.db')
    hits = [{'product_id':'P9001','title':'测试包','skus':[
        {'sku_id':'P9001-S1','spec':'蓝色','price_major':31,'currency':'USD','stock':3},
        {'sku_id':'P9001-S2','spec':'红色','price_major':40,'currency':'EUR','stock':8}]}]
    payload = {'hits':hits,'observed_at':'2026-09-10T01:00:00Z','query_conditions':{'ship_to':'JP'}}
    ref = await store.save('b','s','products',payload)
    payload['result_ref'] = ref
    await store.save('b','s','display_batch',payload)
    block = ToolResultBlock(id='c',name='product_search_tool',output=json.dumps(payload),state=ToolResultState.SUCCESS)
    messages = [SystemMsg('system','买家原约束不得更改'),UserMsg('b','只看看商品'),
        Msg(name='agent',role='assistant',content=[ToolCallBlock(id='c',name=block.name,input='{}',state=ToolCallState.FINISHED),block]),
        UserMsg('b',QUERY)]
    return store,messages,block

@pytest.fixture(autouse=True)
def scope():
    token = ShoppingContext.set(ShoppingContextSnapshot('s','b','zh-CN','CNY'))
    yield
    ShoppingContext.reset(token)

@pytest.mark.parametrize('text',[
    '第一批现在还有哪些蓝色SKU？','查第一批历史SKU，然后下单',
    '回顾第一批与第二批历史规格','记住第一批历史SKU的偏好',
    '列出历史SKU','第一批历史SKU的材质','确认第一批历史SKU',
    '第一批历史SKU的到手价','第0批历史规格','第999999批历史SKU',
    '把第一批历史SKU发给朋友','收藏第一批历史规格',
])
def test_mixed_or_ambiguous_requests_do_not_disable_tools(text):
    assert historical_batch_query(text) is None

@pytest.mark.parametrize('text,number',[(QUERY,1),('第十二批当时规格价格',12),('第21轮历史SKU',21)])
def test_batch_parser_is_not_tied_to_black_sku_fixture(text,number):
    assert historical_batch_query(text) == number

async def test_full_evidence_uses_no_tools_preserves_values_and_state(tmp_path):
    store,messages,_ = await fixture(tmp_path)
    before = [m.model_dump_json() for m in messages]
    result = await visible_answer_request(SimpleNamespace(),messages,store)
    assert result and result['tools'] == [] and result['tool_choice'].mode == 'none'
    assert [m.model_dump_json() for m in messages] == before
    assert messages[0] in result['messages'] and messages[-1] in result['messages']
    data = json.loads(next(m.get_text_content() for m in result['messages'] if m.name=='visible_evidence'))
    assert [(s['sku_id'],s['price_major'],s['currency']) for s in data['hits'][0]['skus']] == [('P9001-S1',31,'USD'),('P9001-S2',40,'EUR')]
    assert not any(isinstance(b,(ToolCallBlock,ToolResultBlock)) for m in result['messages'] for b in m.get_content_blocks())


async def test_historical_skill_does_not_disable_fast_path_but_current_activation_does(tmp_path):
    store, messages, _ = await fixture(tmp_path)
    reference = UserMsg('selected_skill_reference', '历史方案仅供原轮使用')
    messages.insert(1, reference)
    agent = SimpleNamespace(state=AgentState(middle_context={'skill_catalog': {
        'mode': 'append_only', 'active_reference_ids': []}}))
    assert await visible_answer_request(agent, messages, store) is not None
    agent.state.middle_context['skill_catalog']['active_reference_ids'] = [reference.id]
    assert await visible_answer_request(agent, messages, store) is None

@pytest.mark.parametrize('suffix', [
    '以及商家评分', '运费是多少', '适合哪些人群', '什么时候补货',
    '; then email it to me', '并推荐类似款', '还有保障条款',
])
async def test_unknown_or_uncovered_fields_keep_tools(tmp_path,suffix):
    store,messages,_ = await fixture(tmp_path)
    messages[-1]=UserMsg('b',QUERY+suffix)
    assert await visible_answer_request(SimpleNamespace(),messages,store) is None

@pytest.mark.parametrize('change', ['archive','partial_skus','missing_price','wrong_ref','wrong_order','page','time','pending','skill','foreign_id'])
async def test_incomplete_wrong_scope_and_pending_keep_normal_agent(tmp_path,change):
    store,messages,block = await fixture(tmp_path)
    data = json.loads(block.output)
    if change=='archive':data={'result_ref':data['result_ref'],'archived':True}
    elif change=='partial_skus':data['hits'][0]['skus'].pop()
    elif change=='missing_price':data['hits'][0]['skus'][0].pop('price_major')
    elif change=='wrong_ref':data['result_ref']='ctx_other'
    elif change=='wrong_order':data['hits'][0]['skus'].reverse()
    elif change=='page':data['next_offset']=1
    elif change=='time':data.pop('observed_at')
    elif change=='pending':messages[-2].content.append(ToolCallBlock(id='pending',name='order',input='{}'))
    elif change=='skill':messages.insert(1,UserMsg('selected_skill_reference','规则'))
    elif change=='foreign_id':messages[-1]=UserMsg('b',QUERY+'也列出P9999-S1')
    block.output=json.dumps(data)
    assert await visible_answer_request(SimpleNamespace(),messages,store) is None

async def test_locator_is_buyer_session_scoped_and_contains_no_prices(tmp_path):
    store,_,_ = await fixture(tmp_path)
    locator = await store.batch_locator('b','s',1)
    assert locator['products']==[{'product_id':'P9001','sku_ids':['P9001-S1','P9001-S2']}]
    assert set(locator)=={'source_ref','products'}
    assert all(set(p)=={'product_id','sku_ids'} for p in locator['products'])
    assert await store.batch_locator('other','s',1) is None
    assert await store.batch_locator('b','other',1) is None

async def test_native_middleware_passes_none_and_preserves_persistent_state(tmp_path):
    store,messages,_ = await fixture(tmp_path)
    async def count_tokens(**kwargs):return 100
    agent=SimpleNamespace(state=AgentState(context=messages),model=SimpleNamespace(
        model='test',context_size=128000,count_tokens=count_tokens,parameters=None))
    mw=LayeredContextMiddleware(store)
    received=[]
    async def next_handler(**kwargs):
        received.append(kwargs)
        return SimpleNamespace(usage={},finished_reason='stop')
    await mw.on_model_call(agent,{'messages':messages,'tools':[{'function':{'name':'lookup'}}]},next_handler)
    assert received[0]['tools']==[] and received[0]['tool_choice'].mode=='none'
    assert any(isinstance(b,ToolResultBlock) for m in agent.state.context for b in m.get_content_blocks())

async def test_production_main_factory_restores_history_and_hides_tools(tmp_path):
    import httpx
    from dataclasses import replace
    from app.application.agents.main_agent import MainAgentFactory
    from app.application.agents.search_agent import SearchAgentFactory
    from app.application.agents.trade_agent import TradeAgentFactory
    from app.infrastructure.eventbus import TradeEventBus
    from app.infrastructure.resilience import CircuitBreakerRegistry
    from app.infrastructure.throttle import GatewayThrottle
    from tests.test_retrieval import _settings
    from tests.test_prompt_cache import client_model, completion
    _,messages,_ = await fixture(tmp_path)
    settings=replace(_settings(tmp_path),context_strategy='layered',harness_enabled=True,
                     llm_api_key='test-key',llm_base_url='https://test.invalid/v1',llm_model='fixture')
    bus,circuit,throttle=TradeEventBus(),CircuitBreakerRegistry(),GatewayThrottle(1,0)
    search=SearchAgentFactory(settings,None,bus,None,circuit,throttle)
    trade=TradeAgentFactory(settings,None,None,None,bus,circuit,throttle)
    factory=MainAgentFactory(settings,search,trade,bus,None,circuit,throttle)
    state=AgentState(context=messages[1:-1])
    agent=factory.build(AgentState.model_validate_json(state.model_dump_json()))
    await agent.model.client.close()
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        assert body.get('tool_choice')=='none' and not body.get('tools')
        raw=completion();raw['choices'][0]['message']['content']='P9001-S1，历史单价31 USD。'
        return httpx.Response(200,json=raw)
    model=await client_model(tmp_path,handler)
    agent.model=model
    try:
        result=await agent.reply(UserMsg('b',QUERY))
        assert '31 USD' in result.get_text_content() and len(requests)==1
        assert search._loop_detector is factory._loop_detector is trade._loop_detector
        assert any(isinstance(b,ToolResultBlock) for m in agent.state.context for b in m.get_content_blocks())
    finally:await model.client.close()


# 当前核验必须实际取证，不能仅因碰巧答对历史数值而视为成功。
@pytest.mark.parametrize("query", [
    "重新核对 P9001-S1 当前库存。",
    "请重新调用商品工具查询 P9001-S1 当前单价和库存，不要下单。",
])
def test_explicit_current_recheck_requires_native_read_tool(query):
    from app.infrastructure.visible_evidence import explicit_current_product_choice
    messages = [UserMsg("b", query)]
    agent = SimpleNamespace(name="agent", state=AgentState(context=messages))
    tools = [{"type": "function", "function": {"name": "product_search_tool",
        "parameters": {"type": "object", "properties": {"sku_id": {"type": "string"}}}}}]
    before = [m.model_dump_json() for m in messages]
    choice = explicit_current_product_choice(agent, messages, tools)
    assert choice.mode == "product_search_tool" and choice.tools is None
    assert [m.model_dump_json() for m in messages] == before


@pytest.mark.parametrize("query", [
    "重新核对 P9001-S1 第一批的历史库存。",
    "P9001-S1 当前库存是多少？",
    "重新核对这些商品的当前库存。",
    "重新核对 P9001-S1 和 P9002-S1 当前库存。",
    "不要重新查询 P9001-S1 当前库存。",
    "无需调用工具，重新核对 P9001-S1 当前库存。",
])
def test_nonexplicit_or_ambiguous_request_keeps_agent_decision(query):
    from app.infrastructure.visible_evidence import explicit_current_product_choice
    messages = [UserMsg("b", query)]
    agent = SimpleNamespace(name="agent", state=AgentState(context=messages))
    tools = [{"type": "function", "function": {"name": "product_search_tool",
        "parameters": {"type": "object", "properties": {"sku_id": {"type": "string"}}}}}]
    assert explicit_current_product_choice(agent, messages, tools) is None


def test_fresh_choice_preserves_denial_approval_and_stops_after_attempt():
    from agentscope.tool import ToolChoice
    from app.infrastructure.visible_evidence import explicit_current_product_choice
    query = UserMsg("b", "重新核对 P9001-S1 当前库存。")
    tools = [{"type": "function", "function": {"name": "product_search_tool",
        "parameters": {"type": "object", "properties": {"sku_id": {"type": "string"}}}}}]
    messages = [query]
    agent = SimpleNamespace(name="agent", state=AgentState(context=messages))
    assert explicit_current_product_choice(agent, messages, []) is None
    historical_tool = [{"type": "function", "function": {"name": "product_search_tool", "parameters": {"properties": {"batch": {"type": "integer"}}}}}]
    assert explicit_current_product_choice(agent, messages, historical_tool) is None
    assert explicit_current_product_choice(agent, messages, tools, ToolChoice(mode="none")) is None
    for allowed in ([], ["other_tool"], ["product_search_tool"]):
        assert explicit_current_product_choice(agent, messages, tools, ToolChoice(mode="auto", tools=allowed)) is None
    pending = Msg(name="agent", role="assistant", content=[
        ToolCallBlock(id="ask", name="order_create_tool", input="{}", state=ToolCallState.ASKING)])
    agent.state.context.append(pending)
    assert explicit_current_product_choice(agent, agent.state.context, tools) is None
    agent.state.context[-1] = Msg(name="agent", role="assistant", content=[
        ToolCallBlock(id="read", name="product_search_tool", input="{}", state=ToolCallState.FINISHED),
        ToolResultBlock(id="read", name="product_search_tool", output="[error]", state=ToolResultState.ERROR)])
    assert explicit_current_product_choice(agent, agent.state.context, tools) is None


@pytest.mark.parametrize("stream", [False, True])
async def test_native_recheck_forces_first_call_then_releases_without_schema_changes(tmp_path, stream):
    import httpx
    from agentscope.tool import FunctionTool, Toolkit, ToolChunk
    from agentscope.message import TextBlock
    from app.infrastructure.context_governance import ContextAwareAgent
    from tests.test_prompt_cache import client_model, completion, streaming
    requests, executions = [], []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        result = completion()
        if len(requests) == 1:
            assert payload["stream"] is False
            assert payload["tool_choice"] == {"type": "function", "function": {"name": "product_search_tool"}}
            result["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "fresh-read", "type": "function", "function": {
                    "name": "product_search_tool", "arguments": '{"sku_id":"P9001-S1"}'}}]}
            result["choices"][0]["finish_reason"] = "tool_calls"
        else:
            assert payload.get("tool_choice") in (None, "auto")
        return streaming(result) if payload.get("stream") else httpx.Response(200, json=result)
    async def product_search_tool(sku_id: str) -> ToolChunk:
        executions.append(sku_id)
        return ToolChunk(content=[TextBlock(text='{"sku_id":"P9001-S1","stock":3}')])
    model = await client_model(tmp_path, handler, stream=stream)
    try:
        agent = ContextAwareAgent("agent", "只读核验", model,
            toolkit=Toolkit(tools=[FunctionTool(product_search_tool, is_read_only=True)]),
            middlewares=[LayeredContextMiddleware(None)])
        await agent.reply(UserMsg("b", "重新核对 P9001-S1 当前库存。"))
        assert executions == ["P9001-S1"]
        assert model.stream is stream and requests[1]["stream"] is stream
        assert len(requests) == 2 and requests[0]["tools"] == requests[1]["tools"]
        history = [m.model_dump_json() for m in agent.state.context]
        await agent.reply(UserMsg("b", "谢谢"))
        assert executions == ["P9001-S1"]
        assert [m.model_dump_json() for m in agent.state.context[:len(history)]] == history
    finally:
        await model.client.close()
