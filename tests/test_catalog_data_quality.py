# -*- coding: utf-8 -*-
"""商品评测数据的字段和分布门禁。"""
from __future__ import annotations

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.infrastructure.persistence.seed_products import build_seed_products
from scripts.eval.data_quality import validate_catalog_distribution, validate_catalog_products


def test_catalog_fixture_passes_field_and_distribution_validation():
    products = build_seed_products()

    assert validate_catalog_products(products) == []
    assert validate_catalog_distribution(products) == []


def test_catalog_validator_reports_duplicate_sku_and_missing_evaluation_metadata():
    products = [
        Product(
            product_id="P-A", title="A", brand="A", category="旅行装备", origin_country="CN", description="A",
            ships_to=["CN"], skus=[Sku("SAME", "标准", Money.from_major_units(1, "CNY"), 1)],
        ),
        Product(
            product_id="P-B", title="B", brand="B", category="旅行装备", origin_country="CN", description="B",
            ships_to=["CN"], skus=[Sku("SAME", "标准", Money.from_major_units(1, "CNY"), 1)],
        ),
    ]

    problems = validate_catalog_products(products)

    assert any("sku_id 重复" in problem for problem in problems)
    assert any("source_platform" in problem for problem in problems)


async def test_dataset_validation_entrypoint_includes_catalog_quality_gate():
    from scripts.eval.validate_datasets import validate_catalog_fixture

    assert await validate_catalog_fixture() == []
