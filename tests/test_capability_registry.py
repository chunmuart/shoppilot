# -*- coding: utf-8 -*-
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest
from agentscope.state import AgentState
from agentscope.tool import FunctionTool

from app.infrastructure.capability_registry import CapabilityRegistry, CapabilityError, CapabilityVersionChanged
from app.application.tools.capability_tools import build_capability_tools, capability_hint
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from scripts.capabilities import main


def document(kind="skill", version="1"):
    return {"kind": kind, "id": "backpack", "version": version, "title": "旅行背包选购",
            "description": "核对预算、轻便需求与目的地", "body": "按用户硬约束检索，再对比已有商品字段；缺少运费时不得编造到手价。",
            "scope": "shopping:backpack", "allowed_tools": ["product_search_tool"] if kind == "skill" else [],
            "evidence": [{"reference": "tests/fixtures/catalog", "summary": "测试证据，不是生产经验"}],
            "expires_at": "2099-01-01T00:00:00Z", "keywords": ["背包", "轻便"]}


def publish(registry, doc):
    registry.import_draft(doc, author="fixture-author")
    registry.review(doc["kind"], doc["id"], doc["version"], reviewer="fixture-reviewer", evidence="仅测试库的人工审核步骤验证")
    return registry.publish(doc["kind"], doc["id"], doc["version"], publisher="fixture-publisher", evidence="测试回归记录")


def test_immutable_review_publish_revoke_and_metadata_has_no_body(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    doc = document()
    registry.import_draft(doc, author="operator")
    assert registry.metadata() == []
    with pytest.raises(CapabilityError, match="发布前"):
        registry.publish("skill", "backpack", "1", publisher="operator", evidence="未审核")
    with pytest.raises(CapabilityError, match="未发布"):
        registry.load_skill("backpack", "1", available_tools={"product_search_tool"})
    changed = {**doc, "body": "不能覆写已存在版本"}
    with pytest.raises(CapabilityError, match="不可覆盖"):
        registry.import_draft(changed, author="operator")
    registry.review("skill", "backpack", "1", reviewer="reviewer", evidence="逐项核对")
    registry.publish("skill", "backpack", "1", publisher="operator", evidence="审核记录1")
    hint = capability_hint(registry, {"product_search_tool"})
    assert doc["description"] in hint and doc["body"] not in hint
    loaded = registry.load_skill("backpack", "1", available_tools={"product_search_tool"})
    assert loaded["body"] == doc["body"] and len(loaded["content_hash"]) == 64
    with pytest.raises(CapabilityError, match="不会动态注册"):
        registry.load_skill("backpack", "1", available_tools=set())
    before = registry.version_fingerprint()
    registry.revoke("skill", "backpack", "1", actor="reviewer", reason="证据失效")
    assert registry.version_fingerprint() != before
    assert registry.metadata() == []
    with pytest.raises(CapabilityError, match="撤销"):
        registry.load_skill("backpack", "1", available_tools={"product_search_tool"})
    assert [entry["action"] for entry in registry.audit()] == ["import", "review", "publish", "revoke"]


@pytest.mark.parametrize("patch", [{"allowed_tools": ["execute_shell"]}, {"allowed_tools": ["remember_preference_tool"]},
    {"scope": "buyer:other"}, {"permissions": "allow_all"}, {"evidence": []}])
def test_draft_cannot_expand_tools_or_authority(tmp_path, patch):
    with pytest.raises(CapabilityError):
        CapabilityRegistry(tmp_path / "caps.db").import_draft({**document(), **patch}, author="operator")


def test_strategy_scope_evidence_expiry_revocation_and_no_preference_write(tmp_path, monkeypatch):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    doc = document("strategy")
    publish(registry, doc)
    assert registry.lookup_strategies("轻便背包", "shopping") == []
    assert registry.lookup_strategies("买耳机", "shopping:backpack") == []
    hit = registry.lookup_strategies("轻便背包", "shopping:backpack")[0]
    assert hit["authority"] == "advisory_only" and hit["evidence"] == doc["evidence"]
    digest = registry.bind_session("s", "b")
    assert registry.bind_session("s", "b") == digest
    monkeypatch.setattr("app.infrastructure.capability_registry.now", lambda: datetime(2100, 1, 1, tzinfo=timezone.utc))
    assert registry.lookup_strategies("背包", "shopping:backpack") == []
    assert registry.version_fingerprint() != digest
    with pytest.raises(CapabilityVersionChanged, match="新建"):
        registry.bind_session("s", "b")
    # 新会话可绑定不含过期策略的新集合，旧会话不静默接受变化。
    assert registry.bind_session("s-new", "b") != digest


def test_same_version_concurrent_import_and_tamper_fail_closed(tmp_path):
    path = tmp_path / "caps.db"
    def importing(index):
        try:
            return CapabilityRegistry(path).import_draft({**document(), "body": str(index)}, author="operator")
        except CapabilityError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(result is not None for result in pool.map(importing, range(4))) == 1
    with sqlite3.connect(path) as db:
        db.execute("UPDATE capabilities SET payload='{}'")
    with pytest.raises(CapabilityError, match="hash"):
        CapabilityRegistry(path).show("skill", "backpack", "1")


async def test_agent_tools_are_read_only_and_revalidate_session_before_body(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    publish(registry, document())
    functions = build_capability_tools(registry, {"product_search_tool"}, TradeEventBus())
    tools = [FunctionTool(function, is_read_only=True) for function in functions]
    assert {tool.name for tool in tools} == {"load_agent_skill_tool", "lookup_strategy_memory_tool"}
    token = ShoppingContext.set(ShoppingContextSnapshot("session", "buyer", "zh-CN", "CNY", ("plastic",)))
    try:
        result = await functions[0]("backpack", "1")
        assert document()["body"] in result.content[0].text
        publish(registry, document("strategy"))
        denied = await functions[0]("backpack", "1")
        assert "新建选购会话" in denied.content[0].text
        assert ShoppingContext.current().excluded_material_tags == ("plastic",)
    finally:
        ShoppingContext.reset(token)


async def test_session_restore_is_blocked_before_agent_build_after_revoke(tmp_path):
    from app.application.agents.main_agent import SessionRegistry
    from app.infrastructure.persistence.json_file_stores import JsonFileSessionStore
    capabilities = CapabilityRegistry(tmp_path / "caps.db")
    publish(capabilities, document())
    calls = []
    factory = SimpleNamespace(capability_registry=capabilities,
        build=lambda restored: calls.append("build") or SimpleNamespace(state=restored or AgentState(session_id="agent")))
    registry = SessionRegistry(factory, JsonFileSessionStore(tmp_path / "sessions"))
    token = ShoppingContext.set(ShoppingContextSnapshot("session", "buyer", "zh-CN", "CNY"))
    try:
        await registry.get_or_create("session")
        assert ShoppingContext.current().capability_digest == capabilities.version_fingerprint()
        await registry.persist("session")
        capabilities.revoke("skill", "backpack", "1", actor="reviewer", reason="误导建议")
        with pytest.raises(CapabilityVersionChanged):
            await registry.get_or_create("session")
        assert calls == ["build"]
    finally:
        ShoppingContext.reset(token)


def test_cli_requires_explicit_review_before_publish_and_preserves_evidence(tmp_path, capsys):
    path, source = tmp_path / "caps.db", tmp_path / "draft.json"
    source.write_text(json.dumps(document()), encoding="utf-8")
    prefix = ["--db", str(path)]
    main([*prefix, "import", str(source), "--author", "operator"])
    main([*prefix, "show", "skill", "backpack", "1"])
    assert document()["body"] in capsys.readouterr().out
    main([*prefix, "review", "skill", "backpack", "1", "--reviewer", "reviewer", "--evidence", "proof-123"])
    main([*prefix, "publish", "skill", "backpack", "1", "--publisher", "operator", "--evidence", "release-123"])
    restored = CapabilityRegistry(path)
    assert restored.show("skill", "backpack", "1")["review_evidence"] == "proof-123"
    assert restored.metadata()[0]["state"] == "published"


@pytest.mark.parametrize("kind", ["skill", "strategy"])
async def test_publish_between_bind_and_tool_read_cannot_mix_execution_digest(tmp_path, monkeypatch, kind):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    publish(registry, document(kind))
    pinned = registry.bind_session("race-session", "buyer")
    original_bind = registry.bind_session

    def publish_after_bind(session, buyer):
        digest = original_bind(session, buyer)
        publish(registry, {**document(kind, "2"), "body": "新发布正文，不得在旧digest中返回"})
        return digest

    monkeypatch.setattr(registry, "bind_session", publish_after_bind)
    functions = build_capability_tools(registry, {"product_search_tool"}, TradeEventBus())
    token = ShoppingContext.set(ShoppingContextSnapshot("race-session", "buyer", "zh-CN", "CNY", capability_digest=pinned))
    try:
        result = await (functions[0]("backpack", "1") if kind == "skill" else functions[1]("背包", "shopping:backpack"))
        assert "不能混用新资料" in result.content[0].text
        assert "新发布正文" not in result.content[0].text
        assert ShoppingContext.current().capability_digest == pinned
        assert registry.version_fingerprint() != pinned
    finally:
        ShoppingContext.reset(token)


def test_factory_hint_reads_metadata_with_same_pinned_digest(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.db")
    publish(registry, document())
    pinned = registry.bind_session("factory-session", "buyer")
    publish(registry, {**document(version="2"), "description": "绑定后的新摘要"})
    token = ShoppingContext.set(ShoppingContextSnapshot("factory-session", "buyer", "zh-CN", "CNY", capability_digest=pinned))
    try:
        with pytest.raises(CapabilityVersionChanged, match="不能混用"):
            capability_hint(registry, {"product_search_tool"})
    finally:
        ShoppingContext.reset(token)
