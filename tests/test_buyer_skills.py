# -*- coding: utf-8 -*-
"""买家 Skill 目录与真实读取状态。所有审核发布只发生在临时 SQLite 测试库。"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from ag_ui.core import RunAgentInput
from agentscope.message import ToolResultState
from fastapi import FastAPI

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.agents.orchestrator import MainAgentOrchestrator
from app.application.tools.capability_tools import build_capability_tools
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.capability_registry import CapabilityRegistry
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.identity import IdentityPolicy
from app.presentation.ag_ui import register_ag_ui_routes
from tests.test_capability_registry import document, publish
from tests.test_ag_ui_journal import body


def api_for(registry):
    factory = SimpleNamespace(capability_registry=registry,
        _search_factory=SimpleNamespace(build_tools=lambda: [SimpleNamespace(name="product_search_tool")]),
        _trade_factory=SimpleNamespace(build_tools=lambda: [SimpleNamespace(name="query_order_tool")]))
    orchestrator = MainAgentOrchestrator(SimpleNamespace(_main_factory=factory), TradeEventBus(), SimpleNamespace())
    api = FastAPI()
    register_ag_ui_routes(api, lambda: orchestrator)
    return api


async def test_catalog_is_empty_then_only_exposes_available_published_metadata(tmp_path, monkeypatch):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_for(registry)), base_url="http://test") as client:
        empty = await client.get("/commerce/skills?buyer_id=b1")
        assert empty.status_code == 200 and empty.json()["skills"] == []
        assert len(empty.json()["capability_digest"]) == 64
        active = {**document(), "expires_at": None, "body": "不能暴露在买家目录的完整正文"}
        published = publish(registry, active)
        registry.import_draft({**active, "id": "draft"}, author="fixture")
        publish(registry, {**active, "id": "revoked"})
        registry.revoke("skill", "revoked", "1", actor="fixture", reason="测试撤销")
        publish(registry, {**document(), "id": "expired"})
        publish(registry, {**active, "id": "web-only", "allowed_tools": ["web_search_tool"]})
        publish(registry, document("strategy"))
        monkeypatch.setattr("app.infrastructure.capability_registry.now", lambda: datetime(2100, 1, 1, tzinfo=timezone.utc))
        response = await client.get("/commerce/skills?buyer_id=b1")
        assert response.status_code == 200
        items = response.json()["skills"]
        assert len(items) == 1 and items[0]["id"] == "backpack"
        assert items[0]["content_hash"] == published["content_hash"]
        assert set(items[0]) == {"id", "version", "title", "description", "scope", "expires_at", "content_hash"}
        assert "不能暴露" not in response.text and "reviewer" not in response.text and "evidence" not in response.text


async def test_skill_catalog_uses_buyer_bearer_identity_without_creating_session(tmp_path):
    api = api_for(CapabilityRegistry(tmp_path / "caps.db"))
    policy = IdentityPolicy(mode="hmac", secret="test-only-buyer-skill-signing-secret-123456")
    api.state.identity_policy = policy
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.get("/commerce/skills?buyer_id=b1")).status_code == 401
        assert (await client.get("/commerce/skills?buyer_id=b1", headers={"Authorization": "Bearer " + policy.issue("b2")})).status_code == 403
        assert (await client.get("/commerce/skills?buyer_id=b1", headers={"Authorization": "Bearer " + policy.issue("b1")})).status_code == 200
        assert not hasattr(api.state, "session_store")  # 读取公共审核目录不提前创建购物会话。


async def test_catalog_version_race_returns_conflict_not_mixed_digest(tmp_path, monkeypatch):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    publish(registry, document())
    original = registry.metadata
    def changed(**kwargs):
        publish(registry, document(version="2"))
        return original(**kwargs)
    monkeypatch.setattr(registry, "metadata", changed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_for(registry)), base_url="http://test") as client:
        response = await client.get("/commerce/skills?buyer_id=b1")
        assert response.status_code == 409
        assert "skills" not in response.json()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_for(None)), base_url="http://test") as client:
        assert (await client.get("/commerce/skills?buyer_id=b1")).status_code == 503


def begin_read(adapter, call="skill-call"):
    adapter.on_agent_event(SimpleNamespace(type="TOOL_CALL_START", tool_call_id=call, tool_call_name="load_agent_skill_tool"))
    adapter.on_agent_event(SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id=call, delta=json.dumps({"skill_id": "backpack", "version": "1"})))
    adapter.on_agent_event(SimpleNamespace(type="TOOL_CALL_END", tool_call_id=call))
    adapter.on_agent_event(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id=call, tool_call_name="load_agent_skill_tool"))


def end_read(adapter, text, success=True, call="skill-call"):
    adapter.on_agent_event(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id=call, delta=text))
    adapter.on_agent_event(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id=call,
        state=ToolResultState.SUCCESS if success else ToolResultState.ERROR))


async def test_real_skill_read_projects_metadata_only_and_survives_journal_reload(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    published = publish(registry, document())
    events = []
    request = RunAgentInput.model_validate({**body(), "state": {"skillUsages": [{"status": "used", "title": "伪造"}]}})
    adapter = AGUIRunAdapter(request, events.append)
    adapter.start()
    assert adapter.state["skillUsages"] == []
    begin_read(adapter)
    assert adapter.state["skillUsages"][0]["status"] == "reading"
    load, _ = build_capability_tools(registry, {"product_search_tool"}, TradeEventBus())
    token = ShoppingContext.set(ShoppingContextSnapshot("s1", "b1", "zh-CN", "CNY"))
    try:
        loaded = await load("backpack", "1")
    finally:
        ShoppingContext.reset(token)
    end_read(adapter, loaded.content[0].text, loaded.state == ToolResultState.SUCCESS)
    adapter.finish("已读取方案，继续选购。")
    usage = adapter.state["skillUsages"][0]
    assert usage == {"toolCallId": "r1:tool:skill-call", "status": "used", "id": "backpack", "version": "1",
                     "title": "旅行背包选购", "contentHash": published["content_hash"]}
    assert "body" not in usage and "evidence" not in usage
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(request.model_dump(mode="json", by_alias=True), "b1", "owner")
    await journal.append("r1", "owner", [event.model_dump(mode="json", by_alias=True, exclude_none=True) for event in events])
    restored = await AGUIJournal(tmp_path / "runs.db").run("r1", "b1")
    assert restored["state"]["skillUsages"] == [usage]


@pytest.mark.parametrize("success,content", [(False, "[error] 已撤销"), (True, "正文不是结构化成功结果"),
    (True, json.dumps({"kind": "skill", "id": "backpack", "version": "1"})),
    (True, json.dumps({**document(version="2"), "authority": "reference_only", "content_hash": "a" * 64}))])
def test_failed_malformed_or_mismatched_skill_result_never_claims_used(success, content):
    adapter = AGUIRunAdapter(RunAgentInput.model_validate(body()), lambda _: None)
    begin_read(adapter)
    end_read(adapter, content, success)
    assert adapter.state["skillUsages"][0]["status"] == "error"
    assert "contentHash" not in adapter.state["skillUsages"][0]


def test_stop_and_normal_finish_without_tool_success_do_not_claim_used():
    for stop in (True, False):
        adapter = AGUIRunAdapter(RunAgentInput.model_validate(body()), lambda _: None)
        begin_read(adapter)
        if stop:
            adapter.fail("已停止", cancelled=True)
        else:
            adapter.finish("工具没有返回结果")
        assert adapter.state["skillUsages"][0]["status"] == "error"
