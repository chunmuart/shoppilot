"""布局在原生 SDK、审批恢复与最终请求边界验证，不请求外部模型。"""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest
from agentscope.message import UserMsg, Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import ContextAwareAgent, LayeredContextMiddleware, governance, update_working_state, project_working_hint
from app.infrastructure.context_usage import context_usage_sink
from app.infrastructure.prompt_cache import PrefixTracker
from tests.test_prompt_cache import client_model, completion, streaming
from tests.test_harness_evaluation import manifest, rows
from scripts.eval.harness.metrics import summarize
from scripts.eval.harness.contracts import load_suite, ROOT


@pytest.mark.asyncio
async def test_native_reply_preserves_static_prefix_latest_budget_and_restore(tmp_path):
    requests=[];samples=[]
    def handler(request):
        requests.append(json.loads(request.content));return httpx.Response(200,json=completion())
    model=await client_model(tmp_path,handler)
    mw=LayeredContextMiddleware(None,prompt_layout='stable_prefix')
    def build(state=None):return ContextAwareAgent('agent','固定规则',model,toolkit=Toolkit(),middlewares=[mw],state=state)
    ctx=ShoppingContext.set(ShoppingContextSnapshot('s','b','zh-CN','CNY'));sink=context_usage_sink.set(samples.append)
    try:
        agent=build()
        await agent.reply(UserMsg('b','本次预算300元，寄到中国。'))
        await agent.reply(UserMsg('b','预算改为180元，选中P1003-S1。'))
        assert requests[0]['messages'][0] == requests[1]['messages'][0]
        assert '180' in json.dumps(requests[1]['messages'][-1]['content'])
        assert len([m for m in agent.state.context if m.name=='shopping_state'])==2
        assert governance(agent)['working']['constraints']['budget']['value']=='180'
        assert samples[1]['prompt_cache']['prefix_system_changed'] is False
        assert samples[1]['prompt_cache']['prefix_comparison']=='append'
        restored=build(AgentState.model_validate_json(agent.state.model_dump_json()))
        before=await restored._prepare_model_input()
        assert '180' in next(m.get_text_content() for m in reversed(before['messages']) if m.name=='shopping_state')
        await restored.reply(UserMsg('b','换个需求，现在想买耳机。'))
        assert governance(restored)['working']['constraints']=={}
        assert requests[-1]['messages'][0]==requests[0]['messages'][0]
    finally:
        context_usage_sink.reset(sink);ShoppingContext.reset(ctx);await model.client.close()


@pytest.mark.asyncio
async def test_legacy_restore_projection_anchors_before_tool_pairs_without_mutating_history(tmp_path):
    model=await client_model(tmp_path,lambda _:httpx.Response(200,json=completion()))
    mw=LayeredContextMiddleware(None,prompt_layout='stable_prefix')
    agent=ContextAwareAgent('agent','规则',model,toolkit=Toolkit(),middlewares=[mw])
    ctx=ShoppingContext.set(ShoppingContextSnapshot('s','b','zh-CN','CNY'))
    try:
        user=UserMsg('b','预算180元');agent.state.context.append(user);update_working_state(agent,[user])
        agent.state.context.append(Msg(name='agent',content=[ToolCallBlock(id='a',name='order',input='{}')],role='assistant'))
        saved=agent.state.model_dump_json()
        first=await agent._prepare_model_input()
        assert [m.name for m in first['messages']]==['system','b','shopping_state','agent']
        assert agent.state.model_dump_json()==saved
        agent.state.context.append(Msg(name='agent',content=[ToolResultBlock(id='a',name='order',output='rejected',state=ToolResultState.SUCCESS)],role='assistant'))
        second=await agent._prepare_model_input()
        assert [m.get_text_content() for m in first['messages'][:3]]==[m.get_text_content() for m in second['messages'][:3]]
        # 若摘要已移走原始消息，回查快照位于消息列表开头，不插入工具对中间。
        remaining=second['messages'][:1]+second['messages'][3:]
        projected=project_working_hint(remaining,governance(agent)['working'])
        assert projected[1].name=='shopping_state' and projected[2:] == remaining[1:]
    finally:ShoppingContext.reset(ctx);await model.client.close()


def test_prefix_diagnostics_separate_buyer_kind_tools_and_rewrites():
    t=PrefixTracker(capacity=2)
    request={'model':'test','messages':[{'role':'system','content':'规则'}, {'role':'user','content':'私密预算180'}], 'tools':[]}
    original=deepcopy(request)
    first=t.observe(request,scope=('buyer-a','s'),kind='business')
    assert first['prefix_comparison']=='first_request'
    appended=deepcopy(request);appended['messages'].append({'role':'assistant','content':'OK'})
    second=t.observe(appended,scope=('buyer-a','s'),kind='business')
    assert second['prefix_comparison']=='append' and second['prefix_common_messages']==2
    changed=deepcopy(appended);changed['messages'][0]['content']='新规则';changed['tools']=[{'function':'lookup'}]
    third=t.observe(changed,scope=('buyer-a','s'),kind='business')
    assert third['prefix_system_changed'] and third['prefix_tools_changed'] and third['prefix_first_changed_message']==0
    assert t.observe(request,scope=('buyer-b','s'),kind='business')['prefix_comparison']=='first_request'
    assert t.observe(request,scope=('buyer-b','s'),kind='summary')['prefix_comparison']=='first_request'
    assert t.observe(request,scope=None,kind='business')['prefix_comparison']=='first_request'
    assert request==original and '私密预算180' not in repr(t.previous) and 'buyer-a' not in json.dumps(third)
    assert len(t.previous)==2


def test_cache_markers_do_not_masquerade_as_content_changes():
    t=PrefixTracker();plain={'messages':[{'role':'system','content':'规则'}]}
    t.observe(plain,scope=('a','s'),kind='business')
    marked={'messages':[{'role':'system','content':[{'type':'text','text':'规则','cache_control':{'type':'ephemeral'}}]}]}
    assert t.observe(marked,scope=('a','s'),kind='business')['prefix_comparison']=='identical'


def test_cache_layout_suite_only_changes_layout_and_cache():
    suite=load_suite(ROOT/'eval/harness/v1/cache-layout-suite.json')
    strategies=suite['strategies']
    assert len(strategies)==4
    assert {s['overrides']['context_product_tokens'] for s in strategies.values()}=={6000}
    assert {s['overrides']['context_target_tokens'] for s in strategies.values()}=={48000}
    assert suite['gates']['objective']=='prompt_cache'
    assert suite['profiles']['cache_probe']['repetitions']==2
    with pytest.raises(ValueError):LayeredContextMiddleware(None,prompt_layout='typo')


def test_cache_goal_requires_money_not_token_shrinkage():
    m=manifest();m['gates']['objective']='prompt_cache'
    data=rows()
    for row in data:
        row['usage'][0]['input_tokens']=100
        row['usage'][0]['prompt_cache']['reported_cost']=2 if row['strategy']=='current' else 1
    blocked=summarize(m,data)['gates']['candidate']
    assert blocked['status']=='BLOCKED' and not any('输入降幅' in r for r in blocked['reasons'])
    m['pricing']={'verified':True,'currency':'TEST_ONLY','source':'unit_test','verified_at':'fixture'}
    assert summarize(m,data)['gates']['candidate']['status']=='BENEFIT_VERIFIED'
    data[-1]['usage'][0]['prompt_cache']['reported_cost']=None
    assert summarize(m,data)['gates']['candidate']['status']=='BLOCKED'


@pytest.mark.asyncio
async def test_fixed_trace_uses_real_sdk_and_counts_streaming_usage(tmp_path,monkeypatch):
    from scripts.eval.harness import cache_replay
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        raw=completion();reply={'budget_major':300 if len(requests)==1 else 180,'sku_id':'P1003-S1'}
        chunks=[{'id':'s','object':'chat.completion.chunk','created':1,'model':'fixture','choices':[{'index':0,'delta':{'content':json.dumps(reply)},'finish_reason':None}]},
                {'id':'s','object':'chat.completion.chunk','created':1,'model':'fixture','choices':[],'usage':raw['usage']}]
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
    model=await client_model(tmp_path,handler,stream=True)
    monkeypatch.setattr(cache_replay,'create_chat_model',lambda *a,**k:model)
    from tests.test_retrieval import _settings
    case={'id':'cache-budget','scenario':'budget','budgets':[300,180]}
    row=await cache_replay.run_cache_replay(case,replace(_settings(tmp_path),context_prompt_layout='stable_prefix'),None,tmp_path,0,None,
        {'output_limit':128,'request_timeout_seconds':5})
    assert row['passed'] and len(row['usage'])==2, json.dumps(row,ensure_ascii=False)
    assert all(u['ttft_ms'] is not None for u in row['usage'])
    assert requests[1]['tool_choice']=='none'
    assert row['usage'][1]['prompt_cache']['prefix_system_changed'] is False


@pytest.mark.parametrize('text,fmt,facts',[
    ('{"budget_major":180,"sku_id":"P1003-S1","stock":80}',True,True),
    ('```json\n{"budget_major":180,"sku_id":"P1003-S1"}\n```',True,True),
    ('{"budget_major":180,"sku_id":"P1003-S1"} 额外说明',False,True),
    ('{"budget_major":300,"sku_id":"P1003-S1"}',True,False),
    ('{"budget_major":180,"sku_id":"P1003-S2"}',True,False),
    ('{"budget_major":"180","sku_id":"P1003-S1"}',True,False),
])
def test_replay_facts_and_json_format_have_separate_contracts(text,fmt,facts):
    from scripts.eval.harness.cache_replay import check_replay_answer
    assert check_replay_answer(text,180)=={'json_object':fmt,'budget_and_sku':facts}
