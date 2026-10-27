# -*- coding: utf-8 -*-
"""正式评测集规模、分桶和开发/发版隔离契约。"""
import json
from collections import Counter
from pathlib import Path

import yaml

from scripts.eval.eval_quality import validate_official_eval_fixture


def test_official_eval_fixture_has_300_cases_and_group_safe_splits():
    problems, summary = validate_official_eval_fixture(Path(__file__).resolve().parents[1] / "eval" / "v1")

    assert problems == []
    assert summary == {
        "product": {"total": 150, "dev": 105, "release": 45},
        "knowledge": {"total": 50, "dev": 35, "release": 15},
        "agent": {"total": 100, "dev": 70, "release": 30},
    }


def test_dataset_validation_entrypoint_includes_official_eval_gate():
    from scripts.eval.validate_datasets import validate_official_eval_fixture

    assert validate_official_eval_fixture() == []


def test_every_official_bucket_has_dev_and_release_coverage():
    """70/30 不能靠把整个场景切到同一侧来凑数。"""
    root = Path(__file__).resolve().parents[1] / "eval" / "v1"
    product = [json.loads(line) for line in (root / "product_retrieval.jsonl").read_text().splitlines() if line]
    knowledge = [json.loads(line) for line in (root / "knowledge_retrieval.jsonl").read_text().splitlines() if line]
    agent = yaml.safe_load((root / "agent_cases.yaml").read_text())["cases"]

    for rows, bucket_key in ((product, "kind"), (knowledge, "kind"), (agent, "scenario")):
        coverage: dict[str, set[str]] = {}
        for row in rows:
            coverage.setdefault(row[bucket_key], set()).add(row["split"])
        assert all(splits == {"dev", "release"} for splits in coverage.values())


def test_official_cases_have_executable_metadata_and_real_multiturn_scenarios():
    root = Path(__file__).resolve().parents[1] / "eval" / "v1"
    product = [json.loads(line) for line in (root / "product_retrieval.jsonl").read_text().splitlines() if line]
    knowledge = [json.loads(line) for line in (root / "knowledge_retrieval.jsonl").read_text().splitlines() if line]
    agent = yaml.safe_load((root / "agent_cases.yaml").read_text())["cases"]

    assert all(
        row.get("template_family")
        and row.get("target_currency")
        and isinstance(row.get("constraints"), dict)
        and row.get("relevant_canonical_ids") is not None
        for row in product
    )
    assert len({row["query"] for row in knowledge}) == len(knowledge)
    assert all(row.get("template_family") for row in knowledge)
    assert all(row.get("template_family") for row in agent)
    assert all(
        len(row["queries"]) >= 2
        for row in agent
        if row["scenario"] in {"order", "memory_multiturn", "long_context"}
    )


def test_release_agent_cases_cover_every_required_capability():
    """release 不能把知识或长上下文等整类能力留在 dev。"""
    root = Path(__file__).resolve().parents[1] / "eval" / "v1"
    agent = yaml.safe_load((root / "agent_cases.yaml").read_text())['cases']
    release_capabilities = {
        capability
        for row in agent if row["split"] == "release"
        for capability in row.get("capabilities", [])
    }

    assert {"retrieval", "knowledge", "order", "memory", "tool_failure", "safety", "long_context"} <= release_capabilities


def test_order_and_safety_cases_include_a_complete_shipping_address():
    """订单题要以确认策略为唯一变量，不能因地址缺失产生假失败或假通过。"""
    root = Path(__file__).resolve().parents[1] / "eval" / "v1"
    agent = yaml.safe_load((root / "agent_cases.yaml").read_text())["cases"]

    for row in agent:
        if row["scenario"] not in {"order", "safety"}:
            continue
        query = row["queries"][0]
        assert "收货地址" in query
        assert "310000" in query
        assert "13800000000" in query


def test_order_and_safety_gold_products_are_orderable_to_cn():
    root = Path(__file__).resolve().parents[1]
    agent = yaml.safe_load((root / "eval" / "v1" / "agent_cases.yaml").read_text())["cases"]
    catalog = {
        row["product_id"]: row
        for row in (
            json.loads(line)
            for line in (root / "data" / "catalog-v1.jsonl").read_text().splitlines()
            if line
        )
    }

    for row in agent:
        if row["scenario"] not in {"order", "safety"}:
            continue
        product_id = row["expected"].get("target_product_id")
        assert product_id in catalog
        product = catalog[product_id]
        assert "CN" in product["ships_to"]
        assert any(sku["stock"] > 0 for sku in product["skus"])


def test_order_confirmation_names_an_in_stock_sku():
    """订单第二轮点名的规格必须可购，不能要求 Agent 对缺货 SKU 下单。"""
    root = Path(__file__).resolve().parents[1]
    agent = yaml.safe_load((root / "eval" / "v1" / "agent_cases.yaml").read_text())["cases"]
    catalog = {
        row["product_id"]: row
        for row in (
            json.loads(line)
            for line in (root / "data" / "catalog-v1.jsonl").read_text().splitlines()
            if line
        )
    }

    for row in agent:
        if row["scenario"] != "order":
            continue
        product = catalog[row["expected"]["target_product_id"]]
        confirmed_query = row["queries"][1]
        confirmed_skus = [
            sku for sku in product["skus"]
            if sku["spec"] in confirmed_query
        ]
        assert len(confirmed_skus) == 1
        assert confirmed_skus[0]["stock"] > 0


def test_long_context_cases_start_with_an_actual_product_lookup():
    root = Path(__file__).resolve().parents[1] / "eval" / "v1"
    agent = yaml.safe_load((root / "agent_cases.yaml").read_text())["cases"]

    for row in agent:
        if row["scenario"] == "long_context":
            assert "先查" in row["queries"][0]


def test_quality_checker_rejects_a_bucket_missing_release_coverage():
    from scripts.eval.eval_quality import validate_bucket_split_coverage

    problems = validate_bucket_split_coverage(
        [{"kind": "literal", "split": "dev"}, {"kind": "semantic", "split": "dev"}, {"kind": "semantic", "split": "release"}],
        "kind",
        "product",
    )

    assert problems == ["product 分桶 literal 未同时覆盖 dev/release：['dev']"]


def test_product_gold_validator_rejects_a_hard_constraint_violation():
    from scripts.eval.eval_quality import validate_product_case_constraints

    row = {
        "id": "prod-bad", "relevant": ["P-BAD"], "target_currency": "CNY",
        "constraints": {
            "category": "户外运动", "ship_to": "CN", "require_in_stock": True,
            "required_material_tags": ["金属"], "excluded_material_tags": ["合成聚合物"],
            "price_max_major": 100,
        },
    }
    catalog = {
        "P-BAD": {
            "product_id": "P-BAD", "category": "家居生活", "ships_to": ["US"],
            "material_tags": ["合成聚合物"],
            "skus": [{"price_major": 101, "currency": "CNY", "stock": 0}],
        },
    }

    problems = validate_product_case_constraints(row, catalog)

    assert any("category" in problem for problem in problems)
    assert any("ship_to" in problem for problem in problems)
    assert any("material" in problem for problem in problems)
    assert any("stock" in problem for problem in problems)
    assert any("price" in problem for problem in problems)
