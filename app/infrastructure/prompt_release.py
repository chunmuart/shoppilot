"""人工 Prompt 发布前的成对 release 门禁；缺证据不等于通过。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from app.infrastructure.prompt_registry import PromptRegistryError, sha256

METRIC_FIELDS = ("task_success_rate", "hard_constraint_pass_rate", "latency_p95_ms", "input_tokens", "output_tokens")


def _load(path: Path, expected_version: str) -> tuple[dict, dict]:
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise PromptRegistryError("release manifest 必须是对象")
    selection, execution = manifest.get("selection", {}), manifest.get("execution", {})
    parameters = manifest.get("parameters", {})
    if (manifest.get("runner") != "agent_regression" or selection.get("split") != "release"
            or selection.get("complete_split") is not True or selection.get("only") is not None
            or parameters.get("gate_scope") != "release" or parameters.get("dry_run") is not False
            or execution.get("gate") != "PASS" or execution.get("status") != "COMPLETED"
            or execution.get("inputs_unchanged") is not True):
        raise PromptRegistryError("发布要求完整、真实执行、输入未变且 PASS 的 release manifest")
    runtime = manifest.get("service_runtime", {})
    if (runtime.get("status") != "matched" or runtime.get("matching") is not True
            or not runtime.get("local_app_source_sha256")
            or runtime["local_app_source_sha256"] != runtime.get("server_app_source_sha256")):
        raise PromptRegistryError("发布证据缺少实际服务源代码版本匹配")
    health = manifest.get("server_health", {})
    version = health.get("prompt_registry", {}).get("effective_version") or {}
    if (health.get("semantic_cache") is not False or version.get("version_id") != expected_version
            or version.get("content_sha256") != expected_version.removeprefix("p-")):
        raise PromptRegistryError("报告未证明实际执行了待发布 Prompt 版本，或语义缓存未关闭")
    results = execution.get("results", [])
    if (not isinstance(results, list) or not results or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in results)
            or type(selection.get("selected_count")) is not int
            or len(results) != selection["selected_count"] or len(results) != selection.get("split_count")
            or len({row.get("id") for row in results}) != len(results)
            or sorted(row.get("id") for row in results) != sorted(selection.get("case_ids", []))
            or any(row.get("p0_pass") is not True or row.get("verdict") != "PASS" for row in results)):
        raise PromptRegistryError("P0/任务失败、缺用例或重复结果阻断 Prompt 发布")
    metrics = execution.get("release_metrics")
    if not isinstance(metrics, dict) or metrics.get("usage_complete") is not True:
        raise PromptRegistryError("缺少可靠成本/时延/任务指标，不能仅凭 Judge 总分发布")
    for name in METRIC_FIELDS:
        value = metrics.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise PromptRegistryError(f"发布指标 {name} 未实测或无效")
    if any(metrics[name] > 1 for name in ("task_success_rate", "hard_constraint_pass_rate")):
        raise PromptRegistryError("成功率和硬约束率必须为 0..1")
    if metrics["latency_p95_ms"] <= 0 or metrics["input_tokens"] + metrics["output_tokens"] <= 0:
        raise PromptRegistryError("零时延/零 Token 不足以证明真实模型成对执行")
    case_metrics = [row.get("metrics", {}) for row in results]
    for row in case_metrics:
        if (row.get("usage_complete") is not True
                or row.get("latency_scope") != "agent_dialogue_and_http_actions_excluding_judge"
                or type(row.get("elapsed_ms")) not in (int, float) or not math.isfinite(row["elapsed_ms"]) or row["elapsed_ms"] <= 0
                or any(type(row.get(key)) is not int or row[key] < 0 for key in ("input_tokens", "output_tokens"))):
            raise PromptRegistryError("每条用例均需完整真实 usage 与明确时延口径")
    derived = {"task_success_rate": 1.0, "hard_constraint_pass_rate": 1.0,
        "latency_p95_ms": sorted(row["elapsed_ms"] for row in case_metrics)[math.ceil(len(case_metrics) * .95) - 1],
        "input_tokens": sum(row["input_tokens"] for row in case_metrics),
        "output_tokens": sum(row["output_tokens"] for row in case_metrics)}
    if any(metrics[key] != derived[key] for key in METRIC_FIELDS):
        raise PromptRegistryError("发布汇总指标与逐条证据不一致")
    return manifest, {"manifest_sha256": sha256(raw), "version_id": expected_version, "metrics": metrics,
                      "toolset_sha256": version.get("toolset_sha256")}


def compare_release_manifests(baseline_path: Path, candidate_path: Path, baseline_version: str, candidate_version: str) -> dict:
    before, baseline = _load(baseline_path, baseline_version)
    after, candidate = _load(candidate_path, candidate_version)
    if baseline_version == candidate_version:
        raise PromptRegistryError("候选版本必须与基线不同")
    for location, field in (("selection", "selected_content_sha256"), ("data", "sha256"), ("code", "sha256")):
        if not before.get(location, {}).get(field) or before[location][field] != after.get(location, {}).get(field):
            raise PromptRegistryError("成对比较要求同一冻结代码、release 选集与数据/知识版本")
    if (not before.get("models") or not before["execution"].get("actual_strategies")
            or not baseline["toolset_sha256"] or baseline["toolset_sha256"] != candidate["toolset_sha256"]
            or before.get("models") != after.get("models")
            or before["execution"].get("actual_strategies") != after["execution"].get("actual_strategies")):
        raise PromptRegistryError("成对比较的工具契约、模型和实际检索策略必须一致")
    old, new = baseline["metrics"], candidate["metrics"]
    if new["task_success_rate"] < old["task_success_rate"] or new["hard_constraint_pass_rate"] < old["hard_constraint_pass_rate"]:
        raise PromptRegistryError("任务成功率或硬约束率退化，阻断发布")
    if new["latency_p95_ms"] > old["latency_p95_ms"] * 1.20:
        raise PromptRegistryError("P95 总时延退化超过 20%，阻断发布")
    old_tokens, new_tokens = old["input_tokens"] + old["output_tokens"], new["input_tokens"] + new["output_tokens"]
    if new_tokens > old_tokens * 1.10:
        raise PromptRegistryError("Token 成本代理退化超过 10%，阻断发布")
    return {"gate": "PASS", "baseline": baseline, "candidate": candidate,
        "selected_content_sha256": before["selection"]["selected_content_sha256"],
        "policy": {"version": 1, "cost_unit": "input_plus_output_tokens_proxy_not_currency", "latency_max_ratio": 1.2, "token_max_ratio": 1.1}}
