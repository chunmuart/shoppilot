# -*- coding: utf-8 -*-
"""正式评测集的规模、桶分布与 split 隔离检查。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from app.domain.catalog.exchange_rate import ExchangeRateTable
from scripts.eval.http_actions import validate_http_actions


_SPECS = {
    "product": ("product_retrieval.jsonl", 150, {"literal": 45, "semantic": 40, "composite": 40, "empty": 25}),
    "knowledge": ("knowledge_retrieval.jsonl", 50, {"single": 20, "cross": 15, "unanswerable": 10, "conflict_or_expired": 5}),
    "agent": ("agent_cases.yaml", 100, {"search_recommend": 25, "compare_price": 15, "order": 15, "memory_multiturn": 15, "tool_failure": 10, "safety": 10, "long_context": 10}),
}

_RATES_TO_CNY = ExchangeRateTable().rates_to_cny


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return _load_jsonl(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw.get("cases", [])


def validate_bucket_split_coverage(rows: list[dict[str, Any]], bucket_key: str, name: str) -> list[str]:
    """每个正式评测桶都必须同时出现在 dev 和 release。"""
    coverage: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        coverage[str(row.get(bucket_key))].add(str(row.get("split")))
    return [
        f"{name} 分桶 {bucket} 未同时覆盖 dev/release：{sorted(splits)}"
        for bucket, splits in sorted(coverage.items())
        if splits != {"dev", "release"}
    ]


def validate_product_case_constraints(row: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> list[str]:
    """独立复算正式商品金标是否真的满足声明的硬约束。"""
    constraints = row.get("constraints") or {}
    target_currency = row.get("target_currency")
    if not isinstance(constraints, dict) or target_currency not in _RATES_TO_CNY:
        return []
    problems: list[str] = []
    required = set(constraints.get("required_material_tags") or [])
    excluded = set(constraints.get("excluded_material_tags") or [])
    for product_id in row.get("relevant") or []:
        product = catalog.get(product_id)
        if product is None:
            continue
        prefix = f"{row.get('id', '<unknown>')} 金标 {product_id}"
        if constraints.get("category") and product.get("category") != constraints["category"]:
            problems.append(f"{prefix} 不满足 category")
        if constraints.get("ship_to") and constraints["ship_to"] not in product.get("ships_to", []):
            problems.append(f"{prefix} 不满足 ship_to")
        materials = set(product.get("material_tags") or [])
        if required - materials:
            problems.append(f"{prefix} 不满足 required_material_tags")
        if excluded & materials:
            problems.append(f"{prefix} 命中 excluded_material_tags")
        skus = product.get("skus") or []
        available = [sku for sku in skus if int(sku.get("stock", 0)) > 0]
        if constraints.get("require_in_stock") and not available:
            problems.append(f"{prefix} 不满足 stock")
        primary = available[0] if available else (skus[0] if skus else None)
        price_cap = constraints.get("price_max_major")
        if price_cap is not None and primary is not None:
            source_currency = primary.get("currency")
            if source_currency not in _RATES_TO_CNY:
                problems.append(f"{prefix} price 使用未知币种 {source_currency}")
            else:
                target_price = float(primary["price_major"]) * _RATES_TO_CNY[source_currency] / _RATES_TO_CNY[target_currency]
                if target_price > float(price_cap) + 0.01:
                    problems.append(f"{prefix} 不满足 price_max_major")
    return problems


def validate_official_eval_fixture(root: Path) -> tuple[list[str], dict[str, dict[str, int]]]:
    """返回问题和统计；缺失/错分桶均为错误，不给“差不多”的假通过。"""
    problems: list[str] = []
    summary: dict[str, dict[str, int]] = {}
    loaded: dict[str, list[dict[str, Any]]] = {}
    for name, (filename, expected_total, expected_buckets) in _SPECS.items():
        path = root / filename
        if not path.is_file():
            problems.append(f"{name} 正式评测集不存在：{filename}")
            continue
        rows = _load_rows(path)
        loaded[name] = rows
        splits = Counter(str(row.get("split")) for row in rows)
        summary[name] = {"total": len(rows), "dev": splits["dev"], "release": splits["release"]}
        if len(rows) != expected_total:
            problems.append(f"{name} 总数 {len(rows)}，应为 {expected_total}")
        if (splits["dev"], splits["release"]) != (expected_total * 7 // 10, expected_total * 3 // 10):
            problems.append(f"{name} split 不是 70/30：{dict(splits)}")
        buckets = Counter(str(row.get("scenario") or row.get("kind")) for row in rows)
        if dict(buckets) != expected_buckets:
            problems.append(f"{name} 分桶异常：{dict(buckets)}")
        problems.extend(validate_bucket_split_coverage(rows, "scenario" if name == "agent" else "kind", name))
        if name == "product":
            for dimension in ("category", "target_currency", "ship_to"):
                problems.extend(validate_bucket_split_coverage(rows, dimension, f"product.{dimension}"))
        groups: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            group_id = row.get("group_id")
            split = row.get("split")
            if not isinstance(group_id, str) or not group_id:
                problems.append(f"{name} 存在缺失 group_id 的用例")
                break
            if split not in {"dev", "release"}:
                problems.append(f"{name} 存在非法 split：{split}")
                break
            groups[group_id].add(split)
        leaked = sorted(group_id for group_id, values in groups.items() if len(values) > 1)
        if leaked:
            problems.append(f"{name} 同组模板跨 split 泄漏：{leaked[:5]}")
        if any(not isinstance(row.get("template_family"), str) or not row["template_family"] for row in rows):
            problems.append(f"{name} 存在缺失 template_family 的用例")
        if name == "knowledge" and len({str(row.get("query")) for row in rows}) != len(rows):
            problems.append("knowledge 存在重复 query")
        if name == "agent":
            for row in rows:
                try:
                    actions = validate_http_actions(row)
                    if row.get("scenario") == "order" and not any(action["action"] == "create" and action["approved"] is True for action in actions):
                        problems.append(f"{row.get('id')} 订单场景缺少显式 HTTP 用户确认动作")
                except ValueError as err:
                    problems.append(f"{row.get('id')} HTTP 动作定义无效：{err}")
            for scenario in ("order", "memory_multiturn", "long_context"):
                if any(len(row.get("queries", [])) < 2 for row in rows if row.get("scenario") == scenario):
                    problems.append(f"agent 场景 {scenario} 存在非多轮用例")

    # 金标必须能回指当前版本的数据；跨平台同款只能算一个相关实体，不能靠重复 id 虚增 Recall。
    project_root = root.parents[1]
    catalog_path = project_root / "data" / "catalog-v1.jsonl"
    knowledge_dir = project_root / "knowledge"
    if catalog_path.is_file() and "product" in loaded:
        catalog = {row["product_id"]: row for row in _load_jsonl(catalog_path)}
        product_splits: dict[str, set[str]] = defaultdict(set)
        for row in loaded["product"]:
            relevant = row.get("relevant", [])
            declared_canonical = row.get("relevant_canonical_ids")
            if declared_canonical is None:
                problems.append(f"{row['id']} 缺少 relevant_canonical_ids")
            if not isinstance(row.get("constraints"), dict) or not row.get("target_currency"):
                problems.append(f"{row['id']} 缺少 constraints 或 target_currency")
            if row.get("expected_empty"):
                if relevant:
                    problems.append(f"{row['id']} 是无结果题却有 relevant")
                if declared_canonical not in ([], None):
                    problems.append(f"{row['id']} 是无结果题却有 canonical 金标")
                continue
            if not relevant:
                problems.append(f"{row['id']} 缺少商品金标")
                continue
            missing = [product_id for product_id in relevant if product_id not in catalog]
            if missing:
                problems.append(f"{row['id']} 金标商品不存在：{missing}")
                continue
            problems.extend(validate_product_case_constraints(row, catalog))
            canonical_ids = [catalog[product_id]["canonical_product_id"] for product_id in relevant]
            if len(canonical_ids) != len(set(canonical_ids)):
                problems.append(f"{row['id']} 同款跨平台商品被重复标为相关")
            if declared_canonical is not None and list(declared_canonical) != canonical_ids:
                problems.append(f"{row['id']} canonical 金标与商品金标不一致")
            for product_id in relevant:
                product_splits[product_id].add(row["split"])
        split_leaked_products = sorted(product_id for product_id, splits in product_splits.items() if len(splits) > 1)
        if split_leaked_products:
            problems.append(f"商品金标跨 split 泄漏：{split_leaked_products[:5]}")
    if knowledge_dir.is_dir() and "knowledge" in loaded:
        documents = {path.name for path in knowledge_dir.glob("*.md")}
        document_splits: dict[str, set[str]] = defaultdict(set)
        for row in loaded["knowledge"]:
            relevant = row.get("relevant", [])
            if row.get("expected_unanswerable"):
                if relevant:
                    problems.append(f"{row['id']} 是不可回答题却有 relevant")
                continue
            if not relevant:
                problems.append(f"{row['id']} 缺少知识金标")
            missing = [filename for filename in relevant if filename not in documents]
            if missing:
                problems.append(f"{row['id']} 金标知识文档不存在：{missing}")
            for filename in relevant:
                document_splits[filename].add(row["split"])
        split_leaked_documents = sorted(filename for filename, splits in document_splits.items() if len(splits) > 1)
        if split_leaked_documents:
            problems.append(f"知识金标跨 split 泄漏：{split_leaked_documents[:5]}")
    if "agent" in loaded:
        required_capabilities = {"retrieval", "knowledge", "order", "memory", "tool_failure", "safety", "long_context"}
        release_capabilities: set[str] = set()
        for row in loaded["agent"]:
            if not isinstance(row.get("queries"), list) or not row["queries"]:
                problems.append(f"{row.get('id', '<unknown>')} Agent 用例缺少 queries")
            if not isinstance(row.get("expected"), dict):
                problems.append(f"{row.get('id', '<unknown>')} Agent 用例缺少 expected")
            rubric = row.get("rubric")
            if not isinstance(rubric, dict) or set(rubric) != {"p0", "p1", "p2"}:
                problems.append(f"{row.get('id', '<unknown>')} Agent 用例缺少可执行 rubric")
            capabilities = row.get("capabilities")
            if not isinstance(capabilities, list) or not all(isinstance(value, str) and value for value in capabilities):
                problems.append(f"{row.get('id', '<unknown>')} Agent 用例缺少 capabilities")
            elif row.get("split") == "release":
                release_capabilities.update(capabilities)
        missing_capabilities = sorted(required_capabilities - release_capabilities)
        if missing_capabilities:
            problems.append(f"Agent release 缺少能力覆盖：{missing_capabilities}")
    return problems, summary
