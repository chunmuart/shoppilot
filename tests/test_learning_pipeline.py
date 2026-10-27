"""隔离 SQLite 与可控模型：覆盖回流、恢复、审批及数据泄漏边界。"""
from copy import deepcopy
import json
import sqlite3

import pytest

from app.application.learning import LearningWorker
from app.domain.learning import Reflection, digest
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.capability_registry import CapabilityRegistry, CapabilityError
from app.infrastructure.learning_publication import publish_candidate, withdraw_candidate
from app.infrastructure.learning_store import LearningStore
from scripts.eval.learning_import import import_learning_report
from scripts.eval.feedback import FeedbackStore


def evidence(**overrides):
    return {"synthetic": True, "buyer_id": "fixture-buyer", "session_id": "fixture-session",
            "source_refs": ["fixture:delivery"], "input": "出行前收到防水背包", "output": "先确认出行时间，再比较可达日期",
            "facts": [{"ref": "fixture:delivery", "value": {"delivery_days": 5}}],
            "checks": {"constraint_preserved": True}, "split": "train", "lineage_id": "delivery-family",
            "learning_allowed": True, "meaningful": True, "outcome": "success", "versions": {"fixture": "v1"}, **overrides}


def draft(**overrides):
    return {"title": "按收货期限选购", "applies_when": "买家对收货期限有明确要求时",
            "steps": ["先确认期限，再核验目的地与可达日期", "仅比较符合当前要求的候选"],
            "exceptions": ["没有期限时不额外追问；当前用户要求始终优先"],
            "evidence_refs": ["fixture:delivery"], "keywords": ["出行", "收货"], "scope": "shopping", **overrides}


class Extractor:
    def __init__(self, document=None):
        self.calls = 0
        self.document = document or draft()
        self.last_usage = []

    async def extract(self, source):
        self.calls += 1
        return Reflection(outcome="supported_success", reason="合成工具证据支持完成", strategy=self.document), [{"kind": "learning_extraction", "input_tokens": 10, "output_tokens": 5}]


def enqueue(store, data=None, ref="fixture:delivery"):
    data = data or evidence()
    return store.enqueue(source_kind="fixture", source_ref=ref, source_hash=digest(data), buyer_id=data["buyer_id"], session_id=data["session_id"], payload=data)


def checks():
    return [{"case_id": "delivery-positive", "kind": "positive", "passed": True, "evidence_hash": digest("positive")},
            {"case_id": "delivery-negative", "kind": "negative", "passed": True, "evidence_hash": digest("negative")}]


async def approved_candidate(store):
    enqueue(store)
    result = await LearningWorker(store, Extractor()).run_once()
    candidate = store.get("strategy", result["strategy_id"])
    store.validate_candidate(candidate["id"], expected_hash=candidate["content_hash"], checks=checks(), validator_version="test-v1")
    store.review("strategy", candidate["id"], expected_revision=1, expected_hash=candidate["content_hash"], accepted=True, reviewer="unit-test-human", note="测试正反例")
    return store.get("strategy", candidate["id"])


def run_body():
    return {"runId": "r1", "threadId": "s1", "messages": [{"id": "u1", "role": "user", "content": "查询背包"}], "forwardedProps": {}}


async def test_terminal_outbox_same_transaction_and_recovery(tmp_path):
    path = tmp_path/"runs.db"
    journal = AGUIJournal(path, learning_enabled=True)
    await journal.reserve(run_body(), "b1", "owner")
    store = LearningStore(path)
    assert store.summary()["learning_jobs"] == []
    # 注入 outbox 写失败，确认终态与事件也回滚，不能悄悄漏任务。
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON learning_jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await journal.append("r1", "owner", [{"type": "RUN_FINISHED"}])
    assert (await journal.run("r1", "b1"))["status"] == "running"
    assert (await journal.events("r1", "b1"))[0] == []
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER fail_outbox")
    await journal.append("r1", "owner", [{"type": "RUN_ERROR", "code": "SERVER_RESTART"}])
    resumed = LearningStore(path)
    assert len(resumed.list_records("job")) == 1
    result = await LearningWorker(resumed).run_once()
    sample = resumed.get("sample", result["sample_id"])
    assert sample["sample_type"] == "diagnostic" and sample["payload"]["target_output"] is None
    assert "查询背包" not in json.dumps(sample, ensure_ascii=False)
    assert await LearningWorker(resumed).run_once() is None


async def test_capture_off_does_not_enqueue(tmp_path):
    journal = AGUIJournal(tmp_path/"runs.db")
    await journal.reserve(run_body(), "b", "owner")
    await journal.append("r1", "owner", [{"type": "RUN_FINISHED"}])
    assert LearningStore(journal.path).list_records("job") == []


def test_duplicate_restart_lease_fence_and_exhaustion(tmp_path):
    path = tmp_path/"learning.db"; store = LearningStore(path)
    identity = enqueue(store)
    assert enqueue(store) == identity
    first = store.claim("worker1")
    assert LearningStore(path).claim("worker2") is None
    with store.transaction() as db:
        db.execute("UPDATE learning_jobs SET lease_until=0")
    second = LearningStore(path).claim("worker2")
    assert second["fence"] == first["fence"]+1
    with pytest.raises(RuntimeError, match="fence"):
        store.save_diagnostic(first, evidence(outcome="failure"))
    store.finish(second, result={"usage": [{"input_tokens": 30}]}, error=ValueError("do-not-log-secret"))
    with store.transaction() as db:
        db.execute("UPDATE learning_jobs SET available_at=0")
    third = store.claim("worker3")
    store.finish(third, result={"usage": [{"input_tokens": 40}]}, error=ValueError())
    assert store.claim("worker4") is None
    job = store.list_records("job")[0]
    assert job["status"] == "failed" and job["last_error"] == "ValueError"
    assert sum(x["usage"][0]["input_tokens"] for x in job["result"]["attempt_history"]) == 70


async def test_candidate_not_published_and_positive_negative_gate(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store)
    result = await LearningWorker(store, Extractor()).run_once()
    candidate = store.get("strategy", result["strategy_id"])
    assert candidate["status"] == "captured"
    with pytest.raises(ValueError, match="验证通过"):
        store.review("strategy", candidate["id"], expected_revision=1, expected_hash=candidate["content_hash"], accepted=True, reviewer="human", note="review")
    with pytest.raises(ValueError, match="不适用"):
        store.validate_candidate(candidate["id"], expected_hash=candidate["content_hash"], checks=checks()[:1], validator_version="v1")
    registry = CapabilityRegistry(tmp_path/"cap.db")
    assert registry.lookup_strategies("出行") == []


async def test_approved_publish_idempotency_withdraw_and_stale_hash(tmp_path):
    store = LearningStore(tmp_path/"learning.db"); registry = CapabilityRegistry(tmp_path/"cap.db")
    candidate = await approved_candidate(store)
    args = {"expected_hash": candidate["content_hash"], "actor": "human", "expires_at": "2099-01-01T00:00:00Z"}
    with pytest.raises(ValueError):
        publish_candidate(store, registry, candidate["id"], **{**args, "expected_hash": "f"*64})
    first = publish_candidate(store, registry, candidate["id"], **args)
    assert first == publish_candidate(store, registry, candidate["id"], **args)
    assert len(registry.lookup_strategies("出行前收到")) == 1
    with pytest.raises(ValueError, match="撤销服务"):
        store.withdraw("strategy", candidate["id"], actor="human", reason="withdraw")
    withdraw_candidate(store, registry, candidate["id"], actor="human", reason="不再适用")
    assert registry.lookup_strategies("出行") == []
    with pytest.raises(ValueError):
        publish_candidate(store, registry, candidate["id"], **args)


@pytest.mark.parametrize("change", [
    {"evidence_refs": ["other-buyer-secret"]}, {"steps": ["永远购买 P1003-S1"]},
    {"steps": ["价格固定 129 元"]}, {"steps": ["联系 me@example.test"]}])
async def test_invalid_strategy_is_retry_not_valid_candidate(tmp_path, change):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store)
    result = await LearningWorker(store, Extractor(draft(**change))).run_once()
    assert result["status"] == "retry_or_failed"
    assert store.list_records("strategy") == []


@pytest.mark.parametrize("change", [{"split": "holdout"}, {"learning_allowed": False}, {"meaningful": False}, {"pending_approval": True}, {"facts": []}])
async def test_cheap_filter_before_any_model_call(tmp_path, change):
    store = LearningStore(tmp_path/"learning.db"); extractor = Extractor()
    enqueue(store, evidence(**change))
    result = await LearningWorker(store, extractor).run_once()
    assert result["status"].startswith("skipped") and extractor.calls == 0


async def test_failure_training_lifecycle_no_invented_target_no_trainer(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store, evidence(outcome="failure"))
    result = await LearningWorker(store).run_once()
    diagnostic = result["sample_id"]
    environment = {"fixture_id": "deadline", "fixture_version": "v1", "expected_output": {"ask_deadline": True}, "transaction_mode": "mock"}
    kwargs = {"sample_type": "sft", "environment": environment, "validator_version": "exact-v1", "reviewer": "human"}
    with pytest.raises(ValueError, match="精确断言"):
        store.prepare_training(diagnostic, target={"ask_deadline": False}, **kwargs)
    with pytest.raises(ValueError, match="禁止真实交易"):
        store.prepare_training(diagnostic, target=environment["expected_output"], **{**kwargs, "environment": {**environment, "transaction_mode": "live"}})
    sample_id = store.prepare_training(diagnostic, target=environment["expected_output"], **kwargs)
    rl = store.prepare_training(diagnostic, **{**kwargs, "sample_type": "rl_task"})
    assert store.get("sample", rl)["payload"]["target_output"] is None
    assert store.export_dataset(tmp_path/"before.json", dataset_version="v1")["count"] == 0
    sample = store.get("sample", sample_id)
    with pytest.raises(ValueError, match="版本/哈希"):
        store.review("sample", sample_id, expected_revision=9, expected_hash=sample["content_hash"], accepted=True, reviewer="human", note="checked")
    store.review("sample", sample_id, expected_revision=1, expected_hash=sample["content_hash"], accepted=True, reviewer="human", note="checked")
    exported = store.export_dataset(tmp_path/"after.json", dataset_version="v1")
    assert exported["count"] == 1 and exported["trainer_started"] is False
    store.withdraw("sample", sample_id, actor="human", reason="撤回")
    assert store.export_dataset(tmp_path/"withdrawn.json", dataset_version="v2")["count"] == 0


async def test_holdout_diagnostic_never_enters_training_and_lineage_cannot_cross_split(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store, evidence(outcome="failure", split="holdout"))
    result = await LearningWorker(store).run_once()
    row = store.get("sample", result["sample_id"])
    assert row["learning_allowed"] == 0
    enqueue(store, evidence(outcome="failure", split="train", lineage_id="renamed", input=" 出行前 收到防水背包 "), ref="different")
    assert (await LearningWorker(store).run_once())["status"] == "retry_or_failed"
    assert len(store.list_records("sample")) == 1


async def test_source_owner_hash_and_missing_events_rejected(tmp_path):
    path = tmp_path/"runs.db"; journal = AGUIJournal(path, learning_enabled=True)
    await journal.reserve(run_body(), "b1", "owner")
    await journal.append("r1", "owner", [{"type": "RUN_ERROR"}])
    store = LearningStore(path); job = store.claim("w")
    with pytest.raises(PermissionError):
        store.evidence({**job, "buyer_id": "b2"})
    with store.transaction() as db:
        db.execute("DELETE FROM agui_events")
    with pytest.raises(ValueError, match="不完整"):
        store.evidence(job)


def harness_report(tmp_path):
    manifest = {"schema_version": "harness-eval-v1", "status": "completed", "profile": "release", "run_id": "eval1"}
    rows = [{"case_id": "c1", "strategy": "current", "repetition": 0, "passed": False, "checks": {"sku": False}, "split": "dev", "answer": "错误样例"}]
    path = tmp_path/"manifest.json"; path.write_text(json.dumps(manifest)); path.with_name("results.json").write_text(json.dumps(rows))
    return path


async def test_new_harness_import_not_silently_empty_and_idempotent(tmp_path):
    path = harness_report(tmp_path)
    feedback = FeedbackStore(tmp_path/"feedback.db")
    try:
        result = feedback.import_manifest(path)
        assert result == {"queued_scores": 0, "badcases": 1, "unlinked_scores": 1}
    finally:
        feedback.close()
    store = LearningStore(tmp_path/"learning.db")
    assert import_learning_report(store, path)["jobs"] == 1
    assert import_learning_report(store, path)["jobs"] == 1
    assert len(store.list_records("job")) == 1
    result = await LearningWorker(store).run_once()
    sample = store.get("sample", result["sample_id"])
    assert sample["split"] == "holdout" and sample["learning_allowed"] == 0


def test_unknown_manifest_rejected(tmp_path):
    path = tmp_path/"manifest.json"; path.write_text('{"unexpected":true}')
    store = FeedbackStore(tmp_path/"feedback.db")
    try:
        with pytest.raises(ValueError, match="不支持"):
            store.import_manifest(path)
    finally:
        store.close()


def test_exposure_not_adoption_and_feedback_owner(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    args = dict(run_id="r1", buyer_id="b1", session_id="s1", strategies=[{"id": "strategy", "version": "v1", "content_hash": "a"*64}])
    store.record_exposure(**args); store.record_exposure(**args)
    row = store.list_records("feedback")[0]
    assert row["exposure"] == 2 and row["adopted"] is None and row["outcome"] is None
    with pytest.raises(PermissionError):
        store.record_adoption(row["id"], buyer_id="b2", session_id="s1", adopted=True, outcome="success", evidence_ref="fixture")
    store.record_adoption(row["id"], buyer_id="b1", session_id="s1", adopted=True, outcome="unknown", evidence_ref="fixture-action")
    assert store.list_records("feedback")[0]["outcome"] == "unknown"


async def test_runtime_success_is_screened_and_only_current_turn_is_sent(tmp_path):
    journal = AGUIJournal(tmp_path/"runs.db", learning_enabled=True)
    await journal.reserve(run_body(), "b1", "owner")
    await journal.append("r1", "owner", [
        {"type": "TOOL_CALL_START", "toolCallId": "t1"}, {"type": "TOOL_CALL_END", "toolCallId": "t1"},
        {"type": "TOOL_CALL_START", "toolCallId": "t2"}, {"type": "TOOL_CALL_END", "toolCallId": "t2"},
        {"type": "STATE_SNAPSHOT", "snapshot": {"products": [{"product_id": "P1003", "title": "背包", "private_address": "不发给提炼模型"}]}},
        {"type": "TEXT_MESSAGE_START", "messageId": "a1"}, {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "已按条件核验候选"},
        {"type": "TEXT_MESSAGE_END", "messageId": "a1"}, {"type": "RUN_FINISHED"}])
    store = LearningStore(journal.path)
    source = store.evidence(store.claim("worker"))
    assert source["outcome"] == "unverified" and source["meaningful"] is True
    assert source["input"] == "查询背包" and "private_address" not in str(source["facts"])


async def test_same_procedure_deduplicates_across_evidence_references(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store)
    first = await LearningWorker(store, Extractor()).run_once()
    enqueue(store, evidence(facts=[{"ref": "fixture:other", "value": "same behavior"}]), ref="fixture:other")
    second = await LearningWorker(store, Extractor(draft(evidence_refs=["fixture:other"]))).run_once()
    assert first["strategy_id"] == second["strategy_id"]
    assert len(store.list_records("strategy")) == 1


async def test_registry_base_cas_does_not_overwrite_newer_strategy(tmp_path):
    store = LearningStore(tmp_path/"learning.db"); registry = CapabilityRegistry(tmp_path/"cap.db")
    candidate = await approved_candidate(store)
    original = publish_candidate(store, registry, candidate["id"], expected_hash=candidate["content_hash"], actor="human", expires_at="2099-01-01T00:00:00Z")
    doc = registry.lookup_strategies("出行")[0]
    doc = {k: v for k, v in doc.items() if k not in {"content_hash", "reviewer", "authority"}}
    base = {k: original[k] for k in ("id", "version", "content_hash")}
    newer = {**doc, "version": "v2", "body": doc["body"]+"\n增加限制"}
    registry.publish_learned(newer, base=base, actor="human", evidence="positive-negative-reviewed")
    with pytest.raises(CapabilityError, match="基准版本"):
        registry.publish_learned({**doc, "version": "v3"}, base=base, actor="human", evidence="stale")


async def test_tampered_training_payload_cannot_export(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store, evidence(outcome="failure")); result = await LearningWorker(store).run_once()
    with store.transaction() as db:
        db.execute("UPDATE training_samples SET payload_json='{}' WHERE id=?", (result["sample_id"],))
    with pytest.raises(ValueError, match="哈希"):
        store.get("sample", result["sample_id"])


def test_learning_report_pairing_averages_repetitions_within_scene():
    from scripts.eval.run_learning import aggregate, check_calls
    rows = [{"case_id": case, "arm": arm, "passed": arm == "candidate", "usage": [{"input_tokens": 10}], "elapsed_ms": 2, "tool_calls": []}
            for case in ("a", "b") for arm in ("baseline", "candidate") for _ in range(3)]
    summary = aggregate(rows, [{"input_tokens": 7}])
    assert summary["success_difference_by_case"] == 1 and summary["paired_scene_ci95"] == [1, 1]
    assert summary["candidate"]["input_tokens"] == 60 and summary["extraction_input_tokens"] == 7
    assert summary["benefit_confirmed"] is False
    assert aggregate(rows, [], 0)["treatment_gate"] == "NO_TREATMENT_EXPOSURE"
    assert check_calls({"expected": {"sku_id": "P1003-S2"}}, [{"tool": "product_search_tool", "args": {"sku_id": "P1003-S1"}}])


async def test_successful_holdout_cannot_be_reimported_for_extraction(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store, evidence(split="holdout"))
    await LearningWorker(store, Extractor()).run_once()
    enqueue(store, evidence(split="train"), ref="renamed-input")
    extractor = Extractor()
    result = await LearningWorker(store, extractor).run_once()
    assert result["status"] == "skipped_partition_conflict" and extractor.calls == 0
    assert store.list_records("strategy") == []


async def test_derived_sample_preserves_source_ids_and_ancestor_withdrawal(tmp_path):
    store = LearningStore(tmp_path/"learning.db")
    enqueue(store, evidence(outcome="failure", run_id="actual-source-run", case_id="actual-source-case"))
    result = await LearningWorker(store).run_once()
    fixture = {"fixture_id": "stock", "fixture_version": "v1", "expected_output": {"ok": False}, "transaction_mode": "mock"}
    identity = store.prepare_training(result["sample_id"], sample_type="sft", target={"ok": False}, environment=fixture, validator_version="v1", reviewer="test-human")
    sample = store.get("sample", identity)
    assert sample["payload"]["source_run_id"] == "actual-source-run"
    assert sample["payload"]["source_case_id"] == "actual-source-case"
    store.review("sample", identity, expected_revision=1, expected_hash=sample["content_hash"], accepted=True, reviewer="test-human", note="fixture checked")
    store.withdraw("sample", result["sample_id"], actor="test-human", reason="source invalid")
    assert store.get("sample", identity)["status"] == "withdrawn"
    assert store.export_dataset(tmp_path/"after.json", dataset_version="v2")["count"] == 0


def test_manual_retry_requires_source_hash_and_retains_history(tmp_path):
    store = LearningStore(tmp_path/"learning.db"); identity = enqueue(store)
    for index in range(3):
        job = store.claim("worker")
        store.finish(job, result={"usage": [{"input_tokens": 1}]}, error=ValueError())
        with store.transaction() as db:
            db.execute("UPDATE learning_jobs SET available_at=0")
    with pytest.raises(ValueError, match="来源哈希"):
        store.retry_job(identity, expected_source_hash="wrong", actor="human", reason="service restored")
    store.retry_job(identity, expected_source_hash=job["source_hash"], actor="human", reason="service restored")
    renewed = store.claim("worker2")
    assert renewed["fence"] > job["fence"]
    store.finish(renewed, result={"usage": [{"input_tokens": 2}]})
    row = store.list_records("job")[0]
    assert len(row["result"]["attempt_history"]) == 4
    assert row["result"]["manual_retries"][0]["actor"] == "human"
