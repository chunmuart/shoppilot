# -*- coding: utf-8 -*-
"""检验 UI 展示字段来源、媒体诚实性以及默认 SKU 与报价的一致性。"""
import json
from pathlib import Path

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.sku import Sku
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("product_id", "filename"),
    [("P1003", "wanderlite.png"), ("P1018", "drypack.png"), ("P1049", "budgetpack.png")],
)
async def test_catalog_presentation_preserves_snapshot_fields_and_labels_illustrations(product_id, filename):
    records = {
        record["product_id"]: record
        for record in (
            json.loads(line)
            for line in (PROJECT_ROOT / "data/catalog-v1.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    source = records[product_id]
    source_repo = InMemoryProductRepository()
    products = await source_repo.find_by_ids([product_id])
    usecase = CatalogSearchUseCase(InMemoryProductRepository(products))

    result = await usecase.execute(ProductSearchSpec(normalized_query=source["title"], ship_to="CN"))
    card = result["hits"][0]

    for field in ("description", "rating_summary", "ships_to", "dimensions_cm", "updated_at", "skus"):
        assert card[field] == source[field], f"展示字段 {field} 必须来自目录，不能由 UI 补造"
    assert card["rating_is_live"] is False
    assert card["image_url"] == f"/products/{filename}"
    assert card["image_kind"] == "illustration"
    assert "非商品实拍" in card["image_alt"]
    assert card["default_sku_id"] in {sku["sku_id"] for sku in source["skus"] if sku["stock"] > 0}
    image_path = PROJECT_ROOT / "frontend/public" / card["image_url"].lstrip("/")
    assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "商品媒体映射必须对应可发布的 PNG 资源"


async def test_unmapped_product_does_not_invent_photo_rating_or_shipping_promise():
    product = Product(
        product_id="P-NO-MEDIA", title="测试背包", brand="A", category="旅行装备", origin_country="CN",
        description="轻量背包", skus=[Sku("P-NO-MEDIA-S1", "标准", Money.from_major_units(20, "CNY"), 2)],
    )
    result = await CatalogSearchUseCase(InMemoryProductRepository([product])).execute(
        ProductSearchSpec(normalized_query="背包"),
    )
    card = result["hits"][0]

    assert card["image_url"] is None
    assert card["image_kind"] == "placeholder"
    assert card["image_alt"] == "测试背包：暂无商品图片"
    assert card["rating_summary"] is None
    assert card["rating_is_live"] is False
    assert card["ships_to"] == []
    assert card["dimensions_cm"] == {}
    assert card["updated_at"] == ""
    assert "landed_price" not in card
    assert not {"delivery_eta", "sales_count", "discount", "original_price"} & card.keys()


async def test_default_sku_and_landed_quote_use_available_variant_without_rewriting_source_currency():
    product = Product(
        product_id="P-VARIANT", title="美元背包", brand="A", category="旅行装备", origin_country="US",
        description="背包", ships_to=["CN"],
        skus=[
            Sku("P-VARIANT-S1", "缺货小号", Money.from_major_units(5, "USD"), 0),
            Sku("P-VARIANT-S2", "可售大号", Money.from_major_units(10, "USD"), 7),
        ],
    )
    result = await CatalogSearchUseCase(InMemoryProductRepository([product])).execute(
        ProductSearchSpec(normalized_query="背包", target_currency="CNY", ship_to="CN"),
    )
    card = result["hits"][0]

    assert card["default_sku_id"] == "P-VARIANT-S2"
    assert card["price_major"] == pytest.approx(71)
    assert card["currency"] == "CNY"
    assert card["source_price_major"] == 10
    assert card["source_currency"] == "USD"
    assert card["skus"] == [
        {"sku_id": "P-VARIANT-S1", "spec": "缺货小号", "price_major": 5, "currency": "USD", "stock": 0},
        {"sku_id": "P-VARIANT-S2", "spec": "可售大号", "price_major": 10, "currency": "USD", "stock": 7},
    ]
    assert card["landed_price"]["subtotal_major"] == pytest.approx(card["price_major"])
    assert card["landed_price"]["currency"] == card["currency"]
