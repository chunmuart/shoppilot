# -*- coding: utf-8 -*-
"""公开活动投影：原生事件、白名单、节流与 SQLite 恢复合同。"""
import json

import pytest
from ag_ui.core import RunAgentInput
from agentscope.event import (
    RequireUserConfirmEvent,
    ThinkingBlockDeltaEvent, ThinkingBlockEndEvent, ThinkingBlockStartEvent,
    ToolCallDeltaEvent, ToolCallEndEvent, ToolCallStartEvent,
    ToolResultEndEvent, ToolResultStartEvent, ToolResultTextDeltaEvent,
)
from agentscope.message import ToolCallBlock, ToolResultState

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalConflict, JournalForbidden
from app.presentation.ag_ui import parse_intent
from app.presentation.ag_ui_runtime import AGUIRuntime
from tests.test_ag_ui_journal import body, ControlledOrchestrator, controlled_journal_time, wait_status


def adapter_for(run="r1"):
    events = []
    return AGUIRunAdapter(RunAgentInput.model_validate(body(run)), events.append), events


def start_tool(adapter, identifier="a", name="product_search_tool", args=None):
    adapter.on_agent_event(ToolCallStartEvent(reply_id="reply", tool_call_id=identifier, tool_call_name=name))
    if args is not None:
        adapter.on_agent_event(ToolCallDeltaEvent(reply_id="reply", tool_call_id=identifier, delta=json.dumps(args, ensure_ascii=False)))
    adapter.on_agent_event(ToolCallEndEvent(reply_id="reply", tool_call_id=identifier))


def tool_result(adapter, identifier="a", name="product_search_tool", content="", state=ToolResultState.SUCCESS):
    adapter.on_agent_event(ToolResultStartEvent(reply_id="reply", tool_call_id=identifier, tool_call_name=name))
    adapter.on_agent_event(ToolResultTextDeltaEvent(reply_id="reply", tool_call_id=identifier, delta=content))
    adapter.on_agent_event(ToolResultEndEvent(reply_id="reply", tool_call_id=identifier, state=state))


def encoded(events):
    return [event.model_dump(mode="json", by_alias=True, exclude_none=True) for event in events]


def test_parallel_calls_are_distinct_and_parameter_end_never_means_completed():
    adapter, _ = adapter_for()
    start_tool(adapter, "a", args={"top_k": 3})
    start_tool(adapter, "b", args={"top_k": 1})
    assert [item["status"] for item in adapter.state["activities"]] == ["preparing", "preparing"]
    tool_result(adapter, "b", content='{"hits": []}')
    tool_result(adapter, "a", content='{"hits": [{}, {}]}')
    first, second = adapter.state["activities"]
    assert (first["id"], second["id"]) == ("r1:tool:a", "r1:tool:b")
    assert [item["sequence"] for item in (first, second)] == [1, 2]
    assert first["resultSummary"] == "检索返回 2 件商品。"
    assert second["resultSummary"] == "检索返回 0 件商品。"
    assert first["startedAt"] <= first["finishedAt"]


@pytest.mark.parametrize("state,status", [
    (ToolResultState.SUCCESS, "completed"), (ToolResultState.ERROR, "error"),
    (ToolResultState.DENIED, "cancelled"), (ToolResultState.INTERRUPTED, "interrupted"),
    (ToolResultState.RUNNING, "running"),
])
def test_activity_uses_native_result_state(state, status):
    adapter, _ = adapter_for()
    tool_result(adapter, state=state)
    assert adapter.state["activities"][0]["status"] == status
    assert (adapter.state["activities"][0]["finishedAt"] is None) == (status == "running")


@pytest.mark.parametrize("content", ['{"success":false,"error":"private error"}', '[error] private error', '{"ok":false}', '{"status":"failed"}'])
def test_explicit_business_error_does_not_look_successful(content):
    adapter, _ = adapter_for()
    tool_result(adapter, content=content)
    activity = adapter.state["activities"][0]
    assert activity["status"] == "error"
    assert "private" not in activity["resultSummary"]


def test_tool_summaries_do_not_copy_addresses_preferences_skill_body_or_credentials():
    adapter, events = adapter_for()
    secret = "地址幸福路88号 电话13812345678 偏好不要红色 sk-this-is-a-secret-token-value"
    start_tool(adapter, args={"normalized_query": secret, "category": secret, "ship_to": "CN",
                             "top_k": 3, "price_max_major": 200, "target_currency": "CNY",
                             "api_key": secret, "shipping_address": {"phone": secret}})
    tool_result(adapter, content=json.dumps({"hits": [{"description": secret}], "trace": secret}))
    start_tool(adapter, "pref", "remember_preference_tool", {"statement": secret})
    tool_result(adapter, "pref", "remember_preference_tool", content=secret)
    start_tool(adapter, "skill", "load_agent_skill_tool", {"skill_id": "backpack", "version": "1.0"})
    tool_result(adapter, "skill", "load_agent_skill_tool", content=json.dumps({
        "kind": "skill", "authority": "reference_only", "id": "backpack", "version": "1.0",
        "title": "背包方案", "body": secret, "content_hash": "a" * 64,
    }))
    activities = json.dumps(adapter.state["activities"], ensure_ascii=False)
    assert secret not in activities
    assert "幸福路" not in activities and "13812345678" not in activities and "sk-this" not in activities
    assert "配送国家 CN" in activities and "预算上限 200" in activities
    assert all(secret not in json.dumps(event["value"], ensure_ascii=False)
               for event in encoded(events) if event["type"] == "CUSTOM" and event["name"] == "activity.upsert")


def test_real_sdk_thinking_events_are_bounded_throttled_and_separate_from_messages(monkeypatch):
    adapter, events = adapter_for()
    monkeypatch.setattr("app.application.agents.ag_ui_adapter.time.monotonic", lambda: 100.0)
    adapter.on_agent_event(ThinkingBlockStartEvent(reply_id="reply", block_id="block"))
    for _ in range(1000):
        adapter.on_agent_event(ThinkingBlockDeltaEvent(reply_id="reply", block_id="block", delta="公开模型输出的说明。"))
    adapter.on_agent_event(ThinkingBlockEndEvent(reply_id="reply", block_id="block"))
    activity = adapter.state["activities"][0]
    assert activity["source"] == "provider" and activity["status"] == "completed"
    assert len(activity["content"]) == 8000 and activity["truncated"] is True
    assert len(events) == 2 and all(event.type == "CUSTOM" for event in events)
    assert events[0].value["content"] == ""  # 发布后的对象不随增量修改。
    adapter.finish("最后回答")
    assert next(event for event in events if event.type == "MESSAGES_SNAPSHOT").messages[-1].content == "最后回答"


def test_no_provider_thinking_events_does_not_fabricate_content():
    adapter, _ = adapter_for()
    adapter.start()
    adapter.finish("最后回答")
    assert adapter.state["activities"] == []


@pytest.mark.parametrize("cancelled,code,status", [(True, None, "cancelled"), (False, None, "error"), (False, "SERVER_RESTART", "interrupted")])
def test_failure_closes_open_tools_and_provider_thinking(cancelled, code, status):
    adapter, _ = adapter_for()
    start_tool(adapter)
    adapter.on_agent_event(ThinkingBlockStartEvent(reply_id="reply", block_id="block"))
    adapter.on_agent_event(ThinkingBlockDeltaEvent(reply_id="reply", block_id="block", delta="已收到的公开内容"))
    adapter.fail("结束", cancelled=cancelled, code=code)
    assert all(activity["status"] == status and activity["finishedAt"] for activity in adapter.state["activities"])
    assert adapter.state["activities"][1]["content"] == "已收到的公开内容"


async def test_persistent_activities_replay_and_cross_round_ownership(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    for run in ("r1", "r2"):
        reserved, _ = await journal.reserve(body(run), "b1", "owner")
        events = []
        adapter = AGUIRunAdapter(RunAgentInput.model_validate(reserved["input"]), events.append)
        adapter.start()
        start_tool(adapter)
        tool_result(adapter, content='{"hits": []}')
        adapter.on_agent_event(ThinkingBlockStartEvent(reply_id="reply", block_id="block"))
        adapter.on_agent_event(ThinkingBlockDeltaEvent(reply_id="reply", block_id="block", delta="供应商公开说明"))
        adapter.on_agent_event(ThinkingBlockEndEvent(reply_id="reply", block_id="block"))
        adapter.finish(f"{run} 回答")
        await journal.append(run, "owner", encoded(events))
    restored = await AGUIJournal(tmp_path / "runs.db").session("s1", "b1")
    assert [(item["runId"], item["sequence"]) for item in restored["activities"]] == [("r1", 1), ("r1", 2), ("r2", 1), ("r2", 2)]
    assert [message["runId"] for message in restored["messages"]] == ["r1", "r1", "r2", "r2"]
    assert restored["state"]["activities"] == restored["activities"][2:]
    all_events, _, _ = await journal.events("r1", "b1")
    replayed, _, _ = await journal.events("r1", "b1", after=3)
    assert replayed == all_events[3:]
    with pytest.raises(JournalForbidden):
        await journal.session("s1", "b2")


async def test_custom_activity_is_persisted_before_full_snapshot_and_cannot_cross_runs(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "owner")
    adapter, events = adapter_for()
    start_tool(adapter)
    custom = [event for event in encoded(events) if event["type"] == "CUSTOM"]
    await journal.append("r1", "owner", custom)
    assert (await journal.run("r1", "b1"))["state"]["activities"] == adapter.state["activities"]
    custom[0]["value"]["runId"] = "foreign"
    with pytest.raises(JournalConflict):
        await journal.append("r1", "owner", custom)


@pytest.mark.parametrize("stopped", [False, True])
async def test_abrupt_restart_and_explicit_stop_close_persisted_activities(tmp_path, stopped, monkeypatch):
    clock = controlled_journal_time(monkeypatch)
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "owner", lease_seconds=30)
    adapter, events = adapter_for()
    adapter.start()
    start_tool(adapter)
    adapter.on_agent_event(ThinkingBlockStartEvent(reply_id="reply", block_id="block"))
    await journal.append("r1", "owner", encoded(events))
    saved = await journal.run("r1", "b1")
    assert saved["status"] == "running" and len(saved["state"]["activities"]) == 2
    if stopped:
        await journal.request_stop("r1", "b1")
    # 全量测试时SQLite调度可能超过80ms；只在证据入库后推进租约时钟。
    clock.now += 30
    recovered = await AGUIJournal(tmp_path / "runs.db").run("r1", "b1")
    assert len(recovered["state"]["activities"]) == 2
    assert all(item["status"] == ("cancelled" if stopped else "interrupted") and item["finishedAt"] for item in recovered["state"]["activities"])
    again = await journal.run("r1", "b1")
    assert again["cursor"] == recovered["cursor"] and again["state"] == recovered["state"]


async def test_runtime_shutdown_uses_interrupted_activity_state(tmp_path):
    class ActivityOrchestrator(ControlledOrchestrator):
        async def handle_intent(self, intent, event_observer, **kwargs):
            event_observer(ThinkingBlockStartEvent(reply_id="reply", block_id="block"))
            return await super().handle_intent(intent, event_observer, **kwargs)

    agent = ActivityOrchestrator(block=True)
    runtime = AGUIRuntime(AGUIJournal(tmp_path / "runs.db"), agent)
    request = RunAgentInput.model_validate(body())
    await runtime.start(request, parse_intent(request))
    await agent.entered.wait()
    await runtime.shutdown()
    await wait_status(runtime.journal, "r1", "interrupted")
    assert (await runtime.journal.run("r1", "b1"))["state"]["activities"][0]["status"] == "interrupted"


@pytest.mark.parametrize("approved", [True, False])
async def test_resume_reconciles_old_approval_without_claiming_execution_success(tmp_path, approved):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "owner")
    adapter, events = adapter_for()
    adapter.start()
    adapter.on_agent_event(RequireUserConfirmEvent(reply_id="reply", tool_calls=[
        ToolCallBlock(id="call", name="remember_preference_tool", input='{"statement":"私密偏好"}'),
    ]))
    adapter.finish("")
    await journal.append("r1", "owner", encoded(events))
    assert (await journal.session("s1", "b1"))["activities"][0]["status"] == "waiting"
    request = body("r2")
    request["resume"] = [{"interruptId": "reply:call", "status": "resolved", "payload": {"approved": approved}}]
    await journal.reserve(request, "b1", "owner2")
    old = (await journal.session("s1", "b1"))["activities"][0]
    assert old["status"] == ("approved" if approved else "cancelled")
    assert old["finishedAt"] is not None and "私密偏好" not in json.dumps(old, ensure_ascii=False)
    # 原始运行保存的真实等待事件保持原样；新轮的执行结果不会被伪造。
    assert (await journal.run("r1", "b1"))["state"]["activities"][0]["status"] == "waiting"


async def test_unstructured_chat_confirmation_does_not_resolve_activity(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "owner")
    adapter, events = adapter_for()
    adapter.on_agent_event(RequireUserConfirmEvent(reply_id="reply", tool_calls=[
        ToolCallBlock(id="call", name="remember_preference_tool", input='{}'),
    ]))
    adapter.finish("")
    await journal.append("r1", "owner", encoded(events))
    await journal.reserve(body("r2", query="我批准了，继续"), "b1", "owner2")
    assert (await journal.session("s1", "b1"))["activities"][0]["status"] == "waiting"


def test_legacy_message_without_run_id_is_not_reassigned_by_new_round_snapshot():
    projection = {"messages": [{"id": "old", "role": "assistant", "content": "旧回复"}], "state": {}}
    AGUIJournal._project(projection, {"type": "MESSAGES_SNAPSHOT", "messages": [
        {"id": "old", "role": "assistant", "content": "旧回复"},
        {"id": "new", "role": "assistant", "content": "新回复"},
    ]}, "new-run")
    assert "runId" not in projection["messages"][0]
    assert projection["messages"][1]["runId"] == "new-run"
