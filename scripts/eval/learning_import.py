"""显式识别两代报告；未知格式、缺失结果和运行中报告不能静默导入零条。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.domain.learning import digest


def read_report(path: Path):
    path = path.resolve()
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") == "harness-eval-v1":
        if manifest.get("status") in {None, "running"}:
            raise ValueError("运行中的 Harness 报告不能回流")
        result_bytes = path.with_name("results.json").read_bytes()
        rows = json.loads(result_bytes)
        if not isinstance(rows, list) or not rows:
            raise ValueError("Harness 结果为空或格式无效")
        keys = set()
        for row in rows:
            if not isinstance(row, dict) or not all(k in row for k in ("case_id", "strategy", "repetition", "passed", "checks")) or type(row["passed"]) is not bool or not isinstance(row["checks"], dict):
                raise ValueError("Harness 结果缺少场景/策略/重复次数/断言")
            key = (row["case_id"], row["strategy"], row["repetition"])
            if key in keys:
                raise ValueError("Harness 结果重复")
            keys.add(key)
        source_hash = hashlib.sha256(raw+b"\n"+result_bytes).hexdigest()
        return manifest, rows, source_hash
    execution = manifest.get("execution")
    if not isinstance(execution, dict) or not ({"results", "observations"} & set(execution)):
        raise ValueError("不支持的评测报告格式")
    rows = execution.get("results", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("id") or row.get("verdict") not in {"PASS", "FAIL", "ERROR", "BLOCKED"} for row in rows):
        raise ValueError("旧版评测结果不完整")
    return manifest, rows, hashlib.sha256(raw).hexdigest()


def import_learning_report(store, path: Path):
    manifest, rows, source_hash = read_report(path)
    harness = manifest.get("schema_version") == "harness-eval-v1"
    case_path = path.with_name("cases.json")
    cases = {}
    if case_path.exists():
        raw_cases = json.loads(case_path.read_text())
        if not isinstance(raw_cases, list):
            raise ValueError("场景文件必须是列表")
        cases = {case["id"]: case for case in raw_cases}
    jobs = []
    for row in rows:
        case_id = row["case_id"] if harness else row["id"]
        split = row.get("split") or ("holdout" if manifest.get("profile") == "release" else "dev")
        # 来源为 release 时不能被单行 split=dev 覆盖。
        held_out = split in {"holdout", "release"} or manifest.get("profile") == "release"
        passed = row["passed"] if harness else row["verdict"] == "PASS"
        run_id = manifest.get("run_id", source_hash)
        source_ref = f"{path.resolve()}#{case_id}:{row.get('strategy','legacy')}:{row.get('repetition',0)}"
        evidence = {"buyer_id": "evaluation-isolated", "session_id": run_id,
                    "run_id": run_id, "case_id": case_id, "source_refs": [source_ref],
                    "lineage_id": case_id, "split": "holdout" if held_out else split,
                    "learning_allowed": not held_out,
                    "input": cases.get(case_id, {}).get("question") or cases.get(case_id, {}).get("query") or row.get("input") or {"case_id": case_id},
                    "output": row.get("answer", row.get("output")), "facts": [],
                    "checks": row.get("checks", {}), "outcome": "unverified" if passed else "failure",
                    "meaningful": False, "failure_type": "execution_error" if row.get("error") else "assertion_failure",
                    "versions": {"source_sha256": manifest.get("source", {}).get("sha256"), "model": manifest.get("model"),
                                 "dataset_sha256": manifest.get("dataset_sha256"), "scorer_version": manifest.get("scorer_version")}}
        identity = store.enqueue(source_kind="evaluation", source_ref=source_ref, source_hash=digest([source_hash, row, cases.get(case_id)]),
                                 buyer_id=evidence["buyer_id"], session_id=run_id, payload=evidence)
        jobs.append(identity)
    return {"jobs": len(jobs), "job_ids": jobs, "trainer_started": False}
