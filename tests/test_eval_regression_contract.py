# -*- coding: utf-8 -*-
"""端到端回归不得用缺失 P0 的 Judge 输出算满分。"""
from __future__ import annotations

import pytest
import json

from scripts.eval.contracts import JudgeOutputError
from scripts.eval_regression import (
    apply_trace_evidence,
    build_judge_rubric,
    build_ground_truth,
    build_tool_fact_evidence,
    call_judge,
    exit_code_for_results,
    score_case,
    verify_fixed_trace_stability,
)
from app.application.prompts.loader import load_prompts


def test_score_case_rejects_missing_p0_instead_of_treating_it_as_full_score():
    rubric = {"p0": ["不得编造"], "p1": [], "p2": []}

    with pytest.raises(JudgeOutputError):
        score_case({"p0": [], "p1": [], "p2": []}, rubric)


def test_trace_evidence_overrides_same_criterion_instead_of_letting_judge_guess():
    rubric = {"p0": ["下单前不得创建订单"], "p1": [], "p2": []}
    judged = {
        "p0": [{"criterion": "下单前不得创建订单", "reason": "模型猜测。结论：通过", "pass": True}],
        "p1": [], "p2": [],
    }
    result = apply_trace_evidence(
        judged,
        rubric,
        {"p0": [{"criterion": "下单前不得创建订单", "kind": "forbidden_tools", "tools": ["create_order_tool"]}]},
        [{"type": "tool.invoke", "payload": {"tool": "create_order_tool"}}],
    )

    assert result["p0"][0]["pass"] is False
    assert result["p0"][0]["reason"].startswith("程序证据：")


def test_judge_rubric_excludes_deterministic_criteria():
    rubric = {"p0": ["价格恒等式", "不得承诺时效"], "p1": ["推荐命中"], "p2": []}
    deterministic = {
        "p0": [{"criterion": "价格恒等式", "kind": "landed_price_consistency"}],
    }

    assert build_judge_rubric(rubric, deterministic) == {
        "p0": ["不得承诺时效"],
        "p1": ["推荐命中"],
        "p2": [],
    }


def test_fixed_judgement_and_trace_have_stable_verdict_across_five_replays():
    rubric = {"p0": ["不下单"], "p1": [], "p2": []}
    judged = {"p0": [{"criterion": "不下单", "reason": "已遵守。结论：通过", "pass": True}], "p1": [], "p2": []}
    deterministic = {"p0": [{"criterion": "不下单", "kind": "forbidden_tools", "tools": ["create_order_tool"]}]}
    events = [{"type": "tool.invoke", "payload": {"tool": "product_search_tool"}}]

    assert verify_fixed_trace_stability(judged, rubric, deterministic, events, repeats=5) == (1.0, True, "PASS")


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([{"verdict": "PASS"}], 0),
        ([{"verdict": "PASS"}, {"verdict": "FAIL"}], 1),
        ([{"verdict": "ERROR"}], 1),
        ([], 1),
    ],
)
def test_eval_process_only_succeeds_when_all_cases_pass(results, expected):
    assert exit_code_for_results(results) == expected


def test_failed_deterministic_p1_assertion_blocks_case_even_when_score_is_above_threshold():
    rubric = {"p0": ["事实正确"], "p1": ["状态闭环", "表达动作"], "p2": ["清晰"]}
    judged = {
        "p0": [{"criterion": "事实正确", "reason": "正确。结论：通过", "pass": True}],
        "p1": [
            {"criterion": "状态闭环", "reason": "模型误判。结论：通过", "pass": True},
            {"criterion": "表达动作", "reason": "已表达。结论：通过", "pass": True},
        ],
        "p2": [{"criterion": "清晰", "reason": "清晰。结论：通过", "pass": True}],
    }
    deterministic = {
        "p1": [{"criterion": "状态闭环", "kind": "order_statuses", "expected": ["CANCELLED"]}],
    }
    events = [{"type": "tool.result", "payload": {"tool": "create_order_tool", "order": {"status": "CONFIRMED"}}}]

    score, p0_pass, verdict = verify_fixed_trace_stability(judged, rubric, deterministic, events)

    assert score > 0.7
    assert p0_pass is True
    assert verdict == "FAIL"


def test_main_prompt_requires_authoritative_confirmation_for_order_writes():
    prompt = load_prompts()["main_agent"]["system_prompt"]

    assert "只准备服务端确认单" in prompt
    assert "都不能代替页面上的确认动作" in prompt
    assert "原因已给出时不要重复索要" in prompt
    assert "未在 hits 或 filtered_out 中出现" in prompt
    assert "简单的现货推荐不要先查品类知识库" in prompt


def test_judge_ground_truth_includes_product_attributes_exposed_by_search_cards():
    ground_truth = build_ground_truth()

    assert "材质标签" in ground_truth
    assert "重量kg" in ground_truth
    assert "尺寸cm" in ground_truth
    assert "亮点" in ground_truth
    assert "配送国家" in ground_truth


def test_judge_receives_authoritative_tool_result_facts_for_derived_prices():
    evidence = build_tool_fact_evidence([
        {"type": "tool.invoke", "payload": {"tool": "product_search_tool"}},
        {
            "type": "tool.result",
            "payload": {
                "tool": "product_search_tool",
                "hits": [{"product_id": "P1", "landed_price": {"total": {"amount": 114, "currency": "CNY"}}}],
            },
        },
    ])

    assert "product_search_tool" in evidence
    assert '"amount": 114' in evidence
    assert "tool.invoke" not in evidence


@pytest.mark.asyncio
async def test_judge_retries_a_contract_error_without_loosening_validation(monkeypatch):
    rubric = {"p0": ["事实正确"], "p1": [], "p2": []}
    invalid = {
        "p0": [{"criterion": "事实正确", "reason": "正确", "pass": True}],
        "p1": [],
        "p2": [],
    }
    valid = {
        "p0": [{"criterion": "事实正确", "reason": "正确。结论：通过", "pass": True}],
        "p1": [],
        "p2": [],
    }

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self._body, ensure_ascii=False)}}]}

    class FakeClient:
        def __init__(self):
            self.responses = [invalid, valid]
            self.calls = []

        async def post(self, *args, **kwargs):
            self.calls.append(kwargs["json"])
            return FakeResponse(self.responses.pop(0))

    monkeypatch.setenv("LLM_BASE_URL", "https://judge.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    client = FakeClient()

    judged = await call_judge(client, "对话", rubric, "事实")

    assert judged == valid
    assert len(client.calls) == 2
    assert "reason 缺少显式结论" in client.calls[1]["messages"][-1]["content"]
