"""AG-UI 确认投影/持久恢复，以及会话重载与明确失租时禁止保存。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ag_ui.core import RunAgentInput

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.usecases.order_usecases import OrderItemInput
from app.infrastructure.eventbus import TradeEvent
from app.presentation.ag_ui import parse_intent, stream_run
from tests.test_ag_ui import decode_frames, make_orchestrator, request_data
from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


async def prepare(env, buyer="buyer-test", session="session-test"):
    return await env.service.prepare_order(buyer, session, [OrderItemInput("P1001", "P1001-S1", 1)], address())


def emit_confirmation(adapter, confirmation, event_type="confirmation.required", session="session-test"):
    adapter.on_trade_event(TradeEvent(shopping_session_id=session, type=event_type,
        payload={"confirmation": confirmation}, occurred_at="2026-09-09T00:00:00+00:00"))


async def test_actual_prepared_and_resolved_confirmations_replace_same_card(confirmation_env):
    env = confirmation_env
    events = []
    adapter = AGUIRunAdapter(RunAgentInput.model_validate(request_data()), events.append)
    pending = (await prepare(env))["confirmation"]
    emit_confirmation(adapter, pending)
    assert adapter.state["confirmations"] == [pending]
    result = await env.service.resolve(pending["confirmation_id"], "buyer-test", "session-test", pending["snapshot_hash"], True)
    emit_confirmation(adapter, result["confirmation"], "confirmation.resolved")
    assert len(adapter.state["confirmations"]) == 1
    assert adapter.state["confirmations"][0]["status"] == "approved"
    assert events[-1].type == "STATE_SNAPSHOT"
    assert events[-1].snapshot["confirmations"][0]["result"]["status"] == "CONFIRMED"


@pytest.mark.parametrize("buyer,session,event_session", [
    ("buyer-other", "session-test", "session-test"),
    ("buyer-test", "session-other", "session-other"),
    ("buyer-test", "session-other", "session-test"),
])
async def test_confirmation_projection_rejects_wrong_payload_or_event_owner(confirmation_env, buyer, session, event_session):
    adapter = AGUIRunAdapter(RunAgentInput.model_validate(request_data()), lambda _event: None)
    other = (await prepare(confirmation_env, buyer, session))["confirmation"]
    emit_confirmation(adapter, other, session=event_session)
    assert adapter.state["confirmations"] == []


async def test_agui_run_restores_persistent_pending_and_decided_cards_ignoring_client_state(confirmation_env):
    env = confirmation_env
    decided = (await prepare(env))["confirmation"]
    await env.service.resolve(decided["confirmation_id"], "buyer-test", "session-test", decided["snapshot_hash"], False)
    await prepare(env)
    await prepare(env, session="other-session")
    await prepare(env, buyer="other-buyer")
    expected = (await env.service.list("buyer-test", "session-test"))["confirmations"]
    orchestrator = SimpleNamespace(handle_intent=AsyncMock(return_value=SimpleNamespace(error=None, final_text="已恢复会话")))
    body = RunAgentInput.model_validate(request_data(state={"confirmations": [{"confirmation_id": "client-forged", "status": "approved"}]}))
    frames = [frame async for frame in stream_run(orchestrator, body, parse_intent(body), env.service)]
    events = decode_frames(frames)
    states = [event["snapshot"] for event in events if event["type"] == "STATE_SNAPSHOT"]
    assert states[-1]["confirmations"] == expected
    assert len(expected) == 2 and {card["status"] for card in expected} == {"rejected", "pending"}
    assert events[-1]["type"] == "RUN_FINISHED"
    assert (await env.store.get_inventory())["P1001-S1"] == 50


async def test_agui_real_service_prepare_publishes_pending_card_without_creating_order(confirmation_env):
    env = confirmation_env

    class PreparingOrchestrator:
        async def handle_intent(self, intent, **_kwargs):
            await prepare(env, intent.buyer_id, intent.shopping_session_id)
            return SimpleNamespace(error=None, final_text="请确认订单明细")

    body = RunAgentInput.model_validate(request_data())
    frames = [frame async for frame in stream_run(PreparingOrchestrator(), body, parse_intent(body), env.service)]
    events = decode_frames(frames)
    state = [event["snapshot"] for event in events if event["type"] == "STATE_SNAPSHOT"][-1]
    assert len(state["confirmations"]) == 1
    assert state["confirmations"][0]["status"] == "pending"
    assert state["confirmations"][0]["result"] is None
    assert (await env.store.get_inventory())["P1001-S1"] == 50


async def test_fresh_session_invalidates_before_loading_and_clears_preference_marker():
    orchestrator, _, sessions = make_orchestrator()
    calls = []
    sessions.invalidate = AsyncMock(side_effect=lambda _session: calls.append("invalidate"))
    original = sessions.get_or_create

    async def load(session_id):
        calls.append("load")
        return await original(session_id)

    sessions.get_or_create = load
    orchestrator._injected_preferences["session-test"] = "stale-marker"
    body = RunAgentInput.model_validate(request_data())
    result = await orchestrator.handle_intent(parse_intent(body), fresh_session=True, use_semantic_cache=False)
    assert result.error is None
    assert calls[:2] == ["invalidate", "load"]
    assert orchestrator._injected_preferences.get("session-test") != "stale-marker"
    assert sessions.persisted == 1


async def test_known_lost_lease_never_persists_agent_or_conversation():
    orchestrator, _, sessions = make_orchestrator()
    sessions.invalidate = AsyncMock()
    conversation = SimpleNamespace(touch_session=AsyncMock(), append_turn=AsyncMock(), append_events=AsyncMock())
    orchestrator._conversation_store = conversation
    body = RunAgentInput.model_validate(request_data())
    await orchestrator.handle_intent(parse_intent(body), persistence_guard=lambda: False, use_semantic_cache=False)
    assert sessions.persisted == 0
    sessions.invalidate.assert_awaited_once_with("session-test")
    conversation.touch_session.assert_not_awaited()
    conversation.append_turn.assert_not_awaited()
    conversation.append_events.assert_not_awaited()


async def test_session_lease_automatically_reloads_and_combines_caller_guard():
    orchestrator, _, sessions = make_orchestrator()
    sessions.invalidate = AsyncMock()
    held = []

    @asynccontextmanager
    async def lease(session_id):
        held.append(session_id)
        try:
            yield SimpleNamespace(is_valid=lambda: True)
        finally:
            held.remove(session_id)

    orchestrator._session_lease_factory = lease
    body = RunAgentInput.model_validate(request_data())
    await orchestrator.handle_intent(parse_intent(body), persistence_guard=lambda: False, use_semantic_cache=False)
    assert sessions.persisted == 0
    assert sessions.invalidate.await_count == 2
    assert held == []


async def test_orchestrator_passes_recovered_trade_fact_to_model_and_bypasses_text_cache():
    orchestrator, _, _ = make_orchestrator()
    fact = {"orders": [{"order_id": "order-test", "status": "CANCELLED"}], "pending_confirmations": []}
    orchestrator._trade_state_provider = AsyncMock(return_value=fact)
    orchestrator._lookup_cache = AsyncMock(return_value="旧的订单回复")
    orchestrator._reply_with_retry = AsyncMock(return_value="这笔意向单已取消")
    body = RunAgentInput.model_validate(request_data())
    result = await orchestrator.handle_intent(parse_intent(body))
    assert result.final_text == "这笔意向单已取消"
    orchestrator._lookup_cache.assert_not_awaited()
    messages = orchestrator._reply_with_retry.call_args.args[2]
    assert "order-test" in str(messages[0].content)
    assert "CANCELLED" in str(messages[0].content)
