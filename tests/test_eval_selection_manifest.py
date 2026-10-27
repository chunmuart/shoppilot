# -*- coding: utf-8 -*-
"""验证选择发生在外部调用之前，正式指标与证据不会混入 dev。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from scripts.eval import run_category_recall as category
from scripts.eval import run_product_recall as product
from scripts import eval_regression as agent
from scripts.eval.run_manifest import (
    build_manifest, finish_manifest, public_endpoint, select_cases, validate_baseline_selection, worktree_fingerprint, write_manifest,
)


def _dataset(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def _manifest(directory: Path) -> dict:
    paths = list(directory.glob("*.manifest.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_split_selection_is_explicit_ordered_and_bound_to_content():
    rows = [{"id": "d", "split": "dev", "query": "dev"}, {"id": "r", "split": "release", "query": "release"}]
    chosen, selection = select_cases(rows, "release")
    assert chosen == [rows[1]]
    assert selection["case_ids"] == ["r"]
    assert selection["split_counts"] == {"release": 1}
    assert selection["source_count"] == 2 and selection["selected_count"] == 1
    _, changed = select_cases([rows[0], {**rows[1], "query": "new query"}], "release")
    assert changed["selected_content_sha256"] != selection["selected_content_sha256"]
    assert select_cases(rows)[0] == rows


@pytest.mark.parametrize("rows,split,only", [
    ([{"id": "old"}], "release", None),
    ([{"id": "d", "split": "dev"}], "release", None),
    ([{"id": "bad", "split": "typo"}], "all", None),
    ([{"id": "d", "split": "dev"}], "dev", "missing"),
    ([{"id": "dup"}, {"id": "dup"}], "all", None),
])
def test_invalid_or_empty_selection_does_not_fall_back_to_all(rows, split, only):
    with pytest.raises(ValueError):
        select_cases(rows, split, only)


def test_old_unlabelled_dataset_requires_all_and_only_is_never_full_selection():
    rows = [{"id": "a"}, {"id": "b"}]
    assert select_cases(rows)[1]["split_counts"] == {"unlabelled": 2}
    _, selection = select_cases(rows, "all", "a")
    assert selection["complete_split"] is False


def test_manifest_fingerprints_uncommitted_files_prompt_and_data_without_exporting_keys(tmp_path, monkeypatch):
    (tmp_path / "app/application/prompts").mkdir(parents=True)
    source = tmp_path / "app/example.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "app/application/prompts/shoppilot.yml").write_text("system: original", encoding="utf-8")
    dataset = _dataset(tmp_path / "cases.jsonl", [{"id": "r", "split": "release"}])
    selection = select_cases([{"id": "r", "split": "release"}], "release")[1]
    monkeypatch.setenv("LLM_API_KEY", "never-export-this-key")
    manifest = build_manifest(runner="test", dataset=dataset, selection=selection, parameters={}, root=tmp_path, judge_prompt="judge instruction")
    assert manifest["execution"]["status"] == "NOT_RUN"
    assert manifest["prompts"]["judge_system_sha256"]
    initial = manifest["code"]["sha256"]
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert worktree_fingerprint(tmp_path)["sha256"] != initial
    assert finish_manifest(manifest, actual_strategies=["keyword_2gram"], gate="PASS", root=tmp_path) is False
    assert manifest["execution"]["gate"] == "BLOCK"
    output = write_manifest(manifest, tmp_path / "test.md")
    assert "never-export-this-key" not in output.read_text(encoding="utf-8")


def test_release_baseline_cannot_reuse_all_split_or_unbound_data(tmp_path):
    dataset = _dataset(tmp_path / "data.jsonl", [{"id": "r", "split": "release"}])
    selection = select_cases([{"id": "r", "split": "release"}], "release")[1]
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"strategies": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="split"):
        validate_baseline_selection(baseline, selection, dataset)
    baseline.write_text(json.dumps({"split": "release"}), encoding="utf-8")
    with pytest.raises(ValueError, match="selected_content_sha256"):
        validate_baseline_selection(baseline, selection, dataset)
    baseline.write_text(json.dumps({"split": "release", "selected_content_sha256": selection["selected_content_sha256"]}), encoding="utf-8")
    validate_baseline_selection(baseline, selection, dataset)


def test_endpoint_evidence_strips_credentials_and_query_tokens():
    assert public_endpoint("https://user:password@example.test/v1?api_key=private#private") == "https://example.test/v1"


@pytest.mark.parametrize("remote,expected", [(None, "unverified"), ("invalid", "unverified"), ("a" * 64, "matched"), ("b" * 64, "mismatch")])
def test_agent_runtime_identity_does_not_treat_local_hash_as_server_attestation(remote, expected):
    evidence = agent.compare_runtime_identity("a" * 64, {"runtime": {"app_source_sha256": remote}})
    assert evidence["status"] == expected
    assert "不证明依赖" in evidence["scope"]


@pytest.mark.parametrize("runner", ["product", "category", "agent"])
async def test_dry_run_selects_release_without_initializing_any_external_dependency(tmp_path, monkeypatch, runner):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must never initialize external dependencies")

    rows = [{"id": "dev", "split": "dev"}, {"id": "release", "split": "release"}]
    report_dir = tmp_path / "reports"
    if runner == "agent":
        path = tmp_path / "cases.yaml"
        path.write_text(yaml.safe_dump({"cases": rows}), encoding="utf-8")
        monkeypatch.setattr(agent, "_guard_semantic_cache", forbidden)
        await agent.main(["--cases", str(path), "--split", "release", "--dry-run", "--report-dir", str(report_dir)])
    else:
        path = _dataset(tmp_path / "cases.jsonl", rows)
        module = product if runner == "product" else category
        monkeypatch.setattr(module, "build_usecase" if runner == "product" else "load_settings", forbidden)
        await module.main(["--dataset", str(path), "--split", "release", "--dry-run", "--report-dir", str(report_dir)])
    manifest = _manifest(report_dir)
    assert manifest["selection"]["case_ids"] == ["release"]
    assert manifest["execution"] == {"status": "NOT_RUN", "actual_strategies": [], "gate": "NOT_RUN"}
    assert not list(report_dir.glob("*.md"))


async def test_product_release_runs_only_selected_queries_and_records_real_degradation(tmp_path, monkeypatch):
    repo = InMemoryProductRepository()
    queries = []
    usecase = CatalogSearchUseCase(repo)
    original = usecase.execute

    async def execute(spec):
        queries.append(spec.normalized_query)
        return await original(spec)

    usecase.execute = execute

    async def build(_strategy):
        # 请求 embedding_rerank，但确实执行无外部依赖的关键词链，不能记成 online-main 通过。
        return usecase, repo, "embedding_rerank"

    monkeypatch.setattr(product, "build_usecase", build)
    path = _dataset(tmp_path / "cases.jsonl", [
        {"id": "dev", "split": "dev", "query": "不应执行的 dev 查询", "relevant": ["P1001"]},
        {"id": "release", "split": "release", "query": "旅行三件套", "relevant": ["P1001"]},
    ])
    with pytest.raises(SystemExit) as error:
        await product.main(["--dataset", str(path), "--split", "release", "--profile", "online-main", "--report-dir", str(tmp_path / "reports")])
    assert error.value.code == 1
    assert queries == ["旅行三件套"]
    manifest = _manifest(tmp_path / "reports")
    assert manifest["execution"]["actual_strategies"] == {"embedding_rerank": ["keyword_2gram"]}
    assert manifest["execution"]["gate"] == "BLOCK"
    assert manifest["execution"]["metrics"]["embedding_rerank"]["count"] == 1
    assert [item["case_id"] for item in manifest["execution"]["observations"]["embedding_rerank"]] == ["release"]


async def test_empty_product_case_keeps_individual_evidence_instead_of_only_an_average():
    repo = InMemoryProductRepository()
    observations = []
    aggregate = await product.run_dataset(CatalogSearchUseCase(repo), repo, [{
        "id": "negative", "query": "qxnonexistent947", "split": "release", "relevant": [], "expected_empty": True,
    }], 8, observations=observations)
    assert aggregate.count == 0 and aggregate.empty_count == 1
    assert observations[0]["case_id"] == "negative"
    assert observations[0]["empty_pass"] is True
    assert observations[0]["raw_retrieved"] == []


async def test_category_release_metrics_exclude_dev_and_initialization_errors_leave_block_evidence(tmp_path, monkeypatch):
    queries = []
    class Knowledge:
        async def search(self, queries: list[str], top_k: int):
            observed.extend(queries)
            return [SimpleNamespace(document_id="release.md", score=.9, chunk=SimpleNamespace(metadata={"source": "release.md"}))]
    observed = queries
    monkeypatch.setattr(category, "load_settings", lambda: object())
    monkeypatch.setattr(category, "build_category_knowledge_base", lambda _: Knowledge())
    async def bootstrap(_): return 0
    monkeypatch.setattr(category, "bootstrap_category_knowledge", bootstrap)
    path = _dataset(tmp_path / "cases.jsonl", [
        {"id": "d", "split": "dev", "query": "dev", "relevant": ["dev.md"]},
        {"id": "r", "split": "release", "query": "release", "relevant": ["release.md"]},
    ])
    await category.main(["--dataset", str(path), "--split", "release", "--report-dir", str(tmp_path / "ok")])
    assert queries == ["release"]
    assert _manifest(tmp_path / "ok")["execution"]["metrics"]["count"] == 1
    assert [item["case_id"] for item in _manifest(tmp_path / "ok")["execution"]["observations"]] == ["r"]
    def unavailable(_): raise RuntimeError("service unavailable")
    monkeypatch.setattr(category, "build_category_knowledge_base", unavailable)
    with pytest.raises(SystemExit):
        await category.main(["--dataset", str(path), "--split", "release", "--report-dir", str(tmp_path / "error")])
    failed = _manifest(tmp_path / "error")["execution"]
    assert failed["status"] == "ERROR" and failed["gate"] == "BLOCK"
    assert failed["actual_strategies"] == []


async def test_agent_release_dispatch_and_report_are_restricted_to_release(tmp_path, monkeypatch):
    calls = []
    async def health(*args, **kwargs): return {"semantic_cache": False, "model": "reported-model"}
    async def run_case(client, case, truth):
        calls.append(case["id"])
        return {"id": case["id"], "description": "test", "score": 1, "p0_pass": True, "verdict": "PASS", "judged": {}, "transcript": "test", "trace_events": [{"type": "tool.result", "payload": {"recall_strategy": "embedding_only"}}]}
    monkeypatch.setattr(agent, "_guard_semantic_cache", health)
    monkeypatch.setattr(agent, "run_case", run_case)
    monkeypatch.setattr(agent, "build_ground_truth", lambda: "test facts")
    path = tmp_path / "cases.yaml"
    path.write_text(yaml.safe_dump({"cases": [{"id": "d", "split": "dev"}, {"id": "r", "split": "release"}]}), encoding="utf-8")
    with pytest.raises(SystemExit) as code:
        await agent.main(["--cases", str(path), "--split", "release", "--report-dir", str(tmp_path / "reports")])
    assert code.value.code == 0
    assert calls == ["r"]
    manifest = _manifest(tmp_path / "reports")
    assert manifest["execution"]["actual_strategies"] == {"r": ["embedding_only"]}
    assert len(manifest["execution"]["results"]) == 1
    assert manifest["server_health"]["model"] == "reported-model"
