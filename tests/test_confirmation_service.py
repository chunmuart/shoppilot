# -*- coding: utf-8 -*-
"""用户确认的应用边界：工具只准备，原子存储负责实际交易与幂等。"""
import inspect
import json
from datetime import timedelta

import pytest

from app.application.tools.order_tools import build_cancel_order_tool, build_create_order_tool, build_query_order_tool
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.confirmation_service import ConfirmationService
from app.application.usecases.order_usecases import CancelOrderUseCase, OrderItemInput, PlaceOrderUseCase, QueryOrderUseCase
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


async def prepare(env, quantity=1, *, buyer_id="buyer-1", session_id="session-1"):
    return await env.service.prepare_order(
        buyer_id, session_id, [OrderItemInput("P1001", "P1001-S1", quantity)], address(),
    )


async def approve(env, prepared, approved=True, **overrides):
    confirmation = prepared["confirmation"]
    fields = {"confirmation_id": confirmation["confirmation_id"], "buyer_id": "buyer-1",
        "session_id": "session-1", "snapshot_hash": confirmation["snapshot_hash"], "approved": approved}
    return await env.service.resolve(**{**fields, **overrides})


async def test_prepare_exposes_authoritative_snapshot_without_creating_order_or_deducting_stock(confirmation_env):
    env = confirmation_env
    queue = env.bus.subscribe("session-1")
    before = await env.store.get_inventory(["P1001-S1"])
    prepared = await prepare(env, quantity=2)
    confirmation = prepared["confirmation"]

    assert prepared["confirmation_required"] is True
    assert "order" not in prepared
    assert confirmation["status"] == "pending"
    assert confirmation["result"] is None
    assert confirmation["operation_id"].startswith("operation-")
    assert confirmation["expires_at"]
    assert len(confirmation["snapshot_hash"]) >= 32
    payload = confirmation["payload"]
    assert payload["items"][0]["sku_id"] == "P1001-S1"
    assert payload["items"][0]["quantity"] == 2
    assert payload["items"][0]["unit_price_minor"] == 18900
    assert payload["total_amount_minor"] == 37800
    assert payload["currency"] == "CNY"
    assert payload["shipping_address"]["recipient_name"] == "张三"
    assert payload["amount_scope"] == "merchandise_only"
    assert payload["order_kind"] == "purchase_intent"
    assert await env.store.get_inventory(["P1001-S1"]) == before
    event = queue.get_nowait()
    assert event.type == "confirmation.required"
    assert event.payload["confirmation"] == confirmation


async def test_only_trusted_resolve_creates_order_retries_reuse_it_and_repurchase_has_new_operation(confirmation_env):
    env = confirmation_env
    first = await prepare(env)
    result = await approve(env, first)
    repeat = await approve(env, first)
    assert result == repeat
    assert result["confirmation_required"] is False
    assert result["order"]["status"] == "CONFIRMED"
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 49

    second = await prepare(env)
    assert second["confirmation"]["operation_id"] != first["confirmation"]["operation_id"]
    again = await approve(env, second)
    assert again["order"]["order_id"] != result["order"]["order_id"]
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 48


async def test_rejection_never_deducts_stock_and_opposite_decision_is_rejected(confirmation_env):
    env = confirmation_env
    prepared = await prepare(env)
    rejected = await approve(env, prepared, approved=False)
    assert rejected["confirmation"]["status"] == "rejected"
    assert "order" not in rejected
    assert await approve(env, prepared, approved=False) == rejected
    with pytest.raises(ValueError):
        await approve(env, prepared)
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


@pytest.mark.parametrize("overrides", [
    {"buyer_id": "another-buyer"}, {"session_id": "another-session"}, {"snapshot_hash": "tampered-hash"},
])
async def test_owner_session_and_hash_cannot_be_substituted(confirmation_env, overrides):
    env = confirmation_env
    prepared = await prepare(env)
    with pytest.raises(ValueError):
        await approve(env, prepared, **overrides)
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50
    assert (await env.service.get(prepared["confirmation"]["confirmation_id"], "buyer-1", "session-1"))["confirmation_required"]


async def test_expired_confirmation_cannot_execute(confirmation_env):
    env = confirmation_env
    prepared = await prepare(env)
    env.now[0] += timedelta(seconds=301)
    with pytest.raises(ValueError) as error:
        await approve(env, prepared)
    assert error.value.code == "CONFIRMATION_EXPIRED"
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


async def test_refresh_restores_persistent_confirmation_but_never_another_buyers(confirmation_env):
    env = confirmation_env
    prepared = await prepare(env)
    restored = ConfirmationService(env.products, env.store)
    fetched = await restored.get(prepared["confirmation"]["confirmation_id"], "buyer-1", "session-1")
    assert fetched == prepared
    assert (await restored.list("buyer-1", "session-1"))["confirmations"] == [prepared["confirmation"]]
    assert (await restored.list("other", "session-1"))["confirmations"] == []
    with pytest.raises(ValueError):
        await restored.get(prepared["confirmation"]["confirmation_id"], "other", "session-1")


async def test_query_and_cancel_enforce_owner_and_cancellation_requires_separate_user_decision(confirmation_env):
    env = confirmation_env
    created = await approve(env, await prepare(env, quantity=2))
    order_id = created["order"]["order_id"]
    query = QueryOrderUseCase(env.store)
    cancel = CancelOrderUseCase(env.service)
    with pytest.raises(ValueError):
        await query.execute(order_id, buyer_id="other")
    with pytest.raises(ValueError):
        await cancel.execute(order_id, "不需要了", buyer_id="other", session_id="session-1")
    pending = await cancel.execute(order_id, "不需要了", buyer_id="buyer-1", session_id="session-1")
    assert pending["confirmation_required"] is True
    assert pending["confirmation"]["payload"]["items"][0]["quantity"] == 2
    assert (await query.execute(order_id, buyer_id="buyer-1"))["status"] == "CONFIRMED"
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 48
    cancelled = await approve(env, pending)
    assert cancelled["order"]["status"] == "CANCELLED"
    assert await approve(env, pending) == cancelled
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


@pytest.mark.parametrize("quantity", [0, -1, True, False, 1.5, "1", None])
def test_quantity_is_a_positive_integer_without_coercion(quantity):
    with pytest.raises(ValueError, match="正整数"):
        OrderItemInput("P1001", "P1001-S1", quantity)


@pytest.mark.parametrize("approved", ["true", "同意", 1, 0, None])
async def test_model_text_is_not_a_user_approval(confirmation_env, approved):
    env = confirmation_env
    prepared = await prepare(env)
    with pytest.raises(ValueError, match="布尔值"):
        await approve(env, prepared, approved=approved)
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


async def test_empty_owners_and_legacy_direct_write_entry_are_rejected(confirmation_env):
    env = confirmation_env
    with pytest.raises(ValueError):
        await prepare(env, buyer_id=" ")
    with pytest.raises(ValueError):
        await prepare(env, session_id="")
    with pytest.raises(ValueError):
        await QueryOrderUseCase(env.store).execute("GBX-1", buyer_id="")
    with pytest.raises(TypeError):
        await PlaceOrderUseCase(env.service).execute("buyer-1", [OrderItemInput("P1001", "P1001-S1", 1)], address())


async def test_bad_line_shape_and_incomplete_or_unsupported_address_never_prepare(confirmation_env):
    from dataclasses import replace

    env = confirmation_env
    with pytest.raises(ValueError, match="OrderItemInput"):
        await env.service.prepare_order("buyer-1", "session-1", [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}], address())
    for changes in [{"recipient_name": " "}, {"city": ""}, {"country": "unsupported"}]:
        with pytest.raises(ValueError):
            invalid = replace(address(), **changes)
            await env.service.prepare_order("buyer-1", "session-1", [OrderItemInput("P1001", "P1001-S1", 1)], invalid)
    assert (await env.service.list("buyer-1", "session-1"))["confirmations"] == []
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50


async def test_event_delivery_failure_keeps_committed_decision_recoverable_and_idempotent(confirmation_env, monkeypatch):
    env = confirmation_env

    def fail_publish(*args, **kwargs):
        raise RuntimeError("event stream disconnected")

    monkeypatch.setattr(env.bus, "publish", fail_publish)
    prepared = await prepare(env)
    committed = await approve(env, prepared)
    restored = await env.service.get(prepared["confirmation"]["confirmation_id"], "buyer-1", "session-1")
    assert restored == committed
    assert await approve(env, prepared) == committed
    assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 49


async def test_model_write_tools_prepare_only_and_cannot_choose_identity_approval_or_operation_id(confirmation_env):
    env = confirmation_env
    create = build_create_order_tool(PlaceOrderUseCase(env.service), env.bus, env.evidence)
    search = build_product_search_tool(CatalogSearchUseCase(env.products), env.bus, env.evidence)
    cancel = build_cancel_order_tool(CancelOrderUseCase(env.service), env.bus)
    query = build_query_order_tool(QueryOrderUseCase(env.store), env.bus)
    assert set(inspect.signature(create).parameters) == {"items", "shipping_address", "product_id", "sku_id", "quantity"}
    assert set(inspect.signature(cancel).parameters) == {"order_id", "reason"}
    without_context = await create(items=[], shipping_address={})
    assert without_context.content[0].text.startswith("[error]")
    token = ShoppingContext.set(ShoppingContextSnapshot("session-1", "buyer-1", "zh-CN", "CNY"))
    try:
        from dataclasses import asdict
        item = {"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}
        unverified = await create(items=[item], shipping_address=asdict(address()))
        assert "未检索返回" in unverified.content[0].text
        assert not await env.store.list_confirmations(buyer_id="buyer-1", session_id="session-1")
        await search(product_id="P1001", sku_id="P1001-S1")
        prepared = json.loads((await create(items=[item], shipping_address=asdict(address()))).content[0].text)
        assert prepared["confirmation_required"] is True
        assert "order" not in prepared
        assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50
        bad = await create(items=[{**item, "quantity": 1.5}], shipping_address=asdict(address()))
        assert bad.content[0].text.startswith("[error]")
        committed = await approve(env, prepared)
        order_id = committed["order"]["order_id"]
        assert json.loads((await query(order_id)).content[0].text)["buyer_id"] == "buyer-1"
        cancellation = json.loads((await cancel(order_id, "用户想取消")).content[0].text)
        assert cancellation["confirmation_required"] is True
        assert (await env.store.get_order(order_id, buyer_id="buyer-1"))["status"] == "CONFIRMED"
    finally:
        ShoppingContext.reset(token)


async def test_agent_state_restores_current_order_status_without_shipping_or_confirmation_secrets(confirmation_env):
    env = confirmation_env
    prepared = await prepare(env)
    pending = await env.service.agent_state("buyer-1", "session-1")
    assert pending["orders"] == []
    assert pending["pending_confirmations"][0]["items"][0]["quantity"] == 1
    created = await approve(env, prepared)
    cancel = await env.service.prepare_cancel("buyer-1", "session-1", created["order"]["order_id"], "测试取消")
    await approve(env, cancel)
    state = await env.service.agent_state("buyer-1", "session-1")
    assert len(state["orders"]) == 1
    assert state["orders"][0]["status"] == "CANCELLED"
    assert state["pending_confirmations"] == []
    assert await env.service.agent_state("another-buyer", "session-1") == {"orders": [], "pending_confirmations": []}
    serialized = json.dumps(state, ensure_ascii=False)
    for sensitive in ("shipping_address", address().address_line, address().phone, "snapshot_hash", "confirmation_id"):
        assert sensitive not in serialized
