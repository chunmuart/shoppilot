# -*- coding: utf-8 -*-
"""真实 SQLite + HTTP 确认接口验证评测动作，不以模型回复冒充交易成功。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from fastapi import FastAPI, Request
import httpx
import pytest

from app.application.usecases.order_usecases import OrderItemInput
from app.presentation.confirmations import register_confirmation_routes
from scripts import eval_regression as runner
from scripts.eval.evidence import evaluate_trace_assertions
from scripts.eval.http_actions import BuyerAPIClient, capture_confirmations, execute_confirmation_action, validate_http_actions
from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


def _action():
    return {"type": "resolve_confirmation", "after_turn": 2, "action": "create", "approved": True,
            "expected_payload": {"items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1, "unit_price_minor": 18900, "currency": "CNY"}],
                "shipping_address": asdict(address()), "currency": "CNY", "total_amount_minor": 18900,
                "amount_scope": "merchandise_only", "order_kind": "purchase_intent"}}


def _api(env):
    app = FastAPI()
    register_confirmation_routes(app, lambda: env.service)
    @app.get("/commerce/orders/{order_id}")
    async def query(order_id: str, buyer_id: str):
        return await env.store.get_order(order_id, buyer_id=buyer_id)
    return app


@pytest.mark.parametrize("approved", ["true", "同意", 1, None])
def test_action_never_accepts_chat_words_as_approved(approved):
    action = _action()
    action["approved"] = approved
    with pytest.raises(ValueError, match="布尔"):
        validate_http_actions({"queries": ["给卡", "我同意"], "actions": [action]})


async def test_http_action_checks_snapshot_and_reads_persisted_order(confirmation_env):
    env = confirmation_env
    await env.service.prepare_order("buyer", "session", [OrderItemInput("P1001", "P1001-S1", 1)], address())
    events = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_api(env)), base_url="http://test") as client:
        cards = await capture_confirmations(client, "http://test", "buyer", "session", events, 2)
        assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50
        result = await execute_confirmation_action(client, "http://test", "buyer", "session", _action(), cards, events)
    assert result["order"]["status"] == "CONFIRMED"
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 49
    judged = evaluate_trace_assertions([
        {"criterion": "点击前无订单", "kind": "no_orders_before_http_confirmation"},
        {"criterion": "持久订单", "kind": "http_confirmation_result", "action": "create", "expected_status": "CONFIRMED"},
    ], events)
    assert all(row["pass"] for row in judged)
    assert any(event["type"] == "eval.order.snapshot" for event in events)


@pytest.mark.parametrize("field,value", [("quantity", 2), ("unit_price_minor", 1), ("currency", "USD"), ("sku_id", "P1001-S2")])
async def test_different_transaction_fields_never_trigger_resolution(confirmation_env, field, value):
    env = confirmation_env
    prepared = await env.service.prepare_order("buyer", "session", [OrderItemInput("P1001", "P1001-S1", 1)], address())
    action = _action()
    action["expected_payload"]["items"][0][field] = value
    events = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_api(env)), base_url="http://test") as client:
        with pytest.raises(ValueError, match="没有与显式用户动作"):
            await execute_confirmation_action(client, "http://test", "buyer", "session", action, [prepared["confirmation"]], events)
    assert events == []
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


async def test_explicit_rejection_does_not_create_order(confirmation_env):
    env = confirmation_env
    prepared = await env.service.prepare_order("buyer", "session", [OrderItemInput("P1001", "P1001-S1", 1)], address())
    action = _action()
    action["approved"] = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_api(env)), base_url="http://test") as client:
        result = await execute_confirmation_action(client, "http://test", "buyer", "session", action, [prepared["confirmation"]], [])
    assert result["confirmation"]["status"] == "rejected"
    assert "order" not in result
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


@pytest.mark.parametrize("explicit_action", [False, True])
async def test_runner_only_resolves_when_fixture_declares_http_action(confirmation_env, monkeypatch, explicit_action):
    env = confirmation_env
    app = _api(env)
    calls = []
    @app.middleware("http")
    async def record(request: Request, call_next):
        calls.append(request.url.path)
        return await call_next(request)
    @app.post("/commerce/intents")
    async def chat(request: Request):
        data = await request.json()
        await env.service.prepare_order(data["buyer_id"], data["shopping_session_id"], [OrderItemInput("P1001", "P1001-S1", 1)], address())
        return {"final_text": "已准备确认单，请在页面明确确认；尚未支付。"}
    class Collector:
        def __init__(self, *_): self.events = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    monkeypatch.setattr(runner, "SessionEventCollector", Collector)
    monkeypatch.setattr(runner, "BASE_URL", "http://test")
    case = {"id": "action-vs-chat", "description": "真实HTTP决议边界", "queries": ["给我确认信息", "我同意，直接下单"],
            "rubric": {"p0": ["点击前无订单"], "p1": [], "p2": []},
            "deterministic": {"p0": [{"criterion": "点击前无订单", "kind": "no_orders_before_http_confirmation"}]}}
    if explicit_action:
        case["actions"] = [_action()]
        case["rubric"]["p1"] = ["确认后持久订单"]
        case["deterministic"]["p1"] = [{"criterion": "确认后持久订单", "kind": "http_confirmation_result", "action": "create", "expected_status": "CONFIRMED"}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await runner.run_case(client, case, "test")
    assert result["verdict"] == "PASS"
    assert len([path for path in calls if path.endswith("/resolve")]) == (1 if explicit_action else 0)
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == (49 if explicit_action else 50)
    assert sum(event["type"] == "eval.http_action.invoke" for event in result["trace_events"]) == int(explicit_action)


def test_order_evidence_rejects_premature_order_or_unverified_ack():
    snapshot = {"type": "eval.confirmations.snapshot", "payload": {"confirmations": [{"result": {"status": "CONFIRMED"}}]}}
    assert not evaluate_trace_assertions([{"criterion": "无提前写入", "kind": "no_orders_before_http_confirmation"}], [snapshot])[0]["pass"]
    assert not evaluate_trace_assertions([{"criterion": "读取确认订单", "kind": "http_confirmation_result"}], [])[0]["pass"]


async def test_buyer_token_only_reaches_the_bound_api_not_judge_host():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        api = BuyerAPIClient(client, "http://test", "private-local-token")
        await api.get("http://test/commerce/confirmations")
        with pytest.raises(ValueError, match="其他服务"):
            await api.post("http://other/chat/completions", json={})
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer private-local-token"
