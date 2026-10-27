# -*- coding: utf-8 -*-
"""评测商品数据集的字段与分布门禁。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable

from app.domain.catalog.product import Product


_REQUIRED_PRODUCT_FIELDS = (
    "source_platform",
    "external_product_id",
    "canonical_product_id",
    "tax_category",
    "updated_at",
)
_REQUIRED_CURRENCIES = {"CNY", "USD", "EUR", "JPY", "SGD"}


def validate_catalog_raw_records(records: list[dict[str, Any]]) -> list[str]:
    """校验仅用于评测构造的原始分布标签，避免运行时模型字段被测试元数据污染。"""
    if not records:
        return ["catalog JSONL 为空"]
    problems: list[str] = []
    hard_negatives = sum("hard_negative" in record.get("evaluation_tags", []) for record in records)
    if hard_negatives / len(records) < 0.15:
        problems.append("标题相近但属性不符的 hard_negative 占比不足 15%")
    cny_prices = [
        float(sku["price_major"])
        for record in records
        for sku in record.get("skus", [])
        if sku.get("currency") == "CNY"
    ]
    bands = {
        "low": any(price < 300 for price in cny_prices),
        "middle": any(300 <= price < 1000 for price in cny_prices),
        "high": any(price >= 1000 for price in cny_prices),
    }
    if not all(bands.values()):
        problems.append("CNY 价格未同时覆盖低/中/高客单区间")
    return problems


def validate_catalog_products(products: Iterable[Product]) -> list[str]:
    """校验单条记录的可用性，不通过就不能作为评测金标。"""
    problems: list[str] = []
    seen_products: set[str] = set()
    seen_skus: set[str] = set()
    for product in products:
        if product.product_id in seen_products:
            problems.append(f"product_id 重复：{product.product_id}")
        seen_products.add(product.product_id)
        for field in _REQUIRED_PRODUCT_FIELDS:
            if not getattr(product, field):
                problems.append(f"{product.product_id} 缺少 {field}")
        if not product.material_tags:
            problems.append(f"{product.product_id} 缺少 material_tags")
        if product.weight_kg <= 0:
            problems.append(f"{product.product_id} weight_kg 必须大于 0")
        if not product.dimensions_cm or not all(value > 0 for value in product.dimensions_cm.values()):
            problems.append(f"{product.product_id} dimensions_cm 非法")
        if not product.rating_summary:
            problems.append(f"{product.product_id} 缺少 rating_summary")
        try:
            date.fromisoformat(product.updated_at)
        except ValueError:
            problems.append(f"{product.product_id} updated_at 非 ISO 日期")
        for sku in product.skus:
            if sku.sku_id in seen_skus:
                problems.append(f"sku_id 重复：{sku.sku_id}")
            seen_skus.add(sku.sku_id)
    return problems


def validate_catalog_distribution(products: Iterable[Product]) -> list[str]:
    """校验准生产演示集的最低规模和长尾覆盖。"""
    products = list(products)
    skus = [sku for product in products for sku in product.skus]
    problems: list[str] = []
    if len(products) < 1000:
        problems.append(f"SPU 数不足：{len(products)} < 1000")
    if len(skus) < 1500:
        problems.append(f"SKU 数应至少 1500，实际 {len(skus)}")
    platform_counts = Counter(p.source_platform for p in products)
    for platform in ("amazon", "ebay", "etsy", "walmart"):
        if platform_counts[platform] < 250:
            problems.append(f"平台 {platform} 商品不足 250")
    category_counts = Counter(product.category for product in products)
    if len(category_counts) < 8:
        problems.append(f"一级品类不足：{len(category_counts)} < 8")
    for category, count in category_counts.items():
        if count < 40:
            problems.append(f"品类 {category} SPU 不足：{count} < 40")
    if products and sum(len(product.skus) >= 2 for product in products) / len(products) < 0.35:
        problems.append("多 SKU 商品占比不足 35%")
    if _REQUIRED_CURRENCIES - {sku.price.currency for sku in skus}:
        problems.append("缺少必需币种：" + ",".join(sorted(_REQUIRED_CURRENCIES - {sku.price.currency for sku in skus})))
    fully_out = [product for product in products if all(sku.stock == 0 for sku in product.skus)]
    partially_out = [
        product for product in products
        if any(sku.stock == 0 for sku in product.skus) and any(sku.stock > 0 for sku in product.skus)
    ]
    if products and len(fully_out) / len(products) < 0.10:
        problems.append("全缺货商品占比不足 10%")
    if products and len(partially_out) / len(products) < 0.10:
        problems.append("部分缺货商品占比不足 10%")
    platforms_by_canonical: dict[str, set[str]] = defaultdict(set)
    for product in products:
        platforms_by_canonical[product.canonical_product_id].add(product.source_platform)
    cross_platform_products = sum(
        1 for product in products if len(platforms_by_canonical[product.canonical_product_id]) >= 2
    )
    if products and cross_platform_products / len(products) < 0.20:
        problems.append("跨平台同款/近似款分组占比不足 20%")
    return problems
