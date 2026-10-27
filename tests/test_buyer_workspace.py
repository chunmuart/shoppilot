"""个人 Skill / 偏好从页面持久化到 Agent 上下文的合同回归。"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from agentscope.message import AssistantMsg, UserMsg, ToolResultBlock, ToolResultState

from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
from app.application.agents.selected_skill import SelectedSkill, SelectedSkillError, preload_selected_skill
from app.application.agents.personal_skill_context import clear_personal_skill_outputs
from app.application.tools.capability_tools import build_capability_tools
from app.application.tools.update_preference_tool import build_update_preference_tool
from app.application.tools.remember_preference_tool import build_remember_preference_tool
from app.application.tools.forget_preference_tool import build_forget_preference_tool
from app.domain.buyer.preference import BuyerPreference
from app.infrastructure.buyer_skills import BuyerSkillStore, BuyerSkillConflict
from app.infrastructure.capability_registry import CapabilityRegistry
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore
from app.infrastructure.persistence.sql.repositories import SqlPreferenceStore, create_engine
from app.infrastructure.persistence.sql.tables import Base
from app.presentation.buyer_workspace import register_buyer_workspace_routes
from app.presentation.ag_ui import register_ag_ui_routes


def fixture(tmp_path):
    skill_store=BuyerSkillStore(tmp_path/"personal.db")
    preferences=JsonFilePreferenceStore(tmp_path)
    registry=CapabilityRegistry(tmp_path/"global.db")
    factory=SimpleNamespace(buyer_skill_store=skill_store,capability_registry=registry,
        _search_factory=SimpleNamespace(build_tools=lambda: []),_trade_factory=SimpleNamespace(build_tools=lambda: []))
    o=MainAgentOrchestrator(SimpleNamespace(_main_factory=factory),TradeEventBus(),preferences)
    api=FastAPI()
    policy=IdentityPolicy(mode="hmac",secret="workspace-test-signing-secret-123456789")
    api.state.identity_policy=policy
    register_buyer_workspace_routes(api,lambda:o)
    register_ag_ui_routes(api,lambda:o)
    return api,o,skill_store,preferences,registry,policy


def headers(policy,buyer="alice"):
    return {"Authorization":"Bearer "+policy.issue(buyer)}


async def test_http_private_crud_isolation_versions_and_restart(tmp_path):
    api,o,store,prefs,registry,policy=fixture(tmp_path)
    raw={"title":"轻装旅行","description":"周末出游时","body":"先问预算，再比较重量。"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://test") as c:
        assert (await c.post("/commerce/my-skills?buyer_id=alice",json=raw)).status_code==401
        assert (await c.post("/commerce/my-skills?buyer_id=bob",headers=headers(policy),json=raw)).status_code==403
        created=await c.post("/commerce/my-skills?buyer_id=alice",headers=headers(policy),json=raw)
        assert created.status_code==200
        skill=created.json()["skill"];identifier=skill["id"]
        path=f"/commerce/my-skills/{identifier}?buyer_id=alice"
        assert (await c.get("/commerce/my-skills?buyer_id=bob",headers=headers(policy,"bob"))).json()["skills"]==[]
        foreign=await c.put(f"/commerce/my-skills/{identifier}?buyer_id=bob",headers=headers(policy,"bob"),json={**raw,"expected_version":"1"})
        assert foreign.status_code==404
        catalog=(await c.get("/commerce/skills?buyer_id=alice",headers=headers(policy))).json()
        assert catalog["skills"][0]["id"]==identifier
        assert "body" not in catalog["skills"][0]
        digest=registry.version_fingerprint()
        updated=await c.put(path,headers=headers(policy),json={**raw,"body":"只整理需求，不搜索。","expected_version":"1"})
        assert updated.status_code==200 and updated.json()["skill"]["version"]=="2"
        assert updated.json()["skill"]["content_hash"]!=skill["content_hash"]
        assert (await c.put(path,headers=headers(policy),json={**raw,"expected_version":"1"})).status_code==409
        assert BuyerSkillStore(store.path).load("alice",identifier,"2")["body"]=="只整理需求，不搜索。"
        with pytest.raises(LookupError):store.load("alice",identifier,"1")
        assert registry.version_fingerprint()==digest  # 私人编辑不影响全局买家会话。
        assert (await c.delete(path+"&expected_version=1",headers=headers(policy))).status_code==409
        assert (await c.delete(path+"&expected_version=2",headers=headers(policy))).status_code==200
        with pytest.raises(LookupError):store.load("alice",identifier,"2")
        assert (await c.get("/commerce/skills?buyer_id=alice",headers=headers(policy))).json()["skills"]==[]


@pytest.mark.parametrize("extra",[{"buyer_id":"bob"},{"allowed_tools":["create_order_tool"]},{"authority":"system"},{"id":"../global"}])
async def test_personal_skill_http_cannot_claim_owner_or_permissions(tmp_path,extra):
    api,*_,policy=fixture(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://test") as c:
        response=await c.post("/commerce/my-skills?buyer_id=alice",headers=headers(policy),
            json={"title":"名称","description":"用途","body":"正文",**extra})
        assert response.status_code==422


async def test_private_selected_read_checks_owner_hash_and_latest_version(tmp_path):
    _,_,store,_,registry,_=fixture(tmp_path)
    skill=store.save("alice","名称","用途","只回答已确认的需求")
    async def schemas():return [{"function":{"name":"load_agent_skill_tool"}}]
    agent=SimpleNamespace(toolkit=SimpleNamespace(get_tool_schemas=schemas))
    selection=SelectedSkill(skill["id"],"1",skill["content_hash"])
    snapshot=ShoppingContextSnapshot("session","alice","zh-CN","CNY")
    token=ShoppingContext.set(snapshot)
    try:
        ShoppingContext.set_capability_digest(registry.bind_session("session","alice"))
        reference,_=await preload_selected_skill(selection,registry=registry,agent=agent,
            buyer_id="alice",session_id="session",personal_store=store)
        assert "只回答已确认的需求" in reference.content[0].text
        with pytest.raises(SelectedSkillError):
            await preload_selected_skill(SelectedSkill(skill["id"],"1","0"*64),registry=registry,agent=agent,
                buyer_id="alice",session_id="session",personal_store=store)
        with pytest.raises(SelectedSkillError):
            await preload_selected_skill(selection,registry=registry,agent=agent,
                buyer_id="bob",session_id="session",personal_store=store)
        store.delete("alice",skill["id"],"1")
        with pytest.raises(SelectedSkillError):
            await preload_selected_skill(selection,registry=registry,agent=agent,
                buyer_id="alice",session_id="session",personal_store=store)
    finally:ShoppingContext.reset(token)


async def test_personal_loader_is_scoped_and_old_tool_body_is_cleared(tmp_path):
    _,_,store,_,registry,_=fixture(tmp_path)
    skill=store.save("alice","名称","用途","私有正文")
    load,_=build_capability_tools(registry,set(),TradeEventBus(),store)
    token=ShoppingContext.set(ShoppingContextSnapshot("s","alice","zh-CN","CNY"))
    try:
        result=await load(skill["id"],"1")
        assert result.state==ToolResultState.SUCCESS
        msg=AssistantMsg("tool", [ToolResultBlock(type="tool_result",id="call1",name="load_agent_skill_tool",
            output=result.content,state=result.state)])
        clear_personal_skill_outputs([msg])
        assert "私有正文" not in str(msg.content)
        assert msg.content[0].id=="call1"  # 调用配对仍完整。
    finally:ShoppingContext.reset(token)
    token=ShoppingContext.set(ShoppingContextSnapshot("s2","bob","zh-CN","CNY"))
    try:
        assert (await load(skill["id"],"1")).state==ToolResultState.ERROR
    finally:ShoppingContext.reset(token)


async def test_page_preferences_reenter_context_after_update_and_delete(tmp_path):
    api,o,_,store,_,policy=fixture(tmp_path)
    intent=SubmitIntentInput("s","alice","zh-CN","CNY","选购背包")
    token=ShoppingContext.set(ShoppingContextSnapshot("s","alice","zh-CN","CNY"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://test") as c:
            url="/commerce/preferences?buyer_id=alice"
            assert (await c.post(url,headers=headers(policy),json={"kind":"dislike","statement":"不要真皮"})).status_code==200
            inputs=await o._build_inputs(intent,"new-session")
            assert "不要真皮" in inputs[0].content[0].text
            changed=await c.post(url,headers=headers(policy),json={"kind":"like","statement":"喜欢帆布","previous_statement":"不要真皮"})
            assert changed.status_code==200
            text=(await o._build_inputs(intent,"new-session"))[0].content[0].text
            assert "喜欢帆布" in text and "不要真皮" not in text
            assert (await c.post(url,headers=headers(policy),json={"kind":"like","statement":"不应写入","previous_statement":"不要真皮"})).status_code==409
            assert (await c.get("/commerce/preferences?buyer_id=bob",headers=headers(policy,"bob"))).json()["preferences"]==[]
            assert (await c.request("DELETE",url,headers=headers(policy),json={"statement":"喜欢帆布"})).json()["deleted"]
            text=(await o._build_inputs(intent,"another-new-session"))[0].content[0].text
            assert "喜欢帆布" not in text and "历史已撤回偏好不得恢复" in text
    finally:ShoppingContext.reset(token)


@pytest.mark.parametrize("backend",["json","sql"])
async def test_atomic_tool_update_delete_and_optimistic_conflict(tmp_path,backend):
    engine=None
    if backend=="sql":
        engine=create_engine(f"sqlite+aiosqlite:///{tmp_path/'preferences.db'}")
        async with engine.begin() as c:await c.run_sync(Base.metadata.create_all)
        store=SqlPreferenceStore(engine)
    else:store=JsonFilePreferenceStore(tmp_path)
    try:
        await store.append(BuyerPreference("alice","like","喜欢黑色"))
        await store.append(BuyerPreference("bob","like","喜欢黑色"))
        tool=build_update_preference_tool(store,TradeEventBus())
        token=ShoppingContext.set(ShoppingContextSnapshot("s","alice","zh-CN","CNY"))
        try:
            result=await tool("喜欢黑色","like","喜欢蓝色")
            assert "已更新" in result.content[0].text
            assert [p.statement for p in await store.list_by_buyer("alice")]==["喜欢蓝色"]
            assert [p.statement for p in await store.list_by_buyer("bob")]==["喜欢黑色"]
            results=await asyncio.gather(
                store.replace("alice","喜欢蓝色",BuyerPreference("alice","like","喜欢红色")),
                store.replace("alice","喜欢蓝色",BuyerPreference("alice","like","喜欢白色")))
            assert sorted(results)==[False,True]
            remaining=await store.list_by_buyer("alice")
            forget=build_forget_preference_tool(store,TradeEventBus())
            assert "已撤回" in (await forget(remaining[0].statement)).content[0].text
            assert await store.list_by_buyer("alice")==[]
        finally:ShoppingContext.reset(token)
    finally:
        if engine:await engine.dispose()


@pytest.mark.parametrize("builder,args",[
    (build_remember_preference_tool,("like","喜欢黑色")),
    (build_forget_preference_tool,("喜欢黑色",)),
    (build_update_preference_tool,("喜欢黑色","like","喜欢蓝色"))])
async def test_memory_tools_reject_missing_identity(tmp_path,builder,args):
    store=JsonFilePreferenceStore(tmp_path)
    result=await builder(store,TradeEventBus())(*args)
    assert result.state==ToolResultState.ERROR
    assert await store.list_by_buyer("anonymous")==[]
