"""确认 HTTP 合同：真实 SQLite/service，模型、向量服务均不参与。"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.application.usecases.order_usecases import CancelOrderUseCase, QueryOrderUseCase
from app.infrastructure.persistence.sql.tables import OrderRow
from app.presentation.confirmations import register_confirmation_routes
from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


def order_request(**changes):
    return {"buyer_id": "buyer-1", "session_id": "session-1",
            "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
            "shipping_address": asdict(address()), **changes}


def decision(confirmation, **changes):
    return {"buyer_id": "buyer-1", "session_id": "session-1",
            "snapshot_hash": confirmation["snapshot_hash"], "approved": True, **changes}


@pytest.fixture
async def route_env(confirmation_env):
    api = FastAPI()
    register_confirmation_routes(api, lambda: confirmation_env.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield confirmation_env, client


async def prepare(client, **changes):
    result = await client.post("/commerce/confirmations/orders", json=order_request(**changes))
    assert result.status_code == 200, result.text
    return result.json()["confirmation"]


async def test_http_prepare_persists_only_confirmation_and_get_list_restore_it(route_env):
    env, client = route_env
    prepared = await prepare(client)
    assert prepared["status"] == "pending" and prepared["result"] is None
    assert prepared["payload"]["items"][0]["unit_price_minor"] == 18900
    assert prepared["payload"]["total_amount_minor"] == 18900
    assert (await env.store.get_inventory())["P1001-S1"] == 50
    async with env.engine.connect() as db:
        assert await db.scalar(select(func.count()).select_from(OrderRow)) == 0
    params = {"buyer_id": "buyer-1", "session_id": "session-1"}
    retrieved = await client.get(f"/commerce/confirmations/{prepared['confirmation_id']}", params=params)
    assert retrieved.status_code == 200 and retrieved.json()["confirmation"] == prepared
    listing = await client.get("/commerce/confirmations", params=params)
    assert listing.json()["confirmations"] == [prepared]


@pytest.mark.parametrize("quantity", [True, False, "1", 1.0, 0, -1])
async def test_http_quantities_are_strict_positive_integers(route_env, quantity):
    env, client = route_env
    response = await client.post("/commerce/confirmations/orders", json=order_request(items=[
        {"product_id": "P1001", "sku_id": "P1001-S1", "quantity": quantity}]))
    assert response.status_code == 422
    assert await env.store.list_confirmations(buyer_id="buyer-1", session_id="session-1") == []


@pytest.mark.parametrize("approved", [1, 0, "true", "false", "yes"])
async def test_http_decision_requires_explicit_boolean(route_env, approved):
    env, client = route_env
    confirmation = await prepare(client)
    response = await client.post(f"/commerce/confirmations/{confirmation['confirmation_id']}/resolve",
        json=decision(confirmation, approved=approved))
    assert response.status_code == 422
    assert (await env.store.get_inventory())["P1001-S1"] == 50


@pytest.mark.parametrize("changed,code", [
    ({"buyer_id": "buyer-2"}, "OWNER_MISMATCH"),
    ({"session_id": "session-2"}, "OWNER_MISMATCH"),
    ({"snapshot_hash": "0" * 64}, "SNAPSHOT_MISMATCH"),
])
async def test_http_tampered_identity_session_and_hash_are_rejected(route_env, changed, code):
    env, client = route_env
    confirmation = await prepare(client)
    response = await client.post(f"/commerce/confirmations/{confirmation['confirmation_id']}/resolve",
        json=decision(confirmation, **changed))
    assert response.status_code in {403, 409}
    assert response.json()["detail"]["code"] == code
    assert (await env.store.get_inventory())["P1001-S1"] == 50
    if "snapshot_hash" not in changed:
        identity = {"buyer_id": "buyer-1", "session_id": "session-1", **changed}
        found = await client.get(f"/commerce/confirmations/{confirmation['confirmation_id']}", params=identity)
        assert found.status_code in {403, 409}
        listing = await client.get("/commerce/confirmations", params=identity)
        assert listing.json()["confirmations"] == []


async def test_http_unknown_confirmation_returns_404(route_env):
    _, client = route_env
    response = await client.get("/commerce/confirmations/unknown", params={"buyer_id": "buyer-1", "session_id": "session-1"})
    assert response.status_code == 404


async def test_http_cannot_override_snapshot_content_in_resolve(route_env):
    _, client = route_env
    confirmation = await prepare(client)
    response = await client.post(f"/commerce/confirmations/{confirmation['confirmation_id']}/resolve",
        json=decision(confirmation, items=[{"quantity": 999}], shipping_address={"city": "北京"}))
    assert response.status_code == 422


async def test_http_parallel_resolution_returns_one_order_and_deduction(route_env):
    env, client = route_env
    confirmation = await prepare(client)
    url = f"/commerce/confirmations/{confirmation['confirmation_id']}/resolve"
    results = await asyncio.gather(*[client.post(url, json=decision(confirmation)) for _ in range(3)])
    assert all(result.status_code == 200 for result in results)
    assert all(result.json() == results[0].json() for result in results)
    assert results[0].json()["order"]["status"] == "CONFIRMED"
    assert (await env.store.get_inventory())["P1001-S1"] == 49
    async with env.engine.connect() as db:
        assert await db.scalar(select(func.count()).select_from(OrderRow)) == 1
    opposite = await client.post(url, json=decision(confirmation, approved=False))
    assert opposite.status_code == 409


async def test_real_server_cancel_route_prepares_then_resolves_using_same_ledger(confirmation_env, monkeypatch):
    from app.presentation import server

    env = confirmation_env
    lightweight = SimpleNamespace(
        confirmations=env.service, bus=env.bus, backplane=None,
        cancel_order=CancelOrderUseCase(env.service), query_order=QueryOrderUseCase(env.store),
        orchestrator=None, startup=AsyncMock(), shutdown=AsyncMock(),
    )
    monkeypatch.setattr(server, "build_container", AsyncMock(return_value=lightweight))
    api = server.build_app()
    async with api.router.lifespan_context(api):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            create = await prepare(client)
            placed = await client.post(f"/commerce/confirmations/{create['confirmation_id']}/resolve", json=decision(create))
            order_id = placed.json()["order"]["order_id"]
            response = await client.post(f"/commerce/orders/{order_id}/cancel",
                json={"buyer_id": "buyer-1", "session_id": "session-1", "reason": "用户改变计划"})
            assert response.status_code == 200, response.text
            cancellation = response.json()["confirmation"]
            assert cancellation["action"] == "cancel" and cancellation["status"] == "pending"
            assert (await env.store.get_inventory())["P1001-S1"] == 49
            query = await client.get(f"/commerce/orders/{order_id}", params={"buyer_id": "buyer-1"})
            assert query.status_code == 200 and query.json()["status"] == "CONFIRMED"
            missing_owner = await client.get(f"/commerce/orders/{order_id}")
            assert missing_owner.status_code == 422
            cancelled = await client.post(f"/commerce/confirmations/{cancellation['confirmation_id']}/resolve", json=decision(cancellation))
            assert cancelled.status_code == 200 and cancelled.json()["order"]["status"] == "CANCELLED"
            assert (await env.store.get_inventory())["P1001-S1"] == 50
    lightweight.startup.assert_awaited_once()
    lightweight.shutdown.assert_awaited_once()


@pytest.mark.parametrize("redis_ready,broken_trade,expected", [(True, False, 200), (False, False, 503), (True, True, 503)])
async def test_health_reports_runtime_and_refuses_readiness_without_required_storage(confirmation_env, monkeypatch, redis_ready, broken_trade, expected):
    from contextlib import asynccontextmanager
    from app.presentation import server

    class BrokenEngine:
        @asynccontextmanager
        async def connect(self):
            raise RuntimeError("storage unavailable")
            yield

    env = confirmation_env
    container = SimpleNamespace(
        confirmations=env.service, bus=env.bus, backplane=None,
        startup=AsyncMock(), shutdown=AsyncMock(), db_engine=env.engine,
        trade_db_engine=BrokenEngine() if broken_trade else env.engine,
        settings=SimpleNamespace(llm_model="test-model"), semantic_cache=SimpleNamespace(enabled=False),
        cache=SimpleNamespace(enabled=True, ping=AsyncMock(return_value=redis_ready)), task_queue=None,
        runtime={"app_source_sha256": "a" * 64},
    )
    monkeypatch.setattr(server, "build_container", AsyncMock(return_value=container))
    api = server.build_app()
    async with api.router.lifespan_context(api):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            response = await client.get("/health")
    assert response.status_code == expected
    assert response.json()["status"] == ("ok" if expected == 200 else "degraded")
    assert response.json()["runtime"]["app_source_sha256"] == "a" * 64
