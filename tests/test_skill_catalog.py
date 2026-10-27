"""变化驱动目录：真实 AgentScope/HTTP 序列化与隔离 SQLite，旧证据逐字断言。"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from agentscope.agent import Agent
from agentscope.message import UserMsg, AssistantMsg, TextBlock
from agentscope.state import AgentState

from app.application.agents.personal_skill_context import (
    SkillCatalogMiddleware, catalog_snapshot, receipt_visible,
)
from app.application.tools.capability_tools import STABLE_CAPABILITY_POLICY
from app.infrastructure.buyer_skills import BuyerSkillStore, BuyerSkillConflict
from app.infrastructure.capability_registry import CapabilityRegistry, CapabilityVersionChanged
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from tests.test_capability_registry import publish, document
from tests.test_prompt_cache import client_model, completion


@pytest.fixture
async def case(tmp_path):
    personal = BuyerSkillStore(tmp_path / "skills.db")
    registry = CapabilityRegistry(tmp_path / "capabilities.db")
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion())
    model = await client_model(tmp_path, handler)
    token = ShoppingContext.set(ShoppingContextSnapshot("session", "buyer", "zh-CN", "CNY"))
    middleware = SkillCatalogMiddleware(registry, personal, {"product_search_tool"})
    def build(state=None):
        return Agent("agent", STABLE_CAPABILITY_POLICY, model, state=state, middlewares=[middleware])
    yield SimpleNamespace(personal=personal, registry=registry, requests=requests,
                          agent=build(), build=build, middleware=middleware)
    ShoppingContext.reset(token)
    await model.client.close()


def catalogs(agent):
    return [b.text for m in agent.state.context for b in m.content
            if isinstance(b, TextBlock) and b.text.startswith("<skill-catalog>")]


def normalized(messages):
    copied = deepcopy(messages)
    for message in copied:
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                block.pop("cache_control", None)
    return copied


async def test_unchanged_catalog_is_injected_once_and_http_prefix_is_immutable(case):
    case.personal.save("buyer", "背包方案", "通勤", "轻便优先")
    await case.agent.reply(UserMsg("buyer", "选背包"))
    before = [m.model_dump_json() for m in case.agent.state.context]
    payload = normalized(case.requests[-1]["messages"])
    await case.agent.reply(UserMsg("buyer", "预算改成 500 元"))
    assert len(catalogs(case.agent)) == 1
    assert [m.model_dump_json() for m in case.agent.state.context[:len(before)]] == before
    assert normalized(case.requests[-1]["messages"])[:len(payload)] == payload
    assert case.requests[0]["messages"][0] == case.requests[1]["messages"][0]
    assert "轻便优先" not in catalogs(case.agent)[0]  # 目录不包含正文。


async def test_edit_body_delete_last_skill_and_restart_append_without_rewriting(case):
    skill = case.personal.save("buyer", "背包方案", "通勤", "轻便优先")
    await case.agent.reply(UserMsg("buyer", "开始"))
    old = [m.model_dump_json() for m in case.agent.state.context]
    changed = case.personal.save("buyer", "背包方案", "通勤", "耐用优先",
                                 skill_id=skill["id"], expected_version="1")
    assert changed["version"] == "2"
    await case.agent.reply(UserMsg("buyer", "重新比较"))
    assert len(catalogs(case.agent)) == 2
    assert [m.model_dump_json() for m in case.agent.state.context[:len(old)]] == old
    restored = case.build(AgentState.model_validate_json(case.agent.state.model_dump_json()))
    await restored.reply(UserMsg("buyer", "继续"))
    assert len(catalogs(restored)) == 2
    case.personal.delete("buyer", skill["id"], "2")
    await restored.reply(UserMsg("buyer", "还有方案吗"))
    assert len(catalogs(restored)) == 3 and '"skills":[]' in catalogs(restored)[-1]
    assert [m.model_dump_json() for m in restored.state.context[:len(old)]] == old
    with pytest.raises(LookupError):
        case.personal.load("buyer", skill["id"], "1")


async def test_empty_catalog_and_noop_save_do_not_generate_versions(case):
    await case.agent.reply(UserMsg("buyer", "开始"))
    await case.agent.reply(UserMsg("buyer", "继续"))
    assert len(catalogs(case.agent)) == 1 and '"skills":[]' in catalogs(case.agent)[0]
    one = case.personal.save("buyer", "背包", "通勤", "轻便")
    two = case.personal.save("buyer", " 背包 ", "通勤", "轻便", skill_id=one["id"], expected_version="1")
    assert one == two
    with pytest.raises(BuyerSkillConflict):
        case.personal.save("buyer", "背包", "通勤", "轻便", skill_id=one["id"], expected_version="0")


async def test_checkpoint_without_visible_catalog_reseeds_at_next_user_only(case):
    await case.agent.reply(UserMsg("buyer", "开始"))
    receipt = deepcopy(case.agent.state.middle_context["skill_catalog"]["receipt"])
    case.agent.state.context[:] = [AssistantMsg("agent", "之前的内容已摘要")]
    assert not receipt_visible(case.agent, receipt)
    await case.agent.reply(UserMsg("buyer", "继续"))
    assert len(catalogs(case.agent)) == 1
    assert case.agent.state.middle_context["skill_catalog"]["receipt"]["digest"] == receipt["digest"]
    assert case.agent.state.middle_context["skill_catalog"]["receipt"]["message_id"] != receipt["message_id"]


async def test_cancel_before_accepting_input_does_not_advance_watermark(case):
    async def cancelled(**kwargs):
        raise asyncio.CancelledError()
        yield  # 原生异步生成器接口。
    with pytest.raises(asyncio.CancelledError):
        async for _ in case.middleware.on_reply(case.agent, {"inputs": UserMsg("buyer", "开始")}, cancelled):
            pass
    assert "skill_catalog" not in case.agent.state.middle_context
    await case.agent.reply(UserMsg("buyer", "重试"))
    assert len(catalogs(case.agent)) == 1


async def test_cancel_after_accepting_input_commits_receipt_and_resume_does_not_duplicate(case):
    async def cancelled(**kwargs):
        case.agent.state.context.extend(kwargs["inputs"])
        raise asyncio.CancelledError()
        yield
    with pytest.raises(asyncio.CancelledError):
        async for _ in case.middleware.on_reply(case.agent, {"inputs": UserMsg("buyer", "开始")}, cancelled):
            pass
    assert receipt_visible(case.agent, case.agent.state.middle_context["skill_catalog"]["receipt"])
    received = []
    async def resumed(**kwargs):
        received.append(kwargs["inputs"])
        yield "resumed"
    async for _ in case.middleware.on_reply(case.agent, {"inputs": None}, resumed):
        pass
    assert received == [None] and len(catalogs(case.agent)) == 1
    await case.agent.reply(UserMsg("buyer", "继续"))
    assert len(catalogs(case.agent)) == 1


def test_snapshot_ignores_order_and_timestamp_but_detects_effective_changes():
    a = {"id": "a", "version": "1", "content_hash": "hash-a", "updated_at": "yesterday"}
    b = {"id": "b", "version": "1", "content_hash": "hash-b"}
    base = catalog_snapshot([], [a, b])
    assert base == catalog_snapshot([], [b, {**a, "updated_at": "today"}])
    for changed in ({**a, "content_hash": "new"}, {**a, "title": "新名字"},
                    {**a, "version": "2"}, {**a, "expires_at": "2100-01-01"}):
        assert base["digest"] != catalog_snapshot([], [changed, b])["digest"]
    assert base["digest"] != catalog_snapshot([a], [b])["digest"]


async def test_buyer_isolation_and_watermark_owner_fail_closed(case):
    other = case.personal.save("other", "私有方案", "秘密用途", "秘密正文")
    await case.agent.reply(UserMsg("buyer", "开始"))
    assert other["id"] not in catalogs(case.agent)[0]
    case.agent.state.middle_context["skill_catalog"]["buyer_id"] = "other"
    with pytest.raises(ValueError, match="买家"):
        await case.agent.reply(UserMsg("buyer", "继续"))


def test_public_publish_is_hot_but_revoke_and_strategy_change_remain_blocked(tmp_path):
    registry = CapabilityRegistry(tmp_path / "capabilities.db")
    publish(registry, document())
    first = registry.bind_session("s", "b", allow_skill_updates=True)
    publish(registry, document(version="2"))
    with pytest.raises(CapabilityVersionChanged):
        registry.bind_session("s", "b")  # 本轮工具不能自动接受新资料。
    second = registry.bind_session("s", "b", allow_skill_updates=True)
    assert second != first
    with pytest.raises(CapabilityVersionChanged):
        registry.load_skill("backpack", "1", available_tools={"product_search_tool"}, require_current=True)
    assert registry.load_skill("backpack", "2", available_tools={"product_search_tool"}, require_current=True)
    registry.revoke("skill", "backpack", "1", actor="fixture", reason="硬撤销")
    with pytest.raises(CapabilityVersionChanged):
        registry.bind_session("s", "b", allow_skill_updates=True)
    registry.bind_session("s-new", "b", allow_skill_updates=True)
    publish(registry, document("strategy"))
    with pytest.raises(CapabilityVersionChanged):
        registry.bind_session("s-new", "b", allow_skill_updates=True)


async def test_selected_body_remains_immutable_but_activation_ends_next_turn(case):
    skill = case.personal.save("buyer", "背包", "通勤", "轻便优先")
    reference = UserMsg("selected_skill_reference", "本轮参考：轻便优先",
        metadata={"skill_activation": {"id": skill["id"], "version": "1", "contentHash": skill["content_hash"]}})
    await case.agent.reply([reference, UserMsg("buyer", "使用方案")])
    saved = case.agent.state.context[0].model_dump_json()
    assert reference.id in case.agent.state.middle_context["skill_catalog"]["active_reference_ids"]
    case.personal.delete("buyer", skill["id"], "1")
    await case.agent.reply(UserMsg("buyer", "继续普通选购"))
    assert case.agent.state.context[0].model_dump_json() == saved
    assert case.agent.state.middle_context["skill_catalog"]["active_reference_ids"] == []


async def test_deleted_active_skill_is_rejected_before_model(case):
    skill = case.personal.save("buyer", "背包", "通勤", "轻便")
    reference = UserMsg("selected_skill_reference", "轻便", metadata={"skill_activation": {
        "id": skill["id"], "version": "1", "contentHash": skill["content_hash"]}})
    case.personal.delete("buyer", skill["id"], "1")
    with pytest.raises(LookupError):
        await case.agent.reply([reference, UserMsg("buyer", "使用方案")])
    assert not case.requests


@pytest.mark.parametrize("mode", ["legacy", "append_only"])
@pytest.mark.parametrize("scenario", ["unchanged", "edit", "delete"])
async def test_existing_harness_skill_replay_uses_native_orchestrator_and_sqlite(tmp_path, monkeypatch, mode, scenario):
    from dataclasses import replace
    from scripts.eval.harness.cache_replay import run_skill_replay
    from app.infrastructure.throttle import GatewayThrottle
    from tests.test_retrieval import _settings
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        version = None if count >= 3 and scenario == "delete" else ("2" if count >= 3 and scenario == "edit" else "1")
        response = completion()
        response["choices"][0]["message"]["content"] = json.dumps({"available": version is not None, "version": version})
        return httpx.Response(200, json=response)
    model = await client_model(tmp_path, handler)
    monkeypatch.setattr("scripts.eval.harness.cache_replay.create_chat_model", lambda *a, **k: model)
    row = await run_skill_replay({"id": "skill-"+scenario, "scenario": scenario},
        replace(_settings(tmp_path), skill_catalog_mode=mode), GatewayThrottle(1, 0),
        tmp_path / "replay", 0, None, {"output_limit": 128, "request_timeout_seconds": 10})
    assert row["passed"] and len(row["usage"]) == 4 and len(row["transcript"]) == 4


async def test_native_approval_resume_defers_catalog_change_until_next_buyer_turn(tmp_path):
    from agentscope.tool import Toolkit, FunctionTool, ToolChunk
    from app.application.agents.tool_confirmation import MemoryPermissionMiddleware, awaiting_event, confirmation_inputs
    from tests.test_native_memory_confirmation import Model
    model = Model()
    store = BuyerSkillStore(tmp_path / "skills.db")
    registry = CapabilityRegistry(tmp_path / "cap.db")
    writes = []
    async def remember_preference_tool(statement: str):
        """保存本测试偏好。"""
        writes.append(statement)
        return ToolChunk(content=[TextBlock(text="已保存")])
    toolkit = Toolkit(tools=[FunctionTool(remember_preference_tool)])
    middleware = [MemoryPermissionMiddleware(), SkillCatalogMiddleware(registry, store, set())]
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "buyer", "zh-CN", "CNY"))
    try:
        agent = Agent("test", STABLE_CAPABILITY_POLICY, model, toolkit=toolkit, middlewares=middleware)
        await agent.reply(UserMsg("buyer", "记住喜欢裙子"))
        pending = awaiting_event(agent)
        assert pending and not writes and len(catalogs(agent)) == 1
        store.save("buyer", "新方案", "用途", "正文")
        restored = Agent("test", STABLE_CAPABILITY_POLICY, model, toolkit=toolkit, middlewares=middleware,
                         state=AgentState.model_validate_json(agent.state.model_dump_json()))
        await restored.reply(confirmation_inputs(restored, ({"interrupt_id": f"{pending.reply_id}:call-a", "approved": False},)))
        assert not writes and len(catalogs(restored)) == 1
        await restored.reply(UserMsg("buyer", "继续"))
        assert len(catalogs(restored)) == 2
    finally:
        ShoppingContext.reset(token)
        await model.client.close()


async def test_catalog_and_watermark_share_session_fencing_transaction(case, tmp_path):
    from app.infrastructure.persistence.json_file_stores import JsonFileSessionStore
    from app.domain.session.ports.session_store import StaleSessionWrite
    store = JsonFileSessionStore(tmp_path / "sessions")
    try:
        older = await store.claim("s", buyer_id="buyer")
        await case.agent.reply(UserMsg("buyer", "开始"))
        saved = await store.save_claim(older, case.agent.state.model_dump_json())
        newer = await store.claim("s", buyer_id="buyer")
        restored = AgentState.model_validate_json(newer.state_json)
        assert restored.middle_context["skill_catalog"] == case.agent.state.middle_context["skill_catalog"]
        case.personal.save("buyer", "新方案", "用途", "正文")
        await case.agent.reply(UserMsg("buyer", "继续"))
        with pytest.raises(StaleSessionWrite):
            await store.save_claim(saved, case.agent.state.model_dump_json())
        current = await store.claim("s", buyer_id="buyer")
        assert current.state_json == newer.state_json  # 失败写入不能单独推进目录水位。
    finally:
        await store.close()


def test_skill_evaluation_scorer_rejects_stale_versions_and_string_booleans():
    from scripts.eval.harness.cache_replay import check_skill_answer
    assert all(check_skill_answer('{"available":false,"version":null}', None).values())
    for text in ('{"available":true,"version":"1"}', '{"available":"false","version":null}', '已删除'):
        assert not all(check_skill_answer(text, None).values())


def test_explicit_compaction_can_archive_old_skill_but_pins_current_catalog_and_activation():
    from agentscope.message import ToolCallBlock, ToolResultBlock, ToolResultState
    from app.infrastructure.context_governance import compression_parts
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "buyer", "zh-CN", "CNY"))
    try:
        old = AssistantMsg("agent", [
            ToolCallBlock(id="old", name="load_agent_skill_tool", input="{}"),
            ToolResultBlock(id="old", name="load_agent_skill_tool", output="旧正文", state=ToolResultState.SUCCESS)])
        messages = [UserMsg("buyer", "第一轮"), old]
        for i in range(5):
            messages.extend([UserMsg("buyer", f"第{i+2}轮"), AssistantMsg("agent", "回答")])
        selected = UserMsg("selected_skill_reference", "本轮方案")
        messages.insert(-1, selected)
        latest_user = next(m for m in reversed(messages) if m.name == "buyer")
        state = AgentState(context=messages, middle_context={"skill_catalog": {
            "mode": "append_only", "user_message_id": latest_user.id,
            "receipt": {"message_id": messages[0].id}, "active_reference_ids": [selected.id]}})
        agent = SimpleNamespace(state=state)
        before = [m.model_dump_json() for m in messages]
        head, tail = compression_parts(agent)
        assert old in head and old not in tail
        assert messages[0] in tail and selected in tail
        assert [m.model_dump_json() for m in messages] == before  # 划分本身不改写内容。
    finally:
        ShoppingContext.reset(token)
