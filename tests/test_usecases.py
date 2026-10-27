# -*- coding: utf-8 -*-
"""usecase 层单测：商品召回 + 订单闭环（不依赖 LLM）。"""
import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    OrderItemInput,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.order.address import Address
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryProductRepository,
)
from tests.trade_test_helpers import confirmation_env  # noqa: F401


@pytest.fixture()
def product_repo() -> InMemoryProductRepository:
    return InMemoryProductRepository()


def _address() -> Address:
    return Address(
        recipient_name="张三",
        country="CN",
        state="浙江",
        city="杭州",
        address_line="西湖区某路 1 号",
        postal_code="310000",
        phone="13800000000",
    )


class TestCatalogSearch:
    async def test_recall_travel_set(self, product_repo):
        usecase = CatalogSearchUseCase(product_repo)
        result = await usecase.execute(
            ProductSearchSpec(
                normalized_query="旅行三件套 抗造 轻便 无塑料",
                excluded_material_tags=["合成聚合物"],
            ),
        )
        assert result["hits"], "旅行三件套应能召回"
        assert result["hits"][0]["product_id"] == "P2120", "无塑料约束应优先命中天然材质三件套"

    async def test_ship_to_filter(self, product_repo):
        usecase = CatalogSearchUseCase(product_repo)
        result = await usecase.execute(ProductSearchSpec(normalized_query="旅行茶具", ship_to="US"))
        # P1006 只发 CN/JP，指定 ship_to=US 后不应出现
        assert all(hit["product_id"] != "P1006" for hit in result["hits"])

    async def test_top_k_limit(self, product_repo):
        usecase = CatalogSearchUseCase(product_repo)
        result = await usecase.execute(ProductSearchSpec(normalized_query="旅行", top_k=2))
        assert len(result["hits"]) <= 2

    async def test_no_hit_returns_empty(self, product_repo):
        usecase = CatalogSearchUseCase(product_repo)
        result = await usecase.execute(ProductSearchSpec(normalized_query="quantum flux capacitor"))
        assert result["hits"] == []


class TestOrderLifecycle:
    async def test_place_query_cancel_roundtrip(self, confirmation_env):
        env = confirmation_env
        place = PlaceOrderUseCase(env.service)
        query = QueryOrderUseCase(env.store)
        cancel = CancelOrderUseCase(env.service)

        prepared = await place.execute(
            buyer_id="buyer-1",
            session_id="session-1",
            items=[OrderItemInput(product_id="P1001", sku_id="P1001-S1", quantity=2)],
            shipping_address=_address(),
        )
        assert prepared["confirmation_required"] is True
        assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50
        confirmation = prepared["confirmation"]
        committed = await env.service.resolve(
            confirmation["confirmation_id"], "buyer-1", "session-1", confirmation["snapshot_hash"], True,
        )
        snapshot = committed["order"]
        assert snapshot["status"] == "CONFIRMED"
        assert snapshot["total_amount_major"] == 378.0
        assert snapshot["currency"] == "CNY"

        # 下单扣库存
        assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 48

        queried = await query.execute(snapshot["order_id"], buyer_id="buyer-1")
        assert queried["order_id"] == snapshot["order_id"]

        cancellation = await cancel.execute(
            snapshot["order_id"], "买家改主意了", buyer_id="buyer-1", session_id="session-1",
        )
        assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 48
        confirmation = cancellation["confirmation"]
        cancelled = (await env.service.resolve(
            confirmation["confirmation_id"], "buyer-1", "session-1", confirmation["snapshot_hash"], True,
        ))["order"]
        assert cancelled["status"] == "CANCELLED"
        # 取消回补库存
        assert (await env.store.get_inventory(["P1001-S1"]))["P1001-S1"] == 50

    async def test_insufficient_stock_does_not_write_before_confirmation(self, confirmation_env):
        env = confirmation_env
        place = PlaceOrderUseCase(env.service)
        original_stock = await env.store.get_inventory(["P1006-S1"])

        with pytest.raises(ValueError, match="库存不足"):
            await place.execute(
                buyer_id="buyer-1",
                session_id="session-1",
                items=[
                    OrderItemInput(product_id="P1006", sku_id="P1006-S1", quantity=5),
                    OrderItemInput(product_id="P1006", sku_id="P1006-S1", quantity=99),
                ],
                shipping_address=_address(),
            )
        # 重复 SKU 先合并后校验，准备失败不能修改任何库存。
        assert await env.store.get_inventory(["P1006-S1"]) == original_stock
        assert (await env.service.list("buyer-1", "session-1"))["confirmations"] == []

    async def test_query_unknown_order(self, confirmation_env):
        query = QueryOrderUseCase(confirmation_env.store)
        with pytest.raises(ValueError) as error:
            await query.execute("GBX-999999", buyer_id="buyer-1")
        assert error.value.code == "NOT_FOUND"
