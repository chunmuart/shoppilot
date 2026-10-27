"""GEPA 离线工作流：隔离留出集、真实引擎、只导出候选。"""

import json
from pathlib import Path
import pytest


def test_split_leakage_and_output_overwrite_are_rejected(tmp_path):
    from scripts.optimize_prompts import validate_cases, reserve_output

    cases = [
        {"id": str(i), "split": s, "query": q, "expected": {}}
        for i, (s, q) in enumerate(
            [("train", "预算300背包"), ("val", "预算300背包"), ("holdout", "杯子")]
        )
    ]
    with pytest.raises(ValueError, match="重复"):
        validate_cases(cases)
    reserve_output(tmp_path / "out")
    with pytest.raises(FileExistsError):
        reserve_output(tmp_path / "out")


def test_gepa_runs_real_engine_and_keeps_holdout_out_of_reflection(tmp_path):
    pytest.importorskip("gepa")
    from scripts.optimize_prompts import optimize_candidate

    calls = []
    cases = [
        {"id": s, "split": s, "query": s, "expected": {}}
        for s in ["train", "val", "holdout"]
    ]

    def evaluate(case, text):
        calls.append((case["id"], text))
        return {
            "score": int("改进" in text),
            "feedback": "需要明确保留预算",
            "violations": [],
            "usage": [],
        }

    reflected = []

    def propose(candidate, dataset, components):
        reflected.extend(str(dataset))
        assert "holdout" not in str(dataset)
        return {"instruction": "改进版"}

    report = optimize_candidate(
        "基线", cases, evaluate, tmp_path / "run", max_calls=8, proposer=propose
    )
    assert report["candidate"] == "改进版"
    assert report["holdout"]["baseline_scores"] == [0]
    assert report["holdout"]["candidate_scores"] == [1]
    assert report["publication"] == "manual_review_required"
    assert report["scope"] == "isolated_search_tool_harness"
    assert (tmp_path / "run/report.json").is_file()


def test_agentscope_reflection_accepts_gepa_string_prompt():
    import asyncio
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from agentscope.message import TextBlock
    from scripts.optimize_prompts import SearchEvaluator

    with asyncio.Runner() as runner:
        evaluator = object.__new__(SearchEvaluator)
        evaluator.runner = runner
        evaluator.reflection_usage = []
        evaluator.model = AsyncMock(
            return_value=SimpleNamespace(content=[TextBlock(text="改进提示")])
        )
        assert evaluator.reflect("请根据失败轨迹优化") == "改进提示"
        assert (
            evaluator.model.await_args.args[0][0].content[0].text
            == "请根据失败轨迹优化"
        )


def test_public_skill_export_preserves_permissions_and_enters_draft_only(tmp_path):
    from scripts.optimize_prompts import skill_candidate
    from tests.test_capability_registry import document
    from app.infrastructure.capability_registry import CapabilityRegistry

    source = document()
    candidate = skill_candidate(
        source, "先核对预算，不擅自扩大国家范围。", tmp_path / "report.json"
    )
    assert candidate["allowed_tools"] == source["allowed_tools"]
    assert (
        candidate["scope"] == source["scope"]
        and candidate["version"] != source["version"]
    )
    registry = CapabilityRegistry(tmp_path / "registry.db")
    saved = registry.import_draft(candidate, author="isolated-test")
    assert saved["state"] == "draft"
    assert source["body"] != candidate["body"]
