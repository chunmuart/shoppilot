"""选购表单必须按买家持久化，提交只形成需求，不产生交易或长期记忆。"""

import importlib.util
import asyncio
import json
import pytest
from fastapi import FastAPI
import httpx


def store(path):
    assert importlib.util.find_spec("app.infrastructure.shopping_forms"), (
        "尚未实现持久选购表单"
    )
    from app.infrastructure.shopping_forms import ShoppingFormStore

    return ShoppingFormStore(path)


async def test_form_restores_after_restart_and_is_owner_scoped(tmp_path):
    forms = store(tmp_path / "forms.db")
    form = await forms.create(
        "b", "s", "选购背包", ["query", "budget", "ship_to"], {"query": "背包"}
    )
    restored = await store(tmp_path / "forms.db").get("b", "s", form["form_id"])
    assert restored == form
    assert form["messages"][0]["createSurface"]["catalogId"].endswith("/shopping-v2")
    with pytest.raises(LookupError):
        await forms.get("other", "s", form["form_id"])
    with pytest.raises(LookupError):
        await forms.get("b", "other", form["form_id"])


async def test_form_submission_has_cas_and_idempotent_recovery(tmp_path):
    forms = store(tmp_path / "forms.db")
    form = await forms.create(
        "b", "s", "选购", ["query", "budget", "ship_to", "currency"], {}
    )
    values = {"query": "轻便背包", "budget": 300, "ship_to": "JP", "currency": "CNY"}
    result = await forms.submit("b", "s", form["form_id"], 1, "action-1", values)
    assert "商品单价不超过300 CNY" in result["query"]
    assert "JP" in result["query"] and result["status"] == "submitted"
    assert (
        await forms.submit("b", "s", form["form_id"], 1, "action-1", values) == result
    )
    from app.infrastructure.shopping_forms import FormConflict

    with pytest.raises(FormConflict):
        await forms.submit(
            "b", "s", form["form_id"], 1, "action-1", {**values, "budget": 400}
        )
    with pytest.raises(FormConflict):
        await forms.submit("b", "s", form["form_id"], 1, "action-2", values)
    assert (await store(tmp_path / "forms.db").get("b", "s", form["form_id"]))[
        "submission"
    ] == result


async def test_form_rejects_unknown_components_actions_and_invalid_numbers(tmp_path):
    forms = store(tmp_path / "forms.db")
    with pytest.raises(ValueError):
        await forms.create("b", "s", "攻击", ["html"], {})
    form = await forms.create("b", "s", "选购", ["query", "budget"], {})
    for value in [float("nan"), float("inf"), -1, True, 1000001]:
        with pytest.raises(ValueError):
            await forms.submit(
                "b",
                "s",
                form["form_id"],
                1,
                "request",
                {"query": "包", "budget": value},
            )
    with pytest.raises(ValueError):
        await forms.submit(
            "b",
            "s",
            form["form_id"],
            1,
            "request",
            {"query": "包", "tool": "create_order_tool"},
        )


async def test_form_routes_validate_actions_and_ownership(tmp_path):
    forms = store(tmp_path / "forms.db")
    from app.presentation.shopping_forms import register_shopping_form_routes

    api = FastAPI()
    register_shopping_form_routes(api, lambda: forms)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as c:
        result = await c.post(
            "/commerce/shopping-forms?buyer_id=b", json={"session_id": "s"}
        )
        assert result.status_code == 410
        form = await forms.create_clarification("b", "s", "补充需求", [
            {"id": "query", "type": "text", "label": "这次想买什么？", "required": True},
            {"id": "budget", "type": "number", "label": "预算是多少？", "unit": "CNY"},
        ])
        path = "/commerce/shopping-forms/" + form["form_id"]
        assert (await c.get(path + "?buyer_id=other&session_id=s")).status_code == 404
        payload = {
            "session_id": "s",
            "expected_revision": 1,
            "request_id": "r",
            "action": {
                "name": "applyShoppingRequirements",
                "surfaceId": form["form_id"],
                "sourceComponentId": "root",
                "timestamp": "2026-09-18T00:00:00Z",
                "context": {"query": "轻便背包", "budget": 300},
            },
        }
        assert (
            await c.post(path + "/actions?buyer_id=b", json=payload)
        ).status_code == 200
        payload["action"]["name"] = "create_order_tool"
        assert (
            await c.post(path + "/actions?buyer_id=b", json=payload)
        ).status_code == 422


async def test_agent_surface_is_projected_to_agui_and_replay_state(tmp_path):
    forms = store(tmp_path / "forms.db")
    from tests.test_ag_ui import request_data
    from ag_ui.core import RunAgentInput
    from app.application.agents.ag_ui_adapter import AGUIRunAdapter
    from app.application.tools.shopping_form_tool import build_shopping_form_tool
    from app.infrastructure.eventbus import TradeEventBus, observe_run_events
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot

    events = []
    adapter = AGUIRunAdapter(RunAgentInput(**request_data()), events.append)
    token = ShoppingContext.set(
        ShoppingContextSnapshot("session-test", "buyer-test", "zh-CN", "CNY")
    )
    try:
        with observe_run_events(adapter.on_trade_event):
            await build_shopping_form_tool(forms, TradeEventBus())(
                "选购背包", [{"id": "use_case", "type": "text", "label": "背包主要用在哪个场景？"}]
            )
        assert adapter.state.get("shoppingForm"), "Agent 工具应更新页面和持久事件快照"
        assert any(getattr(e, "name", None) == "a2ui" for e in events)
    finally:
        ShoppingContext.reset(token)


async def test_messages_follow_official_a2ui_schema_and_local_catalog(tmp_path):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    root = Path(__file__).parent / "contracts/a2ui-v0.9"
    catalog = json.loads((root / "catalog.json").read_text())
    registry = Registry().with_resource(catalog["$id"], Resource.from_contents(catalog))
    validator = Draft202012Validator(
        json.loads((root / "server_to_client.json").read_text()), registry=registry
    )
    form = await store(tmp_path / "forms.db").create(
        "b", "s", "预算与配送", ["query", "budget"], {}
    )
    for message in form["messages"]:
        validator.validate(message)
    # 旧记录与新问题统一输出同一通用控件合同。
    dynamic = await store(tmp_path / "forms.db").create_clarification("b", "s", "耳机需求", [
        {"id": "purpose", "type": "single_select", "label": "使用场景？",
         "options": [{"value": "metro", "label": "地铁通勤"}, {"value": "home", "label": "家中"}]},
        {"id": "wear_hours", "type": "number", "label": "每天佩戴多久？", "unit": "小时"},
    ])
    for message in dynamic["messages"]:
        validator.validate(message)
    Draft202012Validator(
        json.loads((root / "client_to_server.json").read_text())
    ).validate(
        {
            "version": "v0.9",
            "action": {
                "name": "applyShoppingRequirements",
                "surfaceId": form["form_id"],
                "sourceComponentId": "root",
                "timestamp": "2026-09-18T00:00:00Z",
                "context": {"requirements": {"query": "背包"}},
            },
        }
    )


async def test_buyer_can_restore_unstarted_draft_without_browser_cache(tmp_path):
    forms = store(tmp_path / "forms.db")
    await forms.create("other", "secret", "不应该可见", ["query"], {})
    assert await forms.latest_for_buyer("buyer") is None
    form = await forms.create("buyer", "draft-session", "尚未开始对话", ["query"], {})
    restored = await store(tmp_path / "forms.db").latest_for_buyer("buyer")
    assert restored["form_id"] == form["form_id"] and restored["created_at"] > 0


async def test_clarification_preserves_travel_answers_and_only_requested_fields(tmp_path):
    forms = store(tmp_path/'clarification.db')
    form = await forms.create('b','s','确认登机背包条件',
        ['budget','airline','size_limit','weight_priority'],{'query':'登机背包'})
    assert 'currency' in form['fields']
    assert 'ship_to' not in form['fields']
    values={'query':'登机背包','budget':80,'currency':'USD','airline':'示例航空',
            'size_limit':'随身包40×30×20cm，包含把手','weight_priority':'lightest'}
    answer=await forms.submit('b','s',form['form_id'],1,'once',values)
    assert answer['values']==values
    assert '80 USD' in answer['query'] and '示例航空' in answer['query']
    assert '40×30×20cm' in answer['query'] and '请核验适用范围' in answer['query']
    assert '尽量轻' in answer['query']
    restored=await store(tmp_path/'clarification.db').get('b','s',form['form_id'])
    assert restored['submission']==answer
    assert await forms.submit('b','s',form['form_id'],1,'once',values)==answer
    with pytest.raises(LookupError):
        await forms.submit('another','s',form['form_id'],1,'once',values)


@pytest.mark.parametrize('extra',[
    {'airline':'a'*81},{'size_limit':'x'*201},{'airline':42},
    {'weight_priority':'anything'},{'weight_priority':{}}, {'create_order':True},
])
async def test_clarification_rejects_invalid_answers(tmp_path,extra):
    forms=store(tmp_path/'form.db')
    f=await forms.create('b','s','补充条件',['airline','size_limit','weight_priority'],{})
    with pytest.raises(ValueError):
        await forms.submit('b','s',f['form_id'],1,'r',{'query':'包',**extra})
    assert (await forms.get('b','s',f['form_id']))['submission'] is None


async def test_agent_scope_calls_clarification_tool_then_accepts_saved_answer(tmp_path):
    from agentscope.agent import Agent
    from agentscope.message import TextBlock, ToolCallBlock, UserMsg
    from agentscope.model import ChatResponse
    from agentscope.tool import FunctionTool, Toolkit
    from app.application.agents.permissions import allow_business_tools
    from app.application.tools.shopping_form_tool import build_shopping_form_tool
    from app.infrastructure.shopping_forms import ClarificationRequest
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from app.infrastructure.eventbus import TradeEventBus, observe_run_events
    from tests.test_native_memory_confirmation import Model

    class ClarifyingModel(Model):
        async def _invoke_upstream(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:
                content=[ToolCallBlock(id='clarify-1',name='show_shopping_form',input=json.dumps({
                    'title':'补充耳机需求','questions':[{'id':'use_case','type':'single_select','label':'主要在哪使用？','options':[{'value':'commute','label':'通勤'},{'value':'home','label':'家中'}]}],'context':'已知想买耳机'
                },ensure_ascii=False))]
            else:
                content=[TextBlock(text='请填写选购条件' if self.calls==2 else '已收到补充条件，继续核验')]
            return ChatResponse(content=content,is_last=True)

    model=ClarifyingModel();forms=store(tmp_path/'forms.db');emitted=[]
    agent=allow_business_tools(Agent('buyer_assistant','选购需求先澄清',model,
        toolkit=Toolkit(tools=[FunctionTool(build_shopping_form_tool(forms,TradeEventBus()), input_schema=ClarificationRequest)])))
    token=ShoppingContext.set(ShoppingContextSnapshot('s','b','zh-CN','USD'))
    try:
        with observe_run_events(lambda event:emitted.append(event)):
            _=[event async for event in agent.reply_stream(UserMsg('buyer','我想选耳机，先问我缺少哪些信息'))]
        form=await forms.latest('b','s')
        assert form is not None and form['defaults']=={}
        assert [q['id'] for q in form['questions']]==['use_case']
        assert form['submission'] is None
        assert emitted and model.calls==2
        result=await forms.submit('b','s',form['form_id'],1,'answer',{
            'use_case':'commute'})
        _=[event async for event in agent.reply_stream(UserMsg('buyer',result['query']))]
        assert model.calls==3
        assert any(block.get('text') == result['query']
                   for message in agent.state.model_dump()['context'] for block in message['content'])
        assert (await forms.latest('b','s'))['form_id']==form['form_id']
    finally:
        ShoppingContext.reset(token)
        await model.client.close()


async def test_agent_questions_are_not_limited_to_travel_or_legacy_fields(tmp_path):
    forms = store(tmp_path / 'dynamic.db')
    questions = [
        {'id': 'use_case', 'type': 'single_select', 'label': '主要在什么场景听音乐？', 'required': True,
         'options': [{'value': 'metro', 'label': '地铁通勤'}, {'value': 'running', 'label': '户外跑步'}]},
        {'id': 'features', 'type': 'multi_select', 'label': '更看重哪些特性？',
         'options': [{'value': 'anc', 'label': '主动降噪'}, {'value': 'comfort', 'label': '长时间佩戴舒适'}]},
        {'id': 'wear_hours', 'type': 'number', 'label': '每天大约佩戴多久？', 'unit': '小时', 'minimum': 0, 'maximum': 24},
        # 同名也不能触发历史预算语义，问题定义是唯一依据。
        {'id': 'budget', 'type': 'text', 'label': '还有哪些想补充的？'},
    ]
    form = await forms.create_clarification('b', 's', '补充耳机需求', questions, '买家已经明确要耳机', '了解使用方式才能比较')
    assert 'fields' not in form and form['defaults'] == {}
    assert [q['id'] for q in form['questions']] == [q['id'] for q in questions]
    result = await forms.submit('b', 's', form['form_id'], 1, 'once', {
        'use_case': 'metro', 'features': ['anc', 'comfort'], 'wear_hours': 2.5, 'budget': '不要入耳式',
    })
    payload = json.loads(result['query'].split('\n', 1)[1])
    assert payload['answers'][0] == {'id': 'use_case', 'question': '主要在什么场景听音乐？',
                                    'value': 'metro', 'display_value': '地铁通勤', 'unit': ''}
    assert payload['answers'][1]['display_value'] == ['主动降噪', '长时间佩戴舒适']
    assert payload['answers'][2]['unit'] == '小时'
    assert '商品单价不超过' not in result['query']
    restored = await store(tmp_path / 'dynamic.db').get('b', 's', form['form_id'])
    assert restored['questions'] == form['questions'] and restored['submission'] == result
    assert await forms.submit('b', 's', form['form_id'], 1, 'once', result['values']) == result
    with pytest.raises(LookupError):
        await forms.submit('other', 's', form['form_id'], 1, 'once', result['values'])
    from app.infrastructure.shopping_forms import FormConflict
    with pytest.raises(FormConflict):
        await forms.submit('b', 's', form['form_id'], 1, 'once', {'use_case': 'running'})
    followup = await forms.create_clarification('b', 's', '再确认一下', [
        {'id': 'fit', 'type': 'text', 'label': '可以接受头戴式吗？'},
    ])
    assert [q['id'] for q in followup['questions']] == ['fit']
    # 未回答不能用选项第一项或背景替代答案。
    blank = await forms.submit('b', 's', followup['form_id'], 1, 'blank', {})
    payload = json.loads(blank['query'].split('\n', 1)[1])
    assert payload['answers'] == [] and payload['unanswered'][0]['id'] == 'fit'


@pytest.mark.parametrize('questions', [
    [], [{'id':'x','type':'html','label':'坏控件'}],
    [{'id':'x','type':'text','label':'重复'}, {'id':'x','type':'text','label':'重复'}],
    [{'id':'x','type':'single_select','label':'缺选项'}],
    [{'id':'x','type':'single_select','label':'重复选项','options':[{'value':'a','label':'甲'},{'value':'a','label':'乙'}]}],
    [{'id':'x','type':'number','label':'错误范围','minimum':10,'maximum':1}],
    [{'id':'x','type':'text','label':'不允许脚本','onChange':'alert(1)'}],
    [{'id':'x','type':'text','label':'不能自动预选','default':'猜测的答案'}],
    [{'id':'constructor','type':'text','label':'非法标识'}],
    [{'id':'x','type':'number','label':'非有限范围','maximum':float('inf')}],
])
async def test_invalid_agent_question_definition_is_not_persisted(tmp_path, questions):
    forms = store(tmp_path / 'dynamic.db')
    with pytest.raises(ValueError):
        await forms.create_clarification('b','s','澄清',questions)
    assert await forms.latest('b','s') is None


@pytest.mark.parametrize('values', [
    {}, {'choice':'forged'}, {'choice':{}}, {'choice':True},
    {'choice':'a','count':True}, {'choice':'a','count':float('nan')},
    {'choice':'a','count':6}, {'choice':'a','count':10**400},
    {'choice':'a','features':['a','a']}, {'choice':'a','features':['forged']},
    {'choice':'a','features':{}}, {'choice':'a','note':42},
    {'choice':'a','note':'x'*2001}, {'choice':'a','query':'额外的字段'},
])
async def test_answer_validation_uses_persisted_question_contract(tmp_path, values):
    forms = store(tmp_path / 'dynamic.db')
    form = await forms.create_clarification('b','s','澄清',[
        {'id':'choice','type':'single_select','label':'选择','required':True,'options':[{'value':'a','label':'甲'}]},
        {'id':'features','type':'multi_select','label':'功能','options':[{'value':'a','label':'甲'}]},
        {'id':'count','type':'number','label':'数量','minimum':0,'maximum':5},
        {'id':'note','type':'text','label':'补充'},
    ])
    with pytest.raises(ValueError):
        await forms.submit('b','s',form['form_id'],1,'r',values)
    assert (await forms.get('b','s',form['form_id']))['submission'] is None


async def test_dynamic_form_route_rejects_tampering_and_accepts_answers(tmp_path):
    from app.presentation.shopping_forms import register_shopping_form_routes
    forms = store(tmp_path / 'dynamic.db')
    form = await forms.create_clarification('b','s','耳机澄清',[
        {'id':'device','type':'text','label':'连接什么设备？','required':True},
    ])
    api = FastAPI(); register_shopping_form_routes(api, lambda: forms)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as client:
        path = '/commerce/shopping-forms/'+form['form_id']+'/actions?buyer_id=b'
        body = {'session_id':'s','expected_revision':1,'request_id':'once',
                'action':{'name':'applyShoppingRequirements','surfaceId':form['form_id'],
                          'sourceComponentId':'root','timestamp':'2026-09-18T00:00:00Z',
                          'context':{'requirements':{'device':'笔记本'}}}}
        body['action']['context']['questions'] = [{'label':'篡改题目'}]
        assert (await client.post(path,json=body)).status_code == 422
        del body['action']['context']['questions']
        result = await client.post(path,json=body)
        assert result.status_code == 200 and '连接什么设备？' in result.json()['query']
        assert (await client.post(path,json=body)).json() == result.json()


def test_tool_schema_exposes_question_types_and_no_fixed_business_fields():
    from agentscope.tool import FunctionTool
    from app.application.tools.shopping_form_tool import build_shopping_form_tool
    from app.infrastructure.shopping_forms import ClarificationRequest
    tool = FunctionTool(build_shopping_form_tool(None, None), input_schema=ClarificationRequest)
    schema = tool.input_schema
    assert 'questions' in schema['properties'] and 'fields' not in schema['properties']
    assert set(schema['$defs']['ClarificationQuestion']['properties']['type']['enum']) == {
        'text','number','single_select','multi_select'
    }
