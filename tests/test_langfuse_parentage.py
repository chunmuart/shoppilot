"""远端 Trace 验收必须核对上下文祖先，不能只数同一 Trace 中的组件。"""
from __future__ import annotations

import pytest

from scripts.verify_langfuse import audit_trace


TRACE = "a" * 32
PROJECT = "parentage-test"
DIRECT = {"api", "agent", "model", "tool"}
QUEUED = DIRECT | {"worker"}


def observation(identifier: str, parent: str | None, component: str) -> dict:
    kind = {"agent": "AGENT", "model": "GENERATION", "tool": "TOOL"}.get(component, "SPAN")
    name = {"api": "POST /commerce/intents", "worker": "commerce.intent.consume"}.get(component, identifier)
    return {"id": identifier, "parentObservationId": parent, "type": kind, "name": name,
            "traceId": TRACE, "projectId": PROJECT, "level": "DEFAULT",
            "startTime": "2026-09-09T00:00:00Z", "endTime": "2026-09-09T00:00:01Z",
            "inputUsage": 10 if component == "model" else None,
            "outputUsage": 2 if component == "model" else None}


def audit(rows: list[dict], required: set[str] = QUEUED) -> dict:
    return audit_trace(rows, TRACE, PROJECT, required)


def test_flattened_components_are_not_a_valid_parent_chain():
    rows = [observation("api", None, "api")]
    rows.extend(observation(component, "api", component) for component in ("worker", "agent", "model", "tool"))
    report = audit(rows)
    assert report["status"] == "INCOMPLETE"
    assert set(report["issues"]) == {
        "agent_worker_ancestor_missing", "model_agent_ancestor_missing", "tool_agent_ancestor_missing"}


def test_wrappers_and_nested_agents_preserve_valid_ancestry():
    rows = [observation(*item) for item in [
        ("api", None, "api"), ("dispatch", "api", "other"),
        ("worker", "dispatch", "worker"), ("run", "worker", "other"),
        ("main", "run", "agent"), ("delegate", "main", "tool"),
        ("search", "delegate", "agent"), ("llm-wrapper", "search", "other"),
        ("model", "llm-wrapper", "model"), ("tool-wrapper", "search", "other"),
        ("tool", "tool-wrapper", "tool"),
    ]]
    # 远端分页返回顺序不影响祖先判断。
    report = audit(list(reversed(rows)))
    assert report["status"] == "VERIFIED" and report["issues"] == []


def test_direct_api_run_does_not_require_worker():
    rows = [observation(*item) for item in [
        ("api", None, "api"), ("wrapper", "api", "other"),
        ("agent", "wrapper", "agent"), ("model", "agent", "model"),
        ("tool", "agent", "tool"),
    ]]
    assert audit(rows, DIRECT)["status"] == "VERIFIED"


@pytest.mark.parametrize("component", ["model", "tool"])
def test_each_model_and_tool_must_belong_to_an_agent(component):
    rows = [observation(*item) for item in [
        ("api", None, "api"), ("worker", "api", "worker"),
        ("agent", "worker", "agent"), ("model", "agent", "model"),
        ("tool", "agent", "tool"), ("orphan", "worker", component),
    ]]
    report = audit(rows)
    assert report["status"] == "INCOMPLETE"
    assert report["issues"] == [f"{component}_agent_ancestor_missing"]


def test_worker_must_belong_to_api_even_when_api_not_explicitly_required():
    rows = [observation(*item) for item in [
        ("root", None, "other"), ("api", "root", "api"),
        ("worker", "root", "worker"), ("agent", "worker", "agent"),
        ("model", "agent", "model"), ("tool", "agent", "tool"),
    ]]
    report = audit(rows, {"worker", "agent", "model", "tool"})
    assert report["status"] == "INCOMPLETE"
    assert report["issues"] == ["worker_api_ancestor_missing"]


def test_agent_must_belong_to_api_when_api_required():
    rows = [observation(*item) for item in [
        ("root", None, "other"), ("api", "root", "api"),
        ("agent", "root", "agent"), ("model", "agent", "model"),
        ("tool", "agent", "tool"),
    ]]
    report = audit(rows, DIRECT)
    assert report["status"] == "INCOMPLETE"
    assert report["issues"] == ["agent_api_ancestor_missing"]


def test_cyclic_parentage_is_bounded_and_never_verified():
    rows = [observation(*item) for item in [
        ("api", None, "api"), ("worker", "agent", "worker"),
        ("agent", "worker", "agent"), ("model", "agent", "model"),
        ("tool", "agent", "tool"),
    ]]
    report = audit(rows)
    assert report["status"] == "INCOMPLETE"
    assert "observation_parent_cycle" in report["issues"]
