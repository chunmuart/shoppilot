# -*- coding: utf-8 -*-
"""AG-UI 合同测试：原生事件夹具替代付费模型，商品查询仍走真实 UseCase。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from ag_ui.core import RunAgentInput
from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import AssistantMsg, ToolResultState
from agentscope.types import ReplyFinishedReason
from fastapi import FastAPI

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.presentation.ag_ui import parse_intent, register_ag_ui_routes, stream_run


def request_data(**changes):
    data = {
        "threadId": "session-test", "runId": "run-test", "state": {},
        "messages": [
            {"id": "old-user", "role": "user", "content": "你好"},
            {"id": "old-assistant", "role": "assistant", "content": "需要什么商品？"},
            {"id": "new-user", "role": "user", "content": "旅行三件套"},
        ],
        "tools": [], "context": [], "forwardedProps": {"buyerId": "buyer-test"},
    }
    data.update(changes)
    return data


def decode_frames(frames):
    return [json.loads(line[6:]) for frame in frames for line in frame.splitlines() if line.startswith("data: ")]


class EmptyPreferences:
    async def list_by_buyer(self, buyer_id):
        return []


class Sessions:
    def __init__(self, agent):
        self.agent = agent
        self.persisted = 0

    async def get_or_create(self, session_id):
        return self.agent

    async def persist(self, session_id):
        self.persisted += 1


class ScriptedAgent:
    """脚本只决定模型会发哪些调用，商品与价格不使用测试伪造数据。"""

    name = "CatalogTestAgent"

    def __init__(self, bus, *, fail=False, block=False, swallow_cancel=False, query="旅行三件套"):
        self.bus = bus
        self.fail = fail
        self.block = block
        self.swallow_cancel = swallow_cancel
        self.query = query
        self.state = SimpleNamespace(summary=None, context=[])
        self.closed = asyncio.Event()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.usecase = CatalogSearchUseCase(InMemoryProductRepository())

    async def reply_stream(self, inputs, yield_final_msg=False):
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            self.started.set()
            yield ReplyStartEvent(session_id="session-test", reply_id="reply-1", name=self.name)
            yield TextBlockStartEvent(reply_id="reply-1", block_id="text-1")
            yield TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="正在查找商品。")
            if self.block:
                await self.release.wait()
            if self.fail:
                raise ValueError("测试执行失败")
            yield TextBlockEndEvent(reply_id="reply-1", block_id="text-1")
            yield ToolCallStartEvent(reply_id="reply-1", tool_call_id="call-1", tool_call_name="product_search_tool")
            yield ToolCallDeltaEvent(reply_id="reply-1", tool_call_id="call-1", delta=json.dumps({"normalized_query": self.query}))
            yield ToolCallEndEvent(reply_id="reply-1", tool_call_id="call-1")
            yield ToolResultStartEvent(reply_id="reply-1", tool_call_id="call-1", tool_call_name="product_search_tool")
            tool = build_product_search_tool(self.usecase, self.bus)
            result = await tool(normalized_query=self.query)
            yield ToolResultTextDeltaEvent(reply_id="reply-1", tool_call_id="call-1", delta=result.content[0].text)
            yield ToolResultEndEvent(reply_id="reply-1", tool_call_id="call-1", state=result.state)
            yield ReplyEndEvent(session_id="session-test", reply_id="reply-1")
            yield AssistantMsg(self.name, "这是根据商品库找到的结果。")
        except asyncio.CancelledError:
            if not self.swallow_cancel:
                raise
            # AgentScope 默认可将异常转换为事件，协议层也必须将其视为取消。
            yield ReplyEndEvent(session_id="session-test", reply_id="reply-1", finished_reason="interrupted")
            yield AssistantMsg(self.name, "本轮已中断")
        finally:
            self.in_flight -= 1
            self.closed.set()


def make_orchestrator(**kwargs):
    bus = TradeEventBus()
    agent = ScriptedAgent(bus, **kwargs)
    sessions = Sessions(agent)
    orchestrator = MainAgentOrchestrator(sessions, bus, EmptyPreferences())
    return orchestrator, agent, sessions


async def collect(orchestrator, **changes):
    body = RunAgentInput.model_validate(request_data(**changes))
    frames = [frame async for frame in stream_run(orchestrator, body, parse_intent(body))]
    return decode_frames(frames)


async def test_real_search_projects_cards_and_preserves_message_history():
    orchestrator, agent, sessions = make_orchestrator()
    events = await collect(orchestrator)
    types = [event["type"] for event in events]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert types.index("TOOL_CALL_END") < types.index("TOOL_CALL_RESULT")
    result = next(event for event in events if event["type"] == "TOOL_CALL_RESULT")
    search = json.loads(result["content"])
    final_state = [event["snapshot"] for event in events if event["type"] == "STATE_SNAPSHOT"][-1]
    from app.infrastructure.persistence.context_evidence import product_decision_view
    assert product_decision_view({"hits": final_state["products"]})["hits"] == search["hits"]
    assert "image_url" in final_state["products"][0] and "image_url" not in search["hits"][0]
    assert final_state["products"] and final_state["searchCompleted"] is True
    assert final_state["status"] == "completed"
    assert final_state["progress"][0]["status"] == "completed"
    final_messages = next(event["messages"] for event in events if event["type"] == "MESSAGES_SNAPSHOT")
    assert [message["id"] for message in final_messages[:3]] == ["old-user", "old-assistant", "new-user"]
    assert final_messages[-1]["content"] == "这是根据商品库找到的结果。"
    assert sessions.persisted == 1


async def test_empty_search_clears_client_products():
    orchestrator, _, _ = make_orchestrator(query="quantum flux capacitor")
    events = await collect(orchestrator, state={"products": [{"product_id": "fake-old-product"}]})
    snapshots = [event["snapshot"] for event in events if event["type"] == "STATE_SNAPSHOT"]
    assert snapshots[0]["products"] == [] and snapshots[0]["searchCompleted"] is False
    assert snapshots[-1]["products"] == [] and snapshots[-1]["searchCompleted"] is True


async def test_ag_ui_bypasses_text_only_semantic_cache_but_legacy_keeps_it():
    orchestrator, agent, _ = make_orchestrator()
    cache = SimpleNamespace(
        lookup=AsyncMock(return_value=SimpleNamespace(reply="缓存推荐文本", similarity=1.0, matched_query="旅行三件套")),
        remember=AsyncMock(),
    )
    orchestrator._semantic_cache = cache
    events = await collect(orchestrator)
    assert agent.calls == 1
    assert any(event["type"] == "TOOL_CALL_RESULT" for event in events)
    cache.lookup.assert_not_awaited()
    cache.remember.assert_not_awaited()
    body = RunAgentInput.model_validate(request_data())
    result = await orchestrator.handle_intent(parse_intent(body))
    assert result.final_text == "缓存推荐文本" and agent.calls == 1
    cache.lookup.assert_awaited_once()


@pytest.mark.parametrize("reason", list(ReplyFinishedReason))
def test_native_finished_reason_enum_is_recognized(reason):
    adapter = AGUIRunAdapter(RunAgentInput.model_validate(request_data()), lambda event: None)
    adapter.on_agent_event(ReplyEndEvent(session_id="session-test", reply_id="reply", finished_reason=reason))
    expected_error = str(reason).lower() in {"error", "interrupted", "exceed_max_iters"}
    assert (adapter.error is not None) == expected_error


@pytest.mark.parametrize("state", list(ToolResultState))
def test_native_tool_result_state_enum_is_recognized(state):
    adapter = AGUIRunAdapter(RunAgentInput.model_validate(request_data()), lambda event: None)
    adapter.on_agent_event(ToolResultEndEvent(reply_id="reply", tool_call_id="call", state=state))
    assert adapter.state["progress"][-1]["status"] == (
        "completed" if state == ToolResultState.SUCCESS else "error"
    )


async def test_failure_closes_message_and_ends_with_run_error():
    orchestrator, agent, sessions = make_orchestrator(fail=True)
    events = await collect(orchestrator)
    types = [event["type"] for event in events]
    assert types[-1] == "RUN_ERROR"
    assert "RUN_FINISHED" not in types
    assert types.count("TEXT_MESSAGE_START") == types.count("TEXT_MESSAGE_END") == 1
    assert agent.closed.is_set() and sessions.persisted == 1


def test_parallel_same_named_tools_keep_distinct_results():
    events = []
    adapter = AGUIRunAdapter(RunAgentInput.model_validate(request_data()), events.append)
    for identifier in ["call-a", "call-b"]:
        adapter.on_agent_event(ToolCallStartEvent(reply_id="reply", tool_call_id=identifier, tool_call_name="product_search_tool"))
        adapter.on_agent_event(ToolCallEndEvent(reply_id="reply", tool_call_id=identifier))
        adapter.on_agent_event(ToolResultStartEvent(reply_id="reply", tool_call_id=identifier, tool_call_name="product_search_tool"))
    for identifier in ["call-b", "call-a"]:
        adapter.on_agent_event(ToolResultTextDeltaEvent(reply_id="reply", tool_call_id=identifier, delta=identifier))
        adapter.on_agent_event(ToolResultEndEvent(reply_id="reply", tool_call_id=identifier, state=ToolResultState.SUCCESS))
    results = [event for event in events if event.type == "TOOL_CALL_RESULT"]
    assert [(event.tool_call_id, event.content) for event in results] == [
        ("run-test:tool:call-b", "call-b"), ("run-test:tool:call-a", "call-a"),
    ]


async def test_same_session_legacy_and_ag_ui_runs_are_serialized():
    orchestrator, agent, sessions = make_orchestrator(block=True)
    body = RunAgentInput.model_validate(request_data())
    first = asyncio.create_task(orchestrator.handle_intent(parse_intent(body)))
    await agent.started.wait()
    second = asyncio.create_task(collect(orchestrator))
    await asyncio.sleep(0.02)
    assert agent.calls == 1
    agent.release.set()
    await first
    await second
    assert agent.peak == 1 and sessions.persisted == 2


async def test_event_observers_are_task_scoped_including_children():
    bus = TradeEventBus()
    seen = [[], []]

    async def run(index):
        with observe_run_events(seen[index].append):
            async def child():
                await asyncio.sleep(0)
                bus.publish("same-session", "tool.result", {"owner": index})
            await asyncio.create_task(child())

    await asyncio.gather(run(0), run(1))
    assert [[event.payload["owner"] for event in events] for events in seen] == [[0], [1]]


async def test_http_endpoint_contract_and_reject_stale_resume():
    orchestrator, _, _ = make_orchestrator()
    app = FastAPI()
    register_ag_ui_routes(app, lambda: orchestrator)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/commerce/ag-ui/run", json=request_data())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert decode_frames([response.text])[-1]["type"] == "RUN_FINISHED"
        response = await client.post("/commerce/ag-ui/run", json=request_data(
            resume=[{"interruptId": "unavailable", "status": "resolved", "payload": {"approved": True}}],
        ))
        assert response.status_code == 200
        assert decode_frames([response.text])[-1]["type"] == "RUN_ERROR"


@pytest.mark.parametrize("swallow_cancel", [False, True])
async def test_http_disconnect_cancels_agent_and_persists_interruption(swallow_cancel):
    orchestrator, agent, sessions = make_orchestrator(block=True, swallow_cancel=swallow_cancel)
    conversations = SimpleNamespace(touch_session=AsyncMock(), append_turn=AsyncMock(), append_events=AsyncMock())
    orchestrator._conversation_store = conversations
    app = FastAPI()
    register_ag_ui_routes(app, lambda: orchestrator)
    disconnected = asyncio.Event()
    received_body = False
    responses = []

    async def receive():
        nonlocal received_body
        if not received_body:
            received_body = True
            return {"type": "http.request", "body": json.dumps(request_data()).encode(), "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        responses.append(message)
        if b"TEXT_MESSAGE_CONTENT" in message.get("body", b""):
            disconnected.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/commerce/ag-ui/run", "raw_path": b"/commerce/ag-ui/run",
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234), "server": ("test", 80), "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    assert disconnected.is_set() and agent.closed.is_set()
    assert agent.in_flight == 0 and sessions.persisted == 1
    saved_turns = [call.args[0] for call in conversations.append_turn.await_args_list]
    assert any(turn.role == "agent" and turn.content == "[cancelled] 本轮执行已中断" for turn in saved_turns)
    assert not orchestrator._session_locks["session-test"].locked()
    assert not any(b"RUN_FINISHED" in message.get("body", b"") for message in responses)

@pytest.mark.parametrize('kind',['capability','contract'])
async def test_old_session_version_returns_actionable_protocol_error(kind):
    from app.infrastructure.capability_registry import CapabilityVersionChanged
    from app.infrastructure.prompt_registry import PromptContractChanged
    orchestrator,agent,sessions=make_orchestrator()
    error=CapabilityVersionChanged('old') if kind=='capability' else PromptContractChanged('old')
    sessions.get_or_create=AsyncMock(side_effect=error)
    frames=[frame async for frame in stream_run(orchestrator,RunAgentInput.model_validate(request_data()),parse_intent(RunAgentInput.model_validate(request_data())))]
    last=decode_frames(frames)[-1]
    assert last['type']=='RUN_ERROR' and last['code']=='SESSION_VERSION_CHANGED'
    assert '旧记录仍保留' in last['message']
    assert agent.calls==0


async def test_context_capacity_error_is_actionable_and_never_retries_business():
    from app.infrastructure.context_governance import ContextCapacityError
    orchestrator,agent,sessions=make_orchestrator()
    orchestrator._reply_with_retry=AsyncMock(side_effect=ContextCapacityError('unsafe input'))
    body=RunAgentInput.model_validate(request_data())
    frames=[frame async for frame in stream_run(orchestrator,body,parse_intent(body))]
    last=decode_frames(frames)[-1]
    assert last['type']=='RUN_ERROR' and last['code']=='CONTEXT_CAPACITY_EXCEEDED'
    assert '分批比较' in last['message'] and '原始记录已保留' in last['message']
    assert agent.calls==0
    orchestrator._reply_with_retry.assert_awaited_once()
