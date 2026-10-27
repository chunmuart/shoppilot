# -*- coding: utf-8 -*-
"""评测器契约测试：坏 Judge 输出和硬约束泄漏都不能被静默放过。"""
from __future__ import annotations

import pytest


def _contracts():
    from scripts.eval.contracts import JudgeOutputError, validate_judge_output

    return JudgeOutputError, validate_judge_output


def _find_violations():
    from scripts.eval.hard_constraints import find_hit_constraint_violations

    return find_hit_constraint_violations


def _rubric() -> dict:
    return {
        "p0": ["价格必须来自工具"],
        "p1": ["调用商品检索"],
        "p2": ["表达清晰"],
    }


def _judge_output() -> dict:
    return {
        "p0": [{"criterion": "价格必须来自工具", "reason": "工具结果金额一致。结论：通过", "pass": True}],
        "p1": [{"criterion": "调用商品检索", "reason": "事件中有 product_search_tool。结论：通过", "pass": True}],
        "p2": [{"criterion": "表达清晰", "reason": "分项明确。结论：通过", "pass": True}],
    }


def test_judge_output_requires_every_rubric_criterion_once():
    JudgeOutputError, validate_judge_output = _contracts()
    output = _judge_output()
    output["p1"] = []

    with pytest.raises(JudgeOutputError, match="p1"):
        validate_judge_output(output, _rubric())


def test_judge_output_rejects_non_boolean_pass():
    JudgeOutputError, validate_judge_output = _contracts()
    output = _judge_output()
    output["p0"][0]["pass"] = "true"

    with pytest.raises(JudgeOutputError, match="pass"):
        validate_judge_output(output, _rubric())


def test_judge_output_rejects_criterion_not_in_rubric():
    JudgeOutputError, validate_judge_output = _contracts()
    output = _judge_output()
    output["p2"][0]["criterion"] = "编造的标准"

    with pytest.raises(JudgeOutputError, match="criterion"):
        validate_judge_output(output, _rubric())


def test_judge_output_requires_explicit_final_verdict():
    JudgeOutputError, validate_judge_output = _contracts()
    output = _judge_output()
    output["p0"][0]["reason"] = "工具结果金额一致"

    with pytest.raises(JudgeOutputError, match="显式结论"):
        validate_judge_output(output, _rubric())


def test_constraint_checker_reports_price_shipping_and_stock_leaks():
    find_hit_constraint_violations = _find_violations()
    hits = [
        {"product_id": "P-OVER", "price_major": 301, "currency": "CNY", "skus": [{"stock": 10}]},
        {"product_id": "P-NOSTOCK", "price_major": 99, "currency": "CNY", "skus": [{"stock": 0}]},
        {"product_id": "P-OK", "price_major": 99, "currency": "CNY", "skus": [{"stock": 10}]},
    ]
    products = {
        "P-OVER": {"ships_to": ["US"]},
        "P-NOSTOCK": {"ships_to": ["CN"]},
        "P-OK": {"ships_to": ["CN"]},
    }

    violations = find_hit_constraint_violations(
        hits,
        products,
        price_max_major=300,
        ship_to="CN",
        target_currency="CNY",
        require_in_stock=True,
    )

    assert violations == {
        "P-OVER": {"over_price_cap", "ship_to_unavailable"},
        "P-NOSTOCK": {"out_of_stock"},
    }


def test_constraint_checker_reports_excluded_material_leak():
    find_hit_constraint_violations = _find_violations()

    violations = find_hit_constraint_violations(
        [{"product_id": "P-POLY", "price_major": 99, "currency": "CNY", "skus": [{"stock": 1}]}],
        {"P-POLY": {"ships_to": ["CN"], "material_tags": ["合成聚合物"]}},
        excluded_material_tags=["合成聚合物"],
    )

    assert violations == {"P-POLY": {"material_excluded"}}


def test_product_recall_check_filter_rejects_returned_product_above_price_cap():
    """旧逻辑只检查 relevant 是否被过滤，预算超标命中会被漏掉。"""
    from scripts.eval.run_product_recall import check_filter

    ok, note = check_filter(
        {"relevant": [], "price_max_major": 300},
        [{"product_id": "P-OVER", "price_major": 301, "currency": "CNY", "skus": [{"stock": 5}]}],
        [],
        {"P-OVER": {"ships_to": ["CN"]}},
    )

    assert ok is False
    assert "over_price_cap" in note


def test_product_recall_check_filter_evaluates_material_only_constraint():
    from scripts.eval.run_product_recall import check_filter

    ok, note = check_filter(
        {"relevant": [], "excluded_material_tags": ["合成聚合物"]},
        [{"product_id": "P-POLY", "price_major": 99, "currency": "CNY", "skus": [{"stock": 3}]}],
        [],
        {"P-POLY": {"ships_to": ["CN"], "material_tags": ["合成聚合物"]}},
    )

    assert ok is False
    assert "material_excluded" in note


@pytest.mark.parametrize(
    ("mutation_id", "expected"),
    [(index, expected) for index, expected in enumerate(
        ["over_price_cap"] * 4
        + ["out_of_stock"] * 4
        + ["ship_to_unavailable"] * 4
        + ["material_excluded"] * 4
        + ["currency_mismatch"] * 4,
        start=1,
    )],
)
def test_twenty_injected_constraint_mutations_are_all_captured(mutation_id, expected):
    """发版门禁要求：20 条故意坏样本不能有一条从评测器漏过。"""
    find_hit_constraint_violations = _find_violations()
    product_id = f"P-MUTATION-{mutation_id}"
    product = {"ships_to": ["CN"], "material_tags": ["金属"]}
    hit = {"product_id": product_id, "price_major": 99, "currency": "CNY", "skus": [{"stock": 5}]}
    kwargs = {"price_max_major": 300, "ship_to": "CN", "target_currency": "CNY", "require_in_stock": True}
    if expected == "over_price_cap":
        hit["price_major"] = 301
    elif expected == "out_of_stock":
        hit["skus"] = [{"stock": 0}]
    elif expected == "ship_to_unavailable":
        product["ships_to"] = ["US"]
    elif expected == "material_excluded":
        product["material_tags"] = ["合成聚合物"]
        kwargs["excluded_material_tags"] = ["合成聚合物"]
    elif expected == "currency_mismatch":
        hit["currency"] = "USD"

    violations = find_hit_constraint_violations([hit], {product_id: product}, **kwargs)

    assert expected in violations[product_id]
