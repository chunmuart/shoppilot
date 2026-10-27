"""只读验证 Langfuse 项目、真实 Trace 和评分，不输出密钥或业务正文。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

import httpx

from app.infrastructure.langfuse_config import LangfuseConfig

OBSERVATION_FIELDS = "core,basic,model,usage,metrics"
COMPONENTS = {"api", "worker", "agent", "model", "tool"}


class VerificationError(ValueError):
    """只使用程序内固定错误码，禁止携带上游响应或异常正文。"""


def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    try:
        response = client.get(path, params=params)
    except httpx.HTTPError:
        raise VerificationError("remote_transport_error") from None
    if response.status_code != 200:
        raise VerificationError(f"remote_http_{response.status_code}")
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        raise VerificationError("remote_json_invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise VerificationError("remote_schema_invalid")
    return payload


def _pages(client: httpx.Client, path: str, params: dict) -> list[dict]:
    rows, cursors = [], set()
    for _ in range(10):
        payload = _get(client, path, params)
        if not all(isinstance(row, dict) for row in payload["data"]):
            raise VerificationError("remote_schema_invalid")
        rows.extend(payload["data"])
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            raise VerificationError("remote_schema_invalid")
        cursor = meta.get("cursor")
        if not cursor:
            return rows
        if not isinstance(cursor, str) or cursor in cursors:
            raise VerificationError("remote_cursor_invalid")
        cursors.add(cursor)
        params = {**params, "cursor": cursor}
    raise VerificationError("remote_pagination_limit")


def _component(row: dict) -> str:
    kind = str(row.get("type", "")).upper()
    if kind in {"AGENT", "GENERATION", "TOOL"}:
        return {"AGENT": "agent", "GENERATION": "model", "TOOL": "tool"}[kind]
    name = row.get("name")
    if isinstance(name, str):
        if re.match(r"^(?:HTTP )?(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)(?: |$)", name):
            return "api"
        if name == "commerce.intent.consume":
            return "worker"
    return "other"


def _tokens(row: dict, key: str) -> int | None:
    value = row.get(key + "Usage")
    if type(value) is int and value >= 0:
        return value
    details = row.get("usageDetails")
    value = details.get(key) if isinstance(details, dict) else None
    return value if type(value) is int and value >= 0 else None


def audit_trace(rows: list[dict], trace_id: str, project_id: str,
                required: set[str]) -> dict:
    issues: set[str] = set()
    if not rows:
        issues.add("trace_not_found")
    identifiers = [row.get("id") for row in rows]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise VerificationError("observation_id_invalid")
    nodes = {row["id"]: row for row in rows}
    if len(nodes) != len(rows):
        issues.add("duplicate_observation_ids")
    if any(row.get("traceId") != trace_id for row in rows):
        issues.add("observation_trace_mismatch")
    if any(row.get("projectId") != project_id for row in rows):
        issues.add("observation_project_mismatch")
    parents = {identifier: row.get("parentObservationId") for identifier, row in nodes.items()}
    if any(parent is not None and not isinstance(parent, str) for parent in parents.values()):
        raise VerificationError("observation_parent_invalid")
    roots = [identifier for identifier, parent in parents.items() if not parent]
    missing = {parent for parent in parents.values() if parent and parent not in nodes}
    if len(roots) != 1:
        issues.add("single_root_missing")
    if missing:
        issues.add("parent_observations_missing")
    for identifier in nodes:
        seen = set()
        current = identifier
        while current in parents:
            if current in seen:
                issues.add("observation_parent_cycle")
                break
            seen.add(current)
            current = parents[current]
    components = {identifier: _component(row) for identifier, row in nodes.items()}
    # 同一 trace 的连通树并不能证明上下文传播正确：模型和工具平铺在 API 下
    # 也会满足组件计数。按祖先校验，兼容框架包装 span 和嵌套 Agent。
    for identifier, component in components.items():
        ancestors = set()
        seen = {identifier}
        current = parents[identifier]
        while current in nodes and current not in seen:
            seen.add(current)
            ancestors.add(components[current])
            current = parents[current]
        if component == "worker" and "worker" in required and "api" not in ancestors:
            issues.add("worker_api_ancestor_missing")
        if component == "agent":
            if "api" in required and "api" not in ancestors:
                issues.add("agent_api_ancestor_missing")
            if "worker" in required and "worker" not in ancestors:
                issues.add("agent_worker_ancestor_missing")
        if component in {"model", "tool"} and "agent" not in ancestors:
            issues.add(f"{component}_agent_ancestor_missing")
    counts = Counter(components.values())
    if any(counts[name] == 0 for name in required):
        issues.add("required_components_missing")
    # 正式搜索验收要求已结束的 span，仍在飞的导出批次不能算完整链。
    unfinished = sum(not row.get("endTime") for row in rows)
    if unfinished:
        issues.add("observations_unfinished")
    error_count = sum(str(row.get("level", "")).upper() == "ERROR" for row in rows)
    if error_count:
        issues.add("observations_contain_errors")
    model_rows = [row for row in rows if _component(row) == "model"]
    usage = [(_tokens(row, "input"), _tokens(row, "output")) for row in model_rows]
    complete = bool(usage) and all(input_ is not None and output is not None for input_, output in usage)
    if not complete and ("model" in required or model_rows):
        issues.add("model_usage_incomplete")
    costs = [row.get("totalCost") for row in model_rows]
    cost_complete = bool(costs) and all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in costs)
    return {"status": "VERIFIED" if not issues else "INCOMPLETE", "trace_id": trace_id,
            "observation_count": len(rows), "component_counts": dict(sorted(counts.items())),
            "required_components": sorted(required), "root_count": len(roots),
            "parent_link_count": sum(bool(parent) and parent in nodes for parent in parents.values()),
            "missing_parent_count": len(missing), "unfinished_count": unfinished,
            "error_count": error_count, "usage_complete": complete,
            "input_tokens": sum(value[0] for value in usage) if complete else None,
            "output_tokens": sum(value[1] for value in usage) if complete else None,
            "cost_usd": round(sum(costs), 10) if cost_complete else None,
            "issues": sorted(issues)}


def audit_score(rows: list[dict], *, score_id: str, trace_id: str, project_id: str,
                name: str, value: float) -> dict:
    matches = [row for row in rows if row.get("id") == score_id]
    issues = []
    if len(matches) != 1:
        issues.append("score_not_uniquely_found")
    else:
        row = matches[0]
        subject = row.get("subject") or {}
        if (not isinstance(subject, dict) or subject.get("kind") != "trace"
                or subject.get("id") != trace_id):
            issues.append("score_trace_subject_mismatch")
        if row.get("projectId") != project_id:
            issues.append("score_project_mismatch")
        if row.get("name") != name:
            issues.append("score_name_mismatch")
        actual = row.get("value")
        if (row.get("dataType") != "NUMERIC" or type(actual) not in (int, float)
                or not math.isfinite(actual) or actual != value):
            issues.append("score_numeric_value_mismatch")
    return {"status": "VERIFIED" if not issues else "INCOMPLETE", "issues": issues,
            "subject_kind": "trace", "score_id_sha256": hashlib.sha256(score_id.encode()).hexdigest()[:24]}


def verify(config: LangfuseConfig, *, trace_id: str = "", project_id: str = "",
           required: set[str] | None = None, lookback_hours: float = 24,
           wait_seconds: float = 0, poll_interval: float = 2,
           score_id: str = "", score_name: str = "", score_value: float | None = None) -> dict:
    """只调用 GET；等待必须由调用方显式开启且有上限。"""
    report = {"schema_version": 1, "read_only": True,
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "remote_trace_verified": False}
    missing = config.missing_fields()
    if missing:
        return {**report, "status": "CONFIGURATION_MISSING", "missing_fields": missing}
    try:
        config.validate()
    except ValueError:
        return {**report, "status": "CONFIGURATION_INVALID", "error_code": "langfuse_configuration_invalid"}
    required = {"api", "agent", "model", "tool"} if required is None else required
    if (required - COMPONENTS or (trace_id and not re.fullmatch(r"[0-9a-f]{32}", trace_id))
            or not 0 < lookback_hours <= 24 * 31 or not 0 <= wait_seconds <= 600
            or not 0.1 <= poll_interval <= 60 or (score_id and (not trace_id or not score_name
            or type(score_value) not in (int, float) or not math.isfinite(score_value)))):
        return {**report, "status": "ARGUMENTS_INVALID"}
    try:
        with httpx.Client(base_url=config.base_url.rstrip("/") + "/", timeout=15,
                          headers={"Authorization": config.authorization_header},
                          follow_redirects=False) as client:
            projects = _get(client, "api/public/projects")["data"]
            ids = [row.get("id") for row in projects if isinstance(row, dict)]
            if project_id:
                if project_id not in ids:
                    raise VerificationError("target_project_not_accessible")
            elif len(ids) == 1 and isinstance(ids[0], str) and ids[0]:
                project_id = ids[0]
            else:
                raise VerificationError("target_project_ambiguous_or_missing")
            report["project"] = {"status": "ACCESS_VERIFIED", "accessible_count": len(ids),
                "project_id_sha256": hashlib.sha256(project_id.encode()).hexdigest()[:24]}
            if not trace_id:
                return {**report, "status": "PROJECT_ACCESS_VERIFIED", "trace": {"status": "NOT_REQUESTED"}}
            deadline = time.monotonic() + wait_seconds
            attempts = 0
            while True:
                attempts += 1
                now = datetime.now(timezone.utc)
                params = {"traceId": trace_id, "fields": OBSERVATION_FIELDS, "limit": 100,
                          "fromStartTime": (now - timedelta(hours=lookback_hours)).isoformat(),
                          "toStartTime": (now + timedelta(seconds=1)).isoformat()}
                rows = _pages(client, "api/public/v2/observations", params)
                report["trace"] = audit_trace(rows, trace_id, project_id, required)
                if score_id:
                    scores = _pages(client, "api/public/v3/scores", {
                        "id": score_id, "traceId": trace_id, "fields": "subject", "limit": 100})
                    report["score"] = audit_score(scores, score_id=score_id, trace_id=trace_id,
                        project_id=project_id, name=score_name, value=score_value)
                report["remote_trace_verified"] = report["trace"]["status"] == "VERIFIED"
                passed = report["remote_trace_verified"] and (not score_id or report["score"]["status"] == "VERIFIED")
                report.update(status="VERIFIED" if passed else "INCOMPLETE", read_attempts=attempts)
                remaining = deadline - time.monotonic()
                if passed or remaining <= 0:
                    return report
                time.sleep(min(poll_interval, remaining))
    except VerificationError as error:
        return {**report, "status": "REMOTE_CHECK_FAILED", "error_code": str(error)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--require-components", default="api,agent,model,tool")
    parser.add_argument("--lookback-hours", type=float, default=24)
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--score-id", default="")
    parser.add_argument("--score-name", default="")
    parser.add_argument("--score-value", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        config = LangfuseConfig.from_env(args.env_file)
        report = verify(config, trace_id=args.trace_id, project_id=args.project_id,
            required={value.strip() for value in args.require_components.split(",") if value.strip()},
            lookback_hours=args.lookback_hours, wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval, score_id=args.score_id,
            score_name=args.score_name, score_value=args.score_value)
    except ValueError:
        report = {"status": "CONFIGURATION_INVALID", "read_only": True,
                  "remote_trace_verified": False, "error_code": "langfuse_env_file_invalid"}
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        except OSError:
            print(json.dumps({"status": "REPORT_WRITE_FAILED", "remote_trace_verified": False}))
            return 2
    print(serialized)
    return 0 if report["status"] in {"VERIFIED", "PROJECT_ACCESS_VERIFIED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
