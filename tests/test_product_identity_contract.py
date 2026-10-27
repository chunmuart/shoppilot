"""精确查询与下单商品来源：使用真实目录、会话证据和 SQLite 交易底座。"""
from dataclasses import asdict
import json

import pytest
from agentscope.tool import FunctionTool

from app.application.tools.product_search_tool import build_product_search_tool
from app.application.tools.order_tools import build_create_order_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.order_usecases import PlaceOrderUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from tests.trade_test_helpers import confirmation_env, test_address as address  # noqa: F401


@pytest.fixture
async def tools_env(confirmation_env):
    env = confirmation_env
    env.search = build_product_search_tool(CatalogSearchUseCase(env.products), env.bus, env.evidence)
    env.create = build_create_order_tool(PlaceOrderUseCase(env.service), env.bus, env.evidence)
    token = ShoppingContext.set(ShoppingContextSnapshot("identity-session", "identity-buyer", "zh-CN", "CNY"))
    try:
        yield env
    finally:
        ShoppingContext.reset(token)


def payload(chunk):
    return json.loads(chunk.content[0].text)


@pytest.mark.parametrize("params,expected_sku", [({"product_id": "P1001"}, None), ({"sku_id": "p1003-s1"}, "P1003-S1"), ({"product_id": "P1003", "sku_id": "P1003-S1"}, "P1003-S1")])
async def test_exact_identity_without_query(tools_env, params, expected_sku):
    data = payload(await tools_env.search(**params, ship_to="CN"))
    assert data["recall_strategy"] == "exact_id_lookup"
    assert len(data["hits"]) == 1
    if expected_sku:
        assert [s["sku_id"] for s in data["hits"][0]["skus"]] == [expected_sku]
    assert data["hits"][0]["landed_price"]


async def test_explicit_identity_wins_and_missing_never_falls_back(tools_env):
    data = payload(await tools_env.search(normalized_query="耳机 P1002", product_id="P1001"))
    assert [h["product_id"] for h in data["hits"]] == ["P1001"]
    missing = payload(await tools_env.search(normalized_query="旅行背包", product_id="P99999999"))
    assert missing["hits"] == []
    assert missing["missing_identifiers"] == ["P99999999"]
    assert missing["existence_checked"] is True


@pytest.mark.parametrize("params", [{"product_id": "P1001", "sku_id": "P1003-S1"}, {"product_id": ""}, {"sku_id": "unknown"}, {}])
async def test_invalid_or_conflicting_search_ids_fail(tools_env, params):
    assert (await tools_env.search(**params)).content[0].text.startswith("[error]")


async def test_exact_identity_keeps_hard_filters_and_old_query_compatibility(tools_env):
    blocked = payload(await tools_env.search(product_id="P1001", price_max_major=0.01))
    assert not blocked["hits"] and blocked["filtered_out"]
    old = payload(await tools_env.search(normalized_query="核对 P1003-S1 库存"))
    assert old["hits"][0]["default_sku_id"] == "P1003-S1"


async def test_order_rejects_unsearched_product_and_does_not_create_confirmation(tools_env):
    env = tools_env
    await env.search(product_id="P1001")
    before = await env.store.get_inventory()
    failed = await env.create(product_id="P1002", shipping_address=asdict(address()))
    assert "当前会话未检索返回" in failed.content[0].text
    assert not await env.store.list_confirmations(buyer_id="identity-buyer", session_id="identity-session")
    assert await env.store.get_inventory() == before


async def test_single_product_id_uses_verified_default_then_waits_for_buyer(tools_env):
    env = tools_env
    hit = payload(await env.search(product_id="P1001"))["hits"][0]
    before = await env.store.get_inventory()
    result = payload(await env.create(product_id="P1001", quantity=2, shipping_address=asdict(address())))
    assert result["confirmation_required"]
    c = result["confirmation"]
    assert c["payload"]["items"][0]["sku_id"] == hit["default_sku_id"]
    assert c["payload"]["items"][0]["quantity"] == 2
    assert (await env.store.list_orders(buyer_id="identity-buyer"))["total"] == 0
    assert await env.store.get_inventory() == before
    committed = await env.service.resolve(c["confirmation_id"], "identity-buyer", "identity-session", c["snapshot_hash"], True)
    assert committed["order"]["status"] == "CONFIRMED"


async def test_specification_must_have_been_returned(tools_env):
    env = tools_env
    await env.search(sku_id="P1003-S1")
    result = await env.create(product_id="P1003", sku_id="P1003-S2", shipping_address=asdict(address()))
    assert "未检索返回" in result.content[0].text
    wrong_owner = await env.create(product_id="P1001", sku_id="P1003-S1", shipping_address=asdict(address()))
    assert "未检索返回" in wrong_owner.content[0].text


@pytest.mark.parametrize("buyer,session", [("another-buyer", "identity-session"), ("identity-buyer", "another-session")])
async def test_order_evidence_is_scoped_to_buyer_and_session(tools_env, buyer, session):
    env = tools_env
    await env.search(product_id="P1001")
    token = ShoppingContext.set(ShoppingContextSnapshot(session, buyer, "zh-CN", "CNY"))
    try:
        assert "未检索返回" in (await env.create(product_id="P1001", shipping_address=asdict(address()))).content[0].text
    finally:
        ShoppingContext.reset(token)


async def test_restarted_or_delegated_tool_uses_persistent_evidence_and_current_inventory(tools_env):
    env = tools_env
    await env.search(product_id="P1001")
    restored = build_create_order_tool(PlaceOrderUseCase(env.service), env.bus, ContextEvidenceStore(env.evidence.path))
    result = payload(await restored(product_id="P1001", shipping_address=asdict(address())))
    assert result["confirmation_required"]
    failed = await restored(product_id="P1001", quantity=999999, shipping_address=asdict(address()))
    assert "库存不足" in failed.content[0].text


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "2"])
async def test_single_product_quantity_is_not_silently_coerced(tools_env, quantity):
    await tools_env.search(product_id="P1001")
    result = await tools_env.create(product_id="P1001", quantity=quantity, shipping_address=asdict(address()))
    assert result.content[0].text.startswith("[error]")


async def test_batch_cannot_mix_single_fields_or_partially_prepare(tools_env):
    env = tools_env
    await env.search(product_id="P1001")
    items = [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}]
    mixed = await env.create(items=items, product_id="P1001", shipping_address=asdict(address()))
    assert "不能混用" in mixed.content[0].text
    invalid_batch = await env.create(items=items + [{"product_id": "P1002", "sku_id": "P1002-S1", "quantity": 1}], shipping_address=asdict(address()))
    assert invalid_batch.content[0].text.startswith("[error]")
    assert not await env.store.list_confirmations(buyer_id="identity-buyer", session_id="identity-session")


async def test_missing_store_fails_closed_and_function_schemas_accept_id_entry(tools_env):
    env = tools_env
    unavailable = build_create_order_tool(PlaceOrderUseCase(env.service), env.bus)
    assert "证据未接入" in (await unavailable(product_id="P1001", shipping_address=asdict(address()))).content[0].text
    search_schema = FunctionTool(env.search).input_schema
    order_schema = FunctionTool(env.create).input_schema
    assert {"product_id", "sku_id"} <= set(search_schema["properties"])
    assert "normalized_query" not in search_schema.get("required", [])
    assert {"product_id", "sku_id", "quantity"} <= set(order_schema["properties"])
    assert "items" not in order_schema.get("required", [])
