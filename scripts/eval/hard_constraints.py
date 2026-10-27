# -*- coding: utf-8 -*-
"""检索评测的硬约束泄漏检查。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _field(product: Any, name: str, default: Any) -> Any:
    if isinstance(product, dict):
        return product.get(name, default)
    return getattr(product, name, default)


def find_hit_constraint_violations(
    hits: Iterable[dict[str, Any]],
    products: dict[str, Any],
    *,
    price_max_major: float | None = None,
    ship_to: str | None = None,
    category: str | None = None,
    target_currency: str | None = None,
    require_in_stock: bool = True,
    excluded_material_tags: Iterable[str] = (),
    required_material_tags: Iterable[str] = (),
) -> dict[str, set[str]]:
    """返回每个违规命中及原因。

    商品卡价格已经是检索链路声明的币种；若币种不符，不能把它当作可比较
    价格而放过，直接记录 ``currency_mismatch``。
    """
    violations: dict[str, set[str]] = defaultdict(set)
    excluded_materials = set(excluded_material_tags)
    required_materials = set(required_material_tags)
    for hit in hits:
        product_id = str(hit.get("product_id", ""))
        product = products.get(product_id)
        if product is None:
            violations[product_id].add("unknown_product")
            continue
        if ship_to and ship_to not in _field(product, "ships_to", []):
            violations[product_id].add("ship_to_unavailable")
        if category and _field(product, "category", "") != category:
            violations[product_id].add("category_mismatch")
        if excluded_materials & set(_field(product, "material_tags", [])):
            violations[product_id].add("material_excluded")
        if required_materials - set(_field(product, "material_tags", [])):
            violations[product_id].add("material_required_missing")
        if price_max_major is not None:
            if target_currency and hit.get("currency") != target_currency:
                violations[product_id].add("currency_mismatch")
            elif float(hit.get("price_major", float("inf"))) > price_max_major:
                violations[product_id].add("over_price_cap")
        if require_in_stock:
            skus = hit.get("skus", [])
            if not skus or not any(int(sku.get("stock", 0)) > 0 for sku in skus):
                violations[product_id].add("out_of_stock")
    return dict(violations)
