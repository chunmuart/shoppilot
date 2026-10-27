"""Prompt 生命周期测试全部使用临时 SQLite 和测试证据，不声称真实候选已评测发布。"""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.application.prompts.loader import load_prompts
from app.infrastructure.cache.semantic_cache import SemanticCache
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.prompt_registry import PromptRegistry, PromptRegistryError
from app.infrastructure.prompt_release import compare_release_manifests
from app.infrastructure.tracing import current_correlation


@pytest.fixture
def env(tmp_path):
    contract = {"tools": {"product_search_tool": {"readonly": True}}, "schema": 1}
    path = tmp_path / "registry.sqlite3"
    registry = PromptRegistry(path, contract)
    prompt = {"main_agent": {"name": "main", "system_prompt": "基线正文"},
        "sub_agents": {"search": {"name": "search", "system_prompt": "检索正文"},
                       "trade": {"name": "trade", "system_prompt": "交易正文"}}}
    baseline_path, candidate_path = tmp_path / "baseline.yml", tmp_path / "candidate.yml"
    baseline_path.write_text(yaml.safe_dump(prompt, allow_unicode=True))
    initial = registry.bootstrap(baseline_path)
    baseline = initial["effective_version"]["version_id"]
    prompt["main_agent"]["system_prompt"] = "候选正文"
    candidate_path.write_text(yaml.safe_dump(prompt, allow_unicode=True))
    candidate = registry.import_version(candidate_path)["version_id"]
    return SimpleNamespace(registry=registry, path=path, contract=contract, initial=initial,
        baseline=baseline, candidate=candidate, baseline_path=baseline_path, candidate_path=candidate_path, tmp=tmp_path)


def report(env, version, **metric_changes):
    metrics = {"task_success_rate": 1.0, "hard_constraint_pass_rate": 1.0, "latency_p95_ms": 1000,
               "input_tokens": 1000, "output_tokens": 100, "usage_complete": True, **metric_changes}
    return {"runner": "agent_regression", "selection": {"split": "release", "complete_split": True, "only": None,
        "selected_count": 1, "split_count": 1, "case_ids": ["c1"], "selected_content_sha256": "a" * 64},
        "parameters": {"gate_scope": "release", "dry_run": False}, "data": {"sha256": "b" * 64}, "code": {"sha256": "c" * 64},
        "models": {"main_configured": "test-model"}, "service_runtime": {"status": "matched", "matching": True, "local_app_source_sha256": "c" * 64, "server_app_source_sha256": "c" * 64},
        "server_health": {"semantic_cache": False, "prompt_registry": {"effective_version": {
            "version_id": version, "content_sha256": version[2:], "toolset_sha256": env.registry.contract_hash}}},
        "execution": {"gate": "PASS", "status": "COMPLETED", "inputs_unchanged": True,
            "actual_strategies": {"vector_rerank": 1}, "release_metrics": metrics,
            "results": [{"id": "c1", "p0_pass": True, "verdict": "PASS", "metrics": {"elapsed_ms": 1000, "input_tokens": 1000, "output_tokens": 100, "usage_complete": True, "latency_scope": "agent_dialogue_and_http_actions_excluding_judge"}}]}}


def reports(env, candidate_changes=None):
    before, after = env.tmp / "baseline.manifest.json", env.tmp / "candidate.manifest.json"
    baseline, candidate = report(env, env.baseline), report(env, env.candidate)
    if candidate_changes:
        candidate_changes(candidate)
    before.write_text(json.dumps(baseline))
    after.write_text(json.dumps(candidate))
    return before, after


def publish(env, bps=5000):
    before, after = reports(env)
    return env.registry.publish(baseline=env.baseline, candidate=env.candidate, candidate_bps=bps,
        experiment="stable-buyer-experiment", baseline_manifest=before, candidate_manifest=after)


def test_bootstrap_never_auto_publishes_yaml_edits_and_versions_are_immutable(env):
    assert env.registry.bootstrap(env.candidate_path)["effective_version"]["version_id"] == env.baseline
    assert env.registry.import_version(env.candidate_path)["version_id"] == env.candidate
    with sqlite3.connect(env.path) as connection:
        assert connection.execute("SELECT count(*) FROM prompt_versions").fetchone()[0] == 2
    changed = PromptRegistry(env.path, {"tools": {"other": {}}, "schema": 1})
    assert changed.import_version(env.baseline_path)["version_id"] != env.baseline
    with pytest.raises(PromptRegistryError, match="工具契约"):
        changed.describe()


async def test_assignments_are_atomic_persistent_and_stable_per_buyer(env):
    publish(env)
    instances = [PromptRegistry(env.path, env.contract) for _ in range(2)]
    assigned = await asyncio.gather(*(instances[i % 2].assign("same-session", "buyer") for i in range(10)))
    assert len({row["version_id"] for row in assigned}) == 1
    for i in range(5):
        assert (await instances[i % 2].assign(f"new-session-{i}", "buyer"))["version_id"] == assigned[0]["version_id"]
    different_buyers = await asyncio.gather(*(env.registry.assign(f"buyer-session-{i}", f"buyer-{i}") for i in range(100)))
    assert {row["variant"] for row in different_buyers} == {"baseline", "candidate"}
    with pytest.raises(PromptRegistryError, match="其他买家"):
        await instances[1].assign("same-session", "intruder")


async def test_rollback_only_affects_new_sessions_and_revoke_fails_closed(env):
    publish(env, bps=10000)
    assert (await env.registry.assign("old", "buyer"))["version_id"] == env.candidate
    env.registry.rollback(env.initial["deployment_id"])
    assert (await env.registry.assign("old", "buyer"))["version_id"] == env.candidate
    assert (await env.registry.assign("new", "buyer"))["version_id"] == env.baseline
    env.registry.revoke(env.candidate, "测试撤销")
    with pytest.raises(PromptRegistryError, match="紧急撤销"):
        await env.registry.assign("old", "buyer")


async def test_evaluation_pin_does_not_silently_switch_existing_session(env):
    pinned = PromptRegistry(env.path, env.contract, pinned_version=env.candidate)
    assert (await pinned.assign("evaluation", "buyer"))["version_id"] == env.candidate
    assert pinned.describe()["effective_version"]["version_id"] == env.candidate
    await env.registry.assign("existing", "buyer")
    with pytest.raises(PromptRegistryError, match="旧会话"):
        await pinned.assign("existing", "buyer")


async def test_current_version_drives_loader_cache_key_and_event_correlation(env):
    cache = SemanticCache(SimpleNamespace(enabled=True), None, namespace="same-model")
    first = await env.registry.assign("baseline-session", "buyer")
    second = await PromptRegistry(env.path, env.contract, pinned_version=env.candidate).assign("candidate-session", "buyer")
    token = ShoppingContext.set(ShoppingContextSnapshot("session", "buyer", "zh-CN", "CNY", ("plastic",), 42))
    try:
        ShoppingContext.set_prompt_assignment(first)
        baseline_key = cache._bucket_key("buyer", "same-preference")
        doc = load_prompts()
        assert doc["main_agent"]["system_prompt"] == "基线正文"
        doc["main_agent"]["system_prompt"] = "调用方改动"
        assert load_prompts()["main_agent"]["system_prompt"] == "基线正文"
        ShoppingContext.set_prompt_assignment(second)
        assert load_prompts()["main_agent"]["system_prompt"] == "候选正文"
        assert cache._bucket_key("buyer", "same-preference") != baseline_key
        assert ShoppingContext.current().excluded_material_tags == ("plastic",)
        assert ShoppingContext.current().session_fence == 42
        assert current_correlation()["prompt_version"] == env.candidate
    finally:
        ShoppingContext.reset(token)


@pytest.mark.parametrize("mutate", [
    lambda m: m["selection"].update(complete_split=False),
    lambda m: m["selection"].update(split="dev"),
    lambda m: m["parameters"].update(dry_run=True),
    lambda m: m["execution"].update(gate="BLOCK"),
    lambda m: m["execution"].update(inputs_unchanged=False),
    lambda m: m["execution"]["results"][0].update(p0_pass=False),
    lambda m: m["execution"]["results"][0].update(verdict="ERROR"),
    lambda m: m["execution"].update(results=[]),
    lambda m: m["server_health"].update(semantic_cache=True),
    lambda m: m["server_health"].pop("prompt_registry"),
    lambda m: m["service_runtime"].update(status="UNKNOWN"),
    lambda m: m["execution"].pop("release_metrics"),
    lambda m: m["execution"]["release_metrics"].update(input_tokens=None),
    lambda m: m["execution"]["release_metrics"].update(input_tokens=True),
    lambda m: m["execution"]["release_metrics"].update(input_tokens=float("nan")),
    lambda m: m["execution"]["release_metrics"].update(latency_p95_ms=1300),
    lambda m: m["execution"]["release_metrics"].update(input_tokens=1500),
    lambda m: m["execution"]["release_metrics"].update(hard_constraint_pass_rate=0.5),
    lambda m: m["selection"].update(selected_content_sha256="different"),
    lambda m: m["models"].update(main_configured="other"),
    lambda m: m["execution"].update(actual_strategies={"embedding_only": 1}),
])
def test_missing_or_regressed_evidence_blocks_publish_without_changing_active_deployment(env, mutate):
    before, after = reports(env, mutate)
    with pytest.raises(PromptRegistryError):
        env.registry.publish(baseline=env.baseline, candidate=env.candidate, candidate_bps=5000,
            experiment="ab", baseline_manifest=before, candidate_manifest=after)
    assert env.registry.describe()["deployment_id"] == env.initial["deployment_id"]


def test_passed_pair_records_evidence_hashes_and_does_not_store_raw_transcripts(env):
    published = publish(env)
    assert published["evidence"]["gate"] == "PASS"
    assert len(published["evidence"]["candidate"]["manifest_sha256"]) == 64
    assert "results" not in published["evidence"]["candidate"]
    assert published["evidence"]["policy"]["cost_unit"].endswith("not_currency")


async def test_corrupted_immutable_payload_cannot_load(env):
    with sqlite3.connect(env.path) as connection:
        connection.execute("UPDATE prompt_versions SET payload='{}' WHERE version_id=?", (env.baseline,))
    with pytest.raises(PromptRegistryError, match="hash"):
        await env.registry.assign("s", "buyer")


@pytest.mark.parametrize("field,value", [("latency_p95_ms", 1300), ("input_tokens", 1400)])
def test_real_case_metrics_regression_is_blocked_even_when_summary_matches(env, field, value):
    def mutate(manifest):
        manifest["execution"]["release_metrics"][field] = value
        case_key = "elapsed_ms" if field == "latency_p95_ms" else field
        manifest["execution"]["results"][0]["metrics"][case_key] = value
    before, after = reports(env, mutate)
    with pytest.raises(PromptRegistryError, match="退化"):
        compare_release_manifests(before, after, env.baseline, env.candidate)


async def test_session_registry_build_uses_pinned_prompt_after_publish(env, tmp_path):
    from agentscope.state import AgentState
    from app.application.agents.main_agent import SessionRegistry
    from app.infrastructure.persistence.json_file_stores import JsonFileSessionStore
    class Factory:
        def build(self, restored):
            return SimpleNamespace(state=restored or AgentState(session_id="native"),
                prompt=load_prompts()["main_agent"]["system_prompt"])
    store = JsonFileSessionStore(tmp_path / "sessions")
    registry = SessionRegistry(Factory(), store, prompt_registry=env.registry)
    token = ShoppingContext.set(ShoppingContextSnapshot("conversation", "buyer", "zh-CN", "CNY"))
    try:
        first = await registry.get_or_create("conversation")
        assert first.prompt == "基线正文"
        await registry.persist("conversation")
        publish(env, 10000)
        await registry.invalidate("conversation")
        restored = await registry.get_or_create("conversation")
        assert restored.prompt == "基线正文"
        assert ShoppingContext.current().prompt_version == env.baseline
    finally:
        ShoppingContext.reset(token)
        await store.close()


async def test_concurrent_first_bootstrap_has_one_initial_deployment(env):
    fresh = env.tmp / "fresh.sqlite3"
    registries = [PromptRegistry(fresh, env.contract) for _ in range(4)]
    results = await asyncio.gather(*(asyncio.to_thread(registry.bootstrap, env.baseline_path) for registry in registries))
    assert len({row["deployment_id"] for row in results}) == 1
    with sqlite3.connect(fresh) as connection:
        assert connection.execute("SELECT count(*) FROM prompt_deployments").fetchone()[0] == 1


def test_tool_upgrade_rebases_unchanged_baseline_only_after_new_pair_passes(env):
    upgraded = PromptRegistry(env.path, {"schema": 2, "tools": {"product_search_tool": {"readonly": True}}})
    new_baseline = upgraded.import_version(env.baseline_path)["version_id"]
    new_candidate = upgraded.import_version(env.candidate_path)["version_id"]
    upgraded_env = SimpleNamespace(**{**vars(env), "registry": upgraded, "baseline": new_baseline, "candidate": new_candidate})
    result = publish(upgraded_env, 10000)
    assert result["evidence"]["baseline"]["version_id"] == new_baseline
    assert upgraded.describe()["effective_version"]["version_id"] == new_candidate


def test_capability_permission_contract_participates_in_toolset_fingerprint(tmp_path):
    from app.infrastructure.prompt_registry import toolset_contract, canonical, sha256
    module = tmp_path / "app/infrastructure/capability_registry.py"
    module.parent.mkdir(parents=True)
    module.write_text('PERMISSIONS = ("read_published",)\n')
    first = sha256(canonical(toolset_contract(tmp_path)))
    module.write_text('PERMISSIONS = ("read_published", "read_expired")\n')
    assert sha256(canonical(toolset_contract(tmp_path))) != first
