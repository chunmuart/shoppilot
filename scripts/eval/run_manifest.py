# -*- coding: utf-8 -*-
"""评测选集与证据清单。只记录白名单配置，绝不导出凭据或本地用户数据。"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from collections import Counter
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPLITS = ("all", "dev", "release")
_SOURCE_DIRS = ("app", "scripts", "tests", "frontend/src", "frontend/tests", "docker", ".github")
_SOURCE_FILES = ("pyproject.toml", "uv.lock", "frontend/package.json", "frontend/package-lock.json", "frontend/vite.config.ts", "frontend/tsconfig.json")
_SOURCE_SUFFIXES = {".py", ".yml", ".yaml", ".toml", ".json", ".ts", ".tsx", ".css", ".sh", ".lock"}


def json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def public_endpoint(value: str) -> str:
    """保留服务定位信息，去掉 userinfo、查询串和 fragment 中可能携带的密钥。"""
    if not value:
        return ""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def select_cases(rows: list[dict], split: str = "all", only: str | None = None) -> tuple[list[dict], dict]:
    """先过滤再装配外部依赖。显式 split 不容忍缺失/错写标签，也不回退到全集。"""
    if split not in SPLITS:
        raise ValueError(f"未知 split：{split}")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("评测输入必须为用例对象列表")
    if any(row.get("split") not in (None, "dev", "release") for row in rows):
        raise ValueError("评测用例存在非法 split")
    if split != "all" and any(row.get("split") is None for row in rows):
        raise ValueError("显式 dev/release 评测要求所有用例都声明 split；旧集请使用 --split all")
    candidates = [(index, row) for index, row in enumerate(rows, 1) if split == "all" or row.get("split") == split]
    chosen = [(index, row) for index, row in candidates if only is None or row.get("id") == only]
    if not chosen:
        raise ValueError(f"没有可执行用例：split={split}, only={only}")
    ids = [str(row.get("id") or f"line-{index}") for index, row in chosen]
    if len(ids) != len(set(ids)):
        raise ValueError("选中用例的 id 重复，无法建立独立证据")
    selected = [row for _, row in chosen]
    return selected, {
        "split": split, "only": only, "source_count": len(rows), "split_count": len(candidates),
        "selected_count": len(selected), "case_ids": ids, "complete_split": len(chosen) == len(candidates),
        "split_counts": dict(sorted(Counter(str(row.get("split") or "unlabelled") for row in selected).items())),
        "bucket_counts": dict(sorted(Counter(str(row.get("scenario") or row.get("kind") or "unspecified") for row in selected).items())),
        "selected_content_sha256": json_hash(selected),
    }


def _fingerprint(paths: list[Path], root: Path) -> dict:
    files = []
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        try:
            name = str(path.relative_to(root))
        except ValueError:
            name = str(path)
        contents = path.read_bytes()
        files.append({"path": name, "sha256": hashlib.sha256(contents).hexdigest(), "bytes": len(contents)})
    return {"sha256": json_hash(files), "file_count": len(files), "files": files}


def worktree_fingerprint(root: Path = PROJECT_ROOT) -> dict:
    paths = [root / name for name in _SOURCE_FILES]
    for directory in _SOURCE_DIRS:
        paths.extend(path for path in (root / directory).rglob("*")
                     if path.suffix in _SOURCE_SUFFIXES and "__pycache__" not in path.parts and "node_modules" not in path.parts)
    result = _fingerprint(paths, root)
    result["scope"] = {"directories": list(_SOURCE_DIRS), "files": list(_SOURCE_FILES), "suffixes": sorted(_SOURCE_SUFFIXES)}
    return result


def _git_value(root: Path, args: list[str]) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def validate_baseline_selection(path: Path | None, selection: dict, dataset: Path) -> None:
    """已批准的全集基线不能偷偷套到 dev 或 release，也不能拿不同数据版本比较。"""
    if path is None:
        return
    baseline = json.loads(path.read_text(encoding="utf-8"))
    declared_split = baseline.get("split", "all")
    if declared_split != selection["split"]:
        raise ValueError(f"基线 split={declared_split} 与本次 {selection['split']} 不一致，需对应选集的批准基线")
    expected_hash = baseline.get("selected_content_sha256")
    if expected_hash is not None and expected_hash != selection["selected_content_sha256"]:
        raise ValueError("基线选集内容 hash 与本次不一致")
    if selection["split"] == "release" and expected_hash is None:
        raise ValueError("release 基线必须声明 selected_content_sha256，不能使用未绑定数据版本的基线")
    if baseline.get("dataset") and (PROJECT_ROOT / baseline["dataset"]).resolve() != dataset.resolve():
        raise ValueError("基线 dataset 与本次输入不一致")


def build_manifest(*, runner: str, dataset: Path, selection: dict, parameters: dict,
                   baseline: Path | None = None, judge_prompt: str | None = None,
                   root: Path = PROJECT_ROOT) -> dict:
    source = worktree_fingerprint(root)
    source["git_commit"] = _git_value(root, ["rev-parse", "HEAD"])
    dirty = _git_value(root, ["status", "--porcelain", "--untracked-files=normal", "--", "."])
    source["git_dirty"] = None if dirty is None else bool(dirty)
    prompts = _fingerprint(list((root / "app/application/prompts").glob("*.yml")), root)
    if judge_prompt is not None:
        prompts["judge_system_sha256"] = hashlib.sha256(judge_prompt.encode()).hexdigest()
    catalog = root / "catalog/catalog-v3.jsonl"
    if not catalog.is_file():
        catalog = root / "data/catalog-v3.jsonl"
    data_paths = [dataset.resolve(), catalog, *(root / "knowledge").glob("*.md"), root / "knowledge/manifest.jsonl"]
    if baseline:
        data_paths.append(baseline.resolve())
    dependencies = {}
    for package in ("agentscope", "httpx", "qdrant-client", "pyyaml"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    return {
        "schema_version": 1, "runner": runner, "started_at": datetime.now(timezone.utc).isoformat(),
        "selection": selection, "parameters": parameters, "code": source, "prompts": prompts,
        "data": _fingerprint(data_paths, root),
        "runtime": {"python": platform.python_version(), "dependencies": dependencies},
        "models": {
            "main_configured": os.getenv("LLM_MODEL", "qwen3-max"),
            "fallback_configured": os.getenv("LLM_FALLBACK_MODEL", "qwen-plus"),
            "embedding_configured": os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            "embedding_dimension_configured": os.getenv("EMBEDDING_DIM", "1024"),
            "reranker_configured": os.getenv("RERANKER_MODEL", ""),
            "judge_requested": os.getenv("EVAL_JUDGE_MODEL") or os.getenv("LLM_MODEL", "qwen-plus"),
        },
        "services_configured": {
            "llm_endpoint": public_endpoint(os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            "embedding_endpoint": public_endpoint(os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            "reranker_endpoint": public_endpoint(os.getenv("RERANKER_BASE_URL", "")),
            "qdrant_endpoint": public_endpoint(os.getenv("QDRANT_URL", "")) or "local_embedded",
            "qdrant_product_collection": os.getenv("QDRANT_COLLECTION", "shoppilot_products"),
            "qdrant_category_collection": os.getenv("CATEGORY_KB_COLLECTION", "shoppilot_category_kb"),
        },
        "execution": {"status": "NOT_RUN", "actual_strategies": [], "gate": "NOT_RUN"},
    }


def finish_manifest(manifest: dict, *, actual_strategies: Any, gate: str, status: str = "COMPLETED",
                    root: Path = PROJECT_ROOT, **evidence: Any) -> bool:
    """报告运行期间输入是否变动；变化后不允许把旧快照配上新代码宣称 PASS。"""
    code_end = worktree_fingerprint(root)["sha256"]
    paths = [root / entry["path"] for entry in manifest["data"]["files"]]
    data_end = _fingerprint(paths, root)["sha256"]
    stable = code_end == manifest["code"]["sha256"] and data_end == manifest["data"]["sha256"]
    manifest["execution"] = {
        "status": status, "gate": gate if stable else "BLOCK", "actual_strategies": actual_strategies,
        "finished_at": datetime.now(timezone.utc).isoformat(), "inputs_unchanged": stable,
        "worktree_sha256_at_end": code_end, "data_sha256_at_end": data_end, **evidence,
    }
    return stable


def write_manifest(manifest: dict, report_path: Path) -> Path:
    path = report_path.with_suffix(".manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    def serialize(value: Any):
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        if isinstance(value, Path):
            return str(value)
        raise TypeError(f"不可序列化的评测证据：{type(value).__name__}")
    path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, default=serialize, allow_nan=False) + "\n", encoding="utf-8")
    return path


def manifest_report(manifest: dict) -> str:
    selection, execution = manifest["selection"], manifest["execution"]
    report = (
        "\n\n## 执行证据\n\n"
        f"- 选集：`{selection['split']}`，{selection['selected_count']}/{selection['source_count']} 条；完整选集：{selection['complete_split']}。\n"
        f"- 选集内容 SHA-256：`{selection['selected_content_sha256']}`。\n"
        f"- 工作区内容 SHA-256：`{manifest['code']['sha256']}`（包含未提交源文件；详细范围见同名 manifest）。\n"
        f"- 执行状态：**{execution['status']}**；门禁：**{execution['gate']}**；门禁范围：`{manifest['parameters'].get('gate_scope', 'diagnostic')}`。\n"
        f"- 实际策略：`{json.dumps(execution['actual_strategies'], ensure_ascii=False)}`。\n"
        "- 模型、Prompt、数据文件、依赖版本及逐项 hash 均保存在同名 `.manifest.json`。\n"
    )
    if "service_runtime" in manifest:
        report += f"- 服务端 app 源文件指纹核对：`{manifest['service_runtime']['status']}`；该核对不证明依赖或运行数据相同。\n"
    return report
