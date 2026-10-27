# -*- coding: utf-8 -*-
"""准生产演示商品集的规模与分布契约。"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from app.infrastructure.persistence.seed_products import build_seed_products
from scripts.eval.data_quality import validate_catalog_raw_records


def test_catalog_fixture_has_required_scale_and_coverage():
    products = build_seed_products()
    skus = [sku for product in products for sku in product.skus]

    assert len(products) == 3700
    assert Counter(p.source_platform for p in products) == {"amazon": 1050, "ebay": 950, "etsy": 1150, "walmart": 550}
    assert len(skus) == 7105
    assert len({product.category for product in products}) >= 8
    assert sum(len(product.skus) >= 2 for product in products) / len(products) >= 0.35
    assert {sku.price.currency for sku in skus} >= {"CNY", "USD", "EUR", "JPY", "SGD"}


def test_catalog_fixture_contains_inventory_and_delivery_negative_cases():
    products = build_seed_products()
    fully_out = [product for product in products if all(sku.stock == 0 for sku in product.skus)]
    partially_out = [product for product in products if any(sku.stock == 0 for sku in product.skus) and any(sku.stock > 0 for sku in product.skus)]
    delivery_counts = Counter(tuple(product.ships_to) for product in products)

    assert len(fully_out) / len(products) >= 0.10
    assert len(partially_out) / len(products) >= 0.10
    assert any("US" not in destinations for destinations in delivery_counts)
    assert any("US" in destinations for destinations in delivery_counts)


def test_catalog_fixture_has_evaluation_metadata_for_cross_platform_and_constraints():
    product = build_seed_products()[0]

    assert product.source_platform
    assert product.external_product_id
    assert product.canonical_product_id
    assert product.material_tags
    assert product.weight_kg > 0
    assert product.dimensions_cm
    assert product.tax_category
    assert product.rating_summary is not None
    assert product.updated_at


def test_versioned_catalog_has_hard_negative_and_price_band_coverage():
    records = [
        json.loads(line)
        for line in (Path(__file__).resolve().parents[1] / "data" / "catalog-v2.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert validate_catalog_raw_records(records) == []
    assert sum("hard_negative" in record.get("evaluation_tags", []) for record in records) / len(records) >= 0.15


def test_expanded_catalog_keeps_existing_products_and_is_reproducible():
    from scripts.generate_catalog_fixture import build_records
    records = build_records()
    actual = [json.loads(l) for l in (Path(__file__).resolve().parents[1]/"data/catalog-v2.jsonl").read_text().splitlines()]
    assert records == actual
    assert len({r["product_id"] for r in records}) == 1000
    old = next(r for r in records if r["product_id"] == "P1003")
    assert old["skus"][0]["sku_id"] == "P1003-S1"
    assert old["skus"][0]["price_major"] == 129
    expanded = records[500:]
    assert all(r["data_provenance"] == "synthetic" for r in expanded)
    assert len({r["canonical_product_id"] for r in expanded}) == 125
    # 同款必须具有相同商品事实；报价/库存/配送可以不同。
    groups = {}
    for r in expanded:
        signature = (r["brand"], r["category"], tuple(r["material_tags"]), r["model_spec"])
        assert groups.setdefault(r["canonical_product_id"], signature) == signature
