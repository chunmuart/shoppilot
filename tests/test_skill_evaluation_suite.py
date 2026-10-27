"""600条场景的去重、分区、评分负例与真实数据库验证；不调用外部模型。"""
from copy import deepcopy
import json

import pytest

from scripts.eval.build_skill_cases import build_suite
from scripts.eval.skill_suite import load_suite, validate_suite, select_cases, run_contract
from scripts.eval.run_learning import check_calls, aggregate, experiment_passed
from scripts.eval.run_skill_suite import summary_for, render

SUITE = load_suite()


def test_frozen_dataset_matches_generator_and_family_split():
    assert SUITE == build_suite()
    inventory = validate_suite(SUITE)
    assert inventory["cases"] == 600 and inventory["families"] == 60
    assert inventory["adapters"] == {"model_search": 400, "safety": 100, "lifecycle": 100}
    assert len(select_cases(SUITE, "dev")) == 240
    assert len(select_cases(SUITE, "release")) == 360
    assert len(select_cases(SUITE, "smoke")) == 6


@pytest.mark.parametrize("mutation", ["duplicate_input", "cross_split", "unknown_field", "unknown_country", "unsafe_id"])
def test_invalid_or_leaking_dataset_is_rejected(mutation):
    suite = deepcopy(SUITE)
    if mutation == "duplicate_input":
        identity = suite["cases"][1]["id"]
        suite["cases"][1] = {**deepcopy(suite["cases"][0]), "id": identity, "variant": 2}
    elif mutation == "cross_split": suite["cases"][0]["split"] = "holdout"
    elif mutation == "unknown_field": suite["cases"][0]["expected"]["brand"] = "test"
    elif mutation == "unknown_country": suite["cases"][0]["expected"]["ship_to"] = "ZZ"
    else: suite["cases"][0]["id"] = "../../real-data"
    with pytest.raises(ValueError): validate_suite(suite)


def test_scorer_rejects_wrong_field_no_call_and_unapproved_write_for_all_model_cases():
    for case in SUITE["cases"]:
        if case["adapter"] != "model_search": continue
        good = [{"tool": "product_search_tool", "args": case["expected"]}]
        assert check_calls(case, good) == []
        assert check_calls(case, [])
        assert check_calls(case, good+[{"tool": "place_order_tool", "args": {}}])
        for field, value in case["expected"].items():
            bad = deepcopy(good)
            bad[0]["args"][field] = (["不应有的条件"] if value == [] else [] if isinstance(value, list)
                                     else 999999 if field == "price_max_major" else "wrong")
            assert check_calls(case, bad), (case["id"], field)


@pytest.mark.parametrize("case", [c for c in SUITE["cases"] if c["adapter"] != "model_search"], ids=lambda c: c["id"])
async def test_production_storage_scenarios(case, tmp_path):
    result = await run_contract(case, tmp_path/"isolated")
    assert result["passed"], result


def test_unexecuted_models_are_not_reported_as_success(tmp_path):
    selected = select_cases(SUITE, "all")
    summary = summary_for(SUITE, [], selected, "validate", "all", 3)
    assert summary["executed_cases"] == 0 and summary["unexecuted_selected_cases"] == 600
    assert summary["model_metrics"] is None and not summary["benefit_confirmed"]
    render(tmp_path, SUITE, summary, [])
    text = (tmp_path/"report.html").read_text()
    assert "未执行" in text and "NOT_CONNECTED" not in text
    assert text.count('<details data-group=') == 600


def test_gate_uses_dataset_size_and_partial_usage_remains_unknown():
    summary = {"expected_runs_per_arm": 75, "candidate": {"runs": 75, "passed": 75},
               "baseline": {"runs": 75}, "treatment_gate": "PASS"}
    assert experiment_passed(summary)
    summary["candidate"]["runs"] = 74
    assert not experiment_passed(summary)
    summary["candidate"] = {"runs": 36, "passed": 36}
    assert not experiment_passed(summary)
    rows = [{"case_id": "a", "arm": "baseline", "passed": True, "usage": [{"input_tokens": 10}], "elapsed_ms": 1, "tool_calls": []},
            {"case_id": "b", "arm": "baseline", "passed": False, "usage": [], "elapsed_ms": 1, "tool_calls": [], "error": "Timeout"}]
    assert aggregate(rows, []) ["baseline"]["input_tokens"] is None


def test_pairing_uses_family_not_300_repeated_observations():
    cases = [c for c in SUITE["cases"] if c["group"] == "expression"]
    rows = [{"case_id": c["id"], "group": c["group"], "family": c["family"], "arm": arm,
             "execution": "real_model", "passed": arm == "candidate", "usage": [{"input_tokens": 1}],
             "elapsed_ms": 1, "tool_calls": []} for c in cases for arm in ("baseline", "candidate") for _ in range(3)]
    result = summary_for(SUITE, rows, cases, "model", "all", 3)
    assert result["model_metrics"]["paired_family_count"] == 10
    assert result["model_metrics"]["paired_family_ci95"] == [1, 1]
    assert "paired_scene_ci95" not in result["model_metrics"]
