"""真实 SQLite Skill/owner/run 日志闭环；只替换模型，不访问远端。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace

from ag_ui.core import RunAgentInput
from agentscope.event import ReplyStartEvent, ReplyEndEvent
from agentscope.message import AssistantMsg
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit
from fastapi import HTTPException
import pytest

from app.application.agents.main_agent import SessionRegistry
from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
from app.application.agents.selected_skill import SelectedSkill
from app.application.tools.capability_tools import build_capability_tools
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalConflict
from app.infrastructure.capability_registry import CapabilityRegistry
from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from app.infrastructure.persistence.sql.repositories import SqlSessionStore, create_engine
from app.presentation.ag_ui import parse_intent, stream_run
from app.presentation.ag_ui_runtime import AGUIRuntime
from tests.test_ag_ui import EmptyPreferences, decode_frames, request_data
from tests.test_capability_registry import document, publish


class RecordingAgent:
    name = "SelectionFixtureAgent"

    def __init__(self, toolkit, state=None):
        self.toolkit = toolkit
        self.state = state or AgentState(session_id="native-test")
        self.calls = []

    async def reply_stream(self, inputs, yield_final_msg=False):
        self.calls.append([message.model_copy(deep=True) for message in inputs or []])
        self.state.context.extend(inputs or [])
        yield ReplyStartEvent(session_id="native-test", reply_id="reply", name=self.name)
        yield ReplyEndEvent(session_id="native-test", reply_id="reply")
        answer = AssistantMsg(self.name, "已按已读方案整理本轮选购建议。")
        self.state.context.append(answer)
        yield answer


@pytest.fixture
async def fixture(tmp_path):
    registry = CapabilityRegistry(tmp_path / "capabilities.db")
    metadata = publish(registry, document())
    bus = TradeEventBus()
    async def product_search_tool(query: str):
        raise AssertionError("本测试不能调商品或远端模型")
    toolkit = Toolkit(tools=[FunctionTool(product_search_tool), *[
        FunctionTool(function, is_read_only=True) for function in
        build_capability_tools(registry, {"product_search_tool"}, bus)]])
    agents = []
    def build(state=None):
        agent = RecordingAgent(toolkit, state)
        agents.append(agent)
        return agent
    factory = SimpleNamespace(capability_registry=registry, build=build)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    sessions = SessionRegistry(factory, SqlSessionStore(engine))
    orchestrator = MainAgentOrchestrator(sessions, bus, EmptyPreferences())
    selected = {"id": "backpack", "version": "1", "contentHash": metadata["content_hash"]}
    yield SimpleNamespace(registry=registry, selected=selected, bus=bus, sessions=sessions,
        orchestrator=orchestrator, agents=agents, toolkit=toolkit, directory=tmp_path)
    await engine.dispose()


def body(fixture, **changes):
    return RunAgentInput.model_validate(request_data(forwardedProps={
        "buyerId": "buyer-test", "selectedSkill": fixture.selected}, **changes))


async def stream(fixture, request=None):
    request = request or body(fixture)
    return decode_frames([frame async for frame in stream_run(fixture.orchestrator, request, parse_intent(request))])


@pytest.mark.parametrize("selection", [None, [], {}, {"id": "x", "version": "1"},
    {"id": "../private", "version": "1", "contentHash": "a" * 64},
    {"id": True, "version": "1", "contentHash": "a" * 64},
    {"id": "x", "version": 1, "contentHash": "a" * 64},
    {"id": "x", "version": "1", "contentHash": "A" * 64},
    {"id": "x", "version": "1", "contentHash": "a" * 64, "body": "客户端注入正文"},
    {"id": "x", "version": "1", "contentHash": "a" * 64, "allowed_tools": ["execute_shell"]},
])
def test_selected_skill_strict_input_never_accepts_body_authority_or_coercion(selection):
    request = RunAgentInput.model_validate(request_data(forwardedProps={"buyerId": "buyer", "selectedSkill": selection}))
    with pytest.raises(HTTPException) as error:
        parse_intent(request)
    assert error.value.status_code == 422


async def test_authoritative_preload_is_before_first_model_and_not_fake_model_tool(fixture, monkeypatch):
    loaded_before_model = []
    original = fixture.registry.load_skill
    def load(*args, **kwargs):
        assert not any(agent.calls for agent in fixture.agents)
        loaded_before_model.append(kwargs["expected_digest"])
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture.registry, "load_skill", load)
    events = await stream(fixture)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert len(loaded_before_model) == 1
    assert len(fixture.agents[0].calls) == 1
    reference = next(message for message in fixture.agents[0].calls[0] if message.name == "selected_skill_reference")
    assert document()["body"] in reference.get_text_content()
    assert '"authority": "reference_only"' in reference.get_text_content()
    usages = [event["snapshot"]["skillUsages"] for event in events if event["type"] == "STATE_SNAPSHOT"]
    assert any(items and items[0]["status"] == "reading" for items in usages)
    assert usages[-1][0] == {"toolCallId": usages[-1][0]["toolCallId"], "source": "server_preload", "status": "used",
        **fixture.selected, "title": document()["title"]}
    assert not any(event["type"].startswith("TOOL_CALL") for event in events)
    assert document()["body"] not in json.dumps(events, ensure_ascii=False)
    assert all(event["name"] == "skill.preload" for event in events if event["type"] == "CUSTOM")


@pytest.mark.parametrize("failure", ["hash", "missing", "draft", "revoked", "expired", "tool_missing", "dependency_missing"])
async def test_invalid_selection_is_error_without_any_model_call(fixture, failure, monkeypatch):
    if failure == "hash": fixture.selected["contentHash"] = "f" * 64
    if failure == "missing": fixture.selected["version"] = "missing"
    if failure == "draft":
        draft = fixture.registry.import_draft(document(version="2"), author="test")
        fixture.selected.update(version="2", contentHash=draft["content_hash"])
    if failure == "revoked": fixture.registry.revoke("skill", "backpack", "1", actor="test", reason="测试撤销")
    if failure == "expired": monkeypatch.setattr("app.infrastructure.capability_registry.now", lambda: datetime(2100, 1, 1, tzinfo=timezone.utc))
    if failure == "tool_missing": await fixture.toolkit.remove_tool("load_agent_skill_tool")
    if failure == "dependency_missing": await fixture.toolkit.remove_tool("product_search_tool")
    events = await stream(fixture)
    assert events[-1]["type"] == "RUN_ERROR"
    assert "所选方案" in events[-1]["message"]
    assert not any(agent.calls for agent in fixture.agents)
    usages = [event["snapshot"]["skillUsages"] for event in events if event["type"] == "STATE_SNAPSHOT"]
    assert usages[-1][0]["status"] == "error"
    assert not any(items and items[0]["status"] == "used" for items in usages)


async def test_owner_and_pinned_session_fail_before_selected_body(fixture):
    await stream(fixture)
    initial_calls = len(fixture.agents[0].calls)
    wrong_owner = RunAgentInput.model_validate(request_data(runId="other-run", forwardedProps={
        "buyerId": "other-buyer", "selectedSkill": fixture.selected}))
    events = await stream(fixture, wrong_owner)
    assert events[-1]["type"] == "RUN_ERROR" and len(fixture.agents[0].calls) == initial_calls
    publish(fixture.registry, document("strategy"))
    events = await stream(fixture, body(fixture, runId="changed-run"))
    assert events[-1]["type"] == "RUN_ERROR" and sum(len(agent.calls) for agent in fixture.agents) == initial_calls


async def test_publish_between_bind_and_load_rejects_old_digest(fixture, monkeypatch):
    original = fixture.registry.load_skill
    def load(*args, **kwargs):
        publish(fixture.registry, document("strategy"))
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture.registry, "load_skill", load)
    events = await stream(fixture)
    assert events[-1]["type"] == "RUN_ERROR" and not fixture.agents[0].calls


async def test_selection_disables_semantic_cache_and_old_reference_removed_next_turn(fixture, monkeypatch):
    async def cache(*_args):
        raise AssertionError("显式选择不能由文本缓存代替读取")
    monkeypatch.setattr(fixture.orchestrator, "_lookup_cache", cache)
    request = body(fixture)
    assert not (await fixture.orchestrator.handle_intent(parse_intent(request))).error
    assert any(msg.name == "selected_skill_reference" for msg in fixture.agents[0].state.context)
    normal = RunAgentInput.model_validate(request_data(runId="normal", forwardedProps={"buyerId": "buyer-test"}))
    result = await fixture.orchestrator.handle_intent(parse_intent(normal), use_semantic_cache=False)
    assert not result.error
    assert not any(msg.name == "selected_skill_reference" for msg in fixture.agents[0].state.context)


async def test_cancel_during_sqlite_preload_never_reports_used_or_calls_model(fixture, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = fixture.registry.load_skill
    def delayed(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture.registry, "load_skill", delayed)
    captured = []
    with observe_run_events(captured.append):
        task = asyncio.create_task(fixture.orchestrator.handle_intent(parse_intent(body(fixture))))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            release.set()
    assert not fixture.agents[0].calls
    assert [event.payload["status"] for event in captured if event.type == "skill.preload"] == ["reading", "error"]
    assert ShoppingContext.current() is None


async def test_lost_lease_after_preload_refuses_success_and_model(fixture, monkeypatch):
    valid = True
    original = fixture.registry.load_skill
    def load(*args, **kwargs):
        nonlocal valid
        result = original(*args, **kwargs)
        valid = False
        return result
    monkeypatch.setattr(fixture.registry, "load_skill", load)
    result = await fixture.orchestrator.handle_intent(parse_intent(body(fixture)), persistence_guard=lambda: valid)
    assert result.error and not fixture.agents[0].calls
    assert ShoppingContext.current() is None


async def test_journal_identity_includes_selection_and_preserves_original_for_resume(fixture):
    journal = AGUIJournal(fixture.directory / "runs.db")
    request = body(fixture).model_dump(mode="json", by_alias=True)
    run, created = await journal.reserve(request, "buyer-test", "owner")
    assert created and run["input"]["forwardedProps"]["selectedSkill"] == fixture.selected
    assert not (await journal.reserve(deepcopy(request), "buyer-test", "owner2"))[1]
    for key, value in [("id", "other"), ("version", "2"), ("contentHash", "f" * 64)]:
        changed = deepcopy(request)
        changed["forwardedProps"]["selectedSkill"][key] = value
        with pytest.raises(JournalConflict): await journal.reserve(changed, "buyer-test", "owner3")
    changed = deepcopy(request)
    changed["forwardedProps"].pop("selectedSkill")
    with pytest.raises(JournalConflict): await journal.reserve(changed, "buyer-test", "owner3")


async def test_persistent_runtime_replay_has_real_preload_without_reexecuting(fixture, monkeypatch):
    count = 0
    original = fixture.registry.load_skill
    def load(*args, **kwargs):
        nonlocal count
        count += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture.registry, "load_skill", load)
    journal = AGUIJournal(fixture.directory / "runs.db")
    runtime = AGUIRuntime(journal, fixture.orchestrator)
    request = body(fixture)
    try:
        await runtime.start(request, parse_intent(request))
        async with asyncio.timeout(3):
            while (await journal.run(request.run_id, "buyer-test"))["status"] == "running":
                await asyncio.sleep(0.01)
        recovered = await journal.run(request.run_id, "buyer-test")
        assert recovered["status"] == "completed"
        assert recovered["state"]["skillUsages"][0]["status"] == "used"
        assert recovered["state"]["skillUsages"][0]["source"] == "server_preload"
        await runtime.start(request, parse_intent(request))
        assert count == 1 and len(fixture.agents[0].calls) == 1
        events, status, _ = await journal.events(request.run_id, "buyer-test")
        assert status == "completed" and any(event["event"].get("name") == "skill.preload" for event in events)
        assert document()["body"] not in json.dumps(events, ensure_ascii=False)
    finally:
        await runtime.shutdown()
