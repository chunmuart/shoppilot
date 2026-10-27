"""与 AG-UI 日志共库的学习发件箱、候选与训练数据；不包含训练器。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import time
import uuid

from app.domain.learning import PROCESSOR_VERSION, canonical, digest, input_fingerprint, validate_checks, validate_strategy

LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_jobs (
 id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
 buyer_id TEXT NOT NULL, session_id TEXT NOT NULL, source_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
 owner TEXT, lease_until REAL NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 last_error TEXT, result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS learning_pending ON learning_jobs(status,available_at,lease_until);
CREATE TABLE IF NOT EXISTS strategy_candidates (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 document_json TEXT NOT NULL, base_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'captured', validation_json TEXT,
 reviewer TEXT, review_note TEXT, reviewed_hash TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(job_id,content_hash));
CREATE TABLE IF NOT EXISTS training_samples (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, sample_type TEXT NOT NULL,
 lineage_id TEXT NOT NULL, input_fingerprint TEXT NOT NULL, split TEXT NOT NULL,
 learning_allowed INTEGER NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'captured',
 validation_json TEXT, reviewer TEXT, review_note TEXT, reviewed_hash TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(job_id,sample_type,content_hash));
CREATE INDEX IF NOT EXISTS learning_lineage ON training_samples(lineage_id,input_fingerprint,split);
CREATE TABLE IF NOT EXISTS strategy_feedback (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, buyer_id TEXT NOT NULL, session_id TEXT NOT NULL,
 strategy_id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL,
 exposure INTEGER NOT NULL DEFAULT 1, adopted INTEGER, outcome TEXT,
 evidence_ref TEXT, created_at REAL NOT NULL,
 UNIQUE(run_id,strategy_id,version,content_hash));
"""


def job_identity(source_kind, source_ref, source_hash, buyer_id, session_id):
    return digest([PROCESSOR_VERSION, source_kind, source_ref, source_hash, buyer_id, session_id])


def terminal_source(row, projection, seq, status):
    """哈希绑定终态和消息边界；任务只保存引用，不复制聊天全文。"""
    return {"run_id": row["run_id"], "buyer_id": row["buyer_id"], "session_id": row["session_id"],
            "input": json.loads(row["input_json"]), "projection": projection, "seq": seq, "status": status}


class LearningStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(LEARNING_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if row is None:
            raise LookupError("学习记录不存在")
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                result[key[:-5]] = json.loads(result.pop(key)) if row[key] else None
        if "content_hash" in result and "document" in result and digest({"draft": result["document"], "base": result["base"]}) != result["content_hash"]:
            raise ValueError("候选正文哈希不匹配")
        if "content_hash" in result and "payload" in result and digest(result["payload"]) != result["content_hash"]:
            raise ValueError("训练数据正文哈希不匹配")
        return result

    def enqueue(self, *, source_kind, source_ref, source_hash, buyer_id, session_id, payload):
        if source_kind not in {"evaluation", "fixture"} or not all((source_ref, source_hash, buyer_id, session_id)):
            raise ValueError("只有可信评测/隔离证据可显式入队，网页运行由事务发件箱入队")
        if payload.get("buyer_id") != buyer_id or payload.get("session_id") != session_id:
            raise PermissionError("学习证据的买家/会话归属不一致")
        identity = job_identity(source_kind, source_ref, source_hash, buyer_id, session_id)
        stamp = time.time()
        with self.transaction() as db:
            previous = db.execute("SELECT payload_json FROM learning_jobs WHERE id=?", (identity,)).fetchone()
            if previous and previous[0] != canonical(payload):
                raise ValueError("同一证据引用与哈希不能换内容")
            db.execute("INSERT OR IGNORE INTO learning_jobs(id,source_kind,source_ref,buyer_id,session_id,source_hash,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (identity, source_kind, source_ref, buyer_id, session_id, source_hash, canonical(payload), stamp, stamp))
        return identity

    def claim(self, owner: str, *, lease_seconds=120, max_attempts=3):
        if not owner or not 1 <= lease_seconds <= 600:
            raise ValueError("领取任务需要 owner 及有界租约")
        stamp = time.time()
        with self.transaction() as db:
            db.execute("UPDATE learning_jobs SET status='failed',last_error='lease_exhausted',updated_at=? WHERE status='running' AND lease_until<=? AND attempts>=?", (stamp, stamp, max_attempts))
            row = db.execute("SELECT * FROM learning_jobs WHERE ((status='pending' AND available_at<=?) OR (status='running' AND lease_until<=?)) AND attempts<? ORDER BY created_at,id LIMIT 1", (stamp, stamp, max_attempts)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE learning_jobs SET status='running',owner=?,fence=fence+1,attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?", (owner, stamp + lease_seconds, stamp, row["id"]))
            return self._decode(db.execute("SELECT * FROM learning_jobs WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def _lease(db, job):
        row = db.execute("SELECT * FROM learning_jobs WHERE id=?", (job["id"],)).fetchone()
        if not row or row["status"] != "running" or row["owner"] != job["owner"] or row["fence"] != job["fence"] or row["lease_until"] <= time.time():
            raise RuntimeError("学习任务租约/fence 已失效")
        return row

    def renew(self, job, lease_seconds=120):
        with self.transaction() as db:
            self._lease(db, job)
            db.execute("UPDATE learning_jobs SET lease_until=? WHERE id=?", (time.time()+lease_seconds, job["id"]))

    def retry_job(self, identity, *, expected_source_hash, actor, reason):
        if not actor.strip() or not reason.strip():
            raise ValueError("人工重试需要操作员及原因")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM learning_jobs WHERE id=?", (identity,)).fetchone()
            if not row or row["status"] != "failed" or row["source_hash"] != expected_source_hash:
                raise ValueError("只可重试来源哈希未变的失败任务")
            result = json.loads(row["result_json"] or "{}") or {}
            result.setdefault("manual_retries", []).append({"actor": actor, "reason": reason, "at": time.time()})
            db.execute("UPDATE learning_jobs SET status='pending',attempts=0,fence=fence+1,owner=NULL,lease_until=0,available_at=0,result_json=?,updated_at=? WHERE id=?", (canonical(result), time.time(), identity))

    def finish(self, job, *, result=None, error=None):
        with self.transaction() as db:
            row = self._lease(db, job)
            previous = json.loads(row["result_json"] or "{}") or {}
            result = dict(result or {})
            result["manual_retries"] = previous.get("manual_retries", [])
            result["attempt_history"] = previous.get("attempt_history", []) + [{"fence": job["fence"],
                "error_type": type(error).__name__ if error else None, "usage": result.get("usage", [])}]
            # 只保存异常类型，不写可能携带凭据或原文的上游错误正文。
            state = "done" if error is None else "failed" if row["attempts"] >= 3 else "pending"
            db.execute("UPDATE learning_jobs SET status=?,last_error=?,result_json=?,lease_until=0,available_at=?,updated_at=? WHERE id=?", (state, type(error).__name__ if error else None, canonical(result), time.time()+min(60, 2**row["attempts"]), time.time(), job["id"]))

    def evidence(self, job):
        if job["source_kind"] != "agui":
            return job["payload"]
        with self.transaction() as db:
            row = db.execute("SELECT * FROM agui_runs WHERE run_id=?", (job["source_ref"],)).fetchone()
            if not row or row["buyer_id"] != job["buyer_id"] or row["session_id"] != job["session_id"]:
                raise PermissionError("运行证据不属于该买家/会话")
            projection = json.loads(row["projection_json"])
            source = terminal_source(row, projection, row["last_seq"], row["status"])
            if digest(source) != job["source_hash"]:
                raise ValueError("运行证据边界或内容已改变")
            events = db.execute("SELECT seq,event_json FROM agui_events WHERE run_id=? ORDER BY seq", (row["run_id"],)).fetchall()
            if [r["seq"] for r in events] != list(range(1, row["last_seq"]+1)):
                raise ValueError("运行事件证据不完整")
        state = projection.get("state", {})
        evidence = {"buyer_id": job["buyer_id"], "session_id": job["session_id"],
                "source_refs": [job["source_ref"]], "input": {"source_ref": job["source_ref"]},
                "output": {"status": row["status"]}, "facts": [], "checks": {},
                "outcome": "failure" if row["status"] != "completed" else "unverified",
                "split": "runtime", "lineage_id": job["source_ref"], "learning_allowed": True,
                "meaningful": False, "failure_type": "operational_"+row["status"], "versions": job["payload"].get("versions", {}),
                "pending_approval": bool(state.get("toolApprovals")), "privacy": "references_only"}
        if row["status"] != "completed":
            return evidence
        current = source["input"].get("messages", [])[-1:]
        previous_ids = {m.get("id") for m in source["input"].get("messages", [])}
        answers = [m for m in projection["messages"] if m.get("role") == "assistant" and m.get("id") not in previous_ids]
        query = str(current[0].get("content", "")) if current else ""
        answer = "\n".join(str(m.get("content", "")) for m in answers)
        text = query+"\n"+answer
        # 明显敏感内容直接跳过，不尝试用正则脱敏后宣称匿名化。
        if len(text) > 12000 or re.search(r"地址|电话|手机号|身份证|收件人|姓名|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{7,}\b|\b(?:sk|pk)-[\w-]+|https?://", text):
            return evidence
        facts = []
        for index, product in enumerate(state.get("products", [])[:12]):
            allowed = {key: product[key] for key in ("product_id", "sku_id", "title", "stock", "price", "currency", "material_tags", "landed_price", "skus") if key in product}
            if allowed:
                facts.append({"ref": f"agui:{job['source_ref']}#/state/products/{index}", "value": allowed, "observed_at": row["updated_at"]})
        calls = sum(json.loads(event["event_json"]).get("type") == "TOOL_CALL_START" for event in events)
        if facts and query and answer and calls >= 2:
            evidence.update(input=query, output=answer, facts=facts, meaningful=True,
                            privacy="bounded_current_turn_same_provider", outcome="unverified")
        return evidence

    @staticmethod
    def _assert_learning_partition(db, job_id, evidence):
        fingerprint = input_fingerprint(evidence.get("input"))
        lineage = evidence.get("lineage_id")
        split = evidence.get("split", "runtime")
        for row in db.execute("SELECT id,payload_json FROM learning_jobs WHERE id!=?", (job_id,)):
            other = json.loads(row["payload_json"])
            if "input" not in other:
                continue
            same = (bool(lineage) and lineage == other.get("lineage_id")) or input_fingerprint(other["input"]) == fingerprint
            if same and (other.get("split", "runtime") != split or other.get("learning_allowed") is False):
                raise ValueError("同源任务已出现在其他分区或被禁止学习，不能生成学习产物")

    def partition_allows_learning(self, job, evidence):
        with self.transaction() as db:
            self._lease(db, job)
            try:
                self._assert_learning_partition(db, job["id"], evidence)
            except ValueError:
                return False
            return True

    def save_candidate(self, job, draft, evidence, *, base=None):
        if not evidence.get("learning_allowed") or evidence.get("split") in {"holdout", "release"}:
            raise ValueError("留出证据只允许诊断，不能提炼策略")
        draft = validate_strategy(draft, evidence)
        base = base or {}
        if base and (set(base) != {"id", "version", "content_hash"} or any(not v for v in base.values())):
            raise ValueError("修订策略需要完整的基准版本和哈希")
        content_hash = digest({"draft": draft, "base": base})
        identity = digest([job["id"], content_hash])
        with self.transaction() as db:
            self._lease(db, job)
            # 完全重复的结构化策略合并为已有候选；语义冲突留给审核。
            self._assert_learning_partition(db, job["id"], evidence)
            procedure = {key: value for key, value in draft.items() if key != "evidence_refs"}
            for old in db.execute("SELECT c.id,c.document_json,c.base_json FROM strategy_candidates c JOIN learning_jobs j ON c.job_id=j.id WHERE c.status NOT IN ('rejected','withdrawn') AND (j.status='done' OR j.id=?)", (job["id"],)):
                previous = json.loads(old["document_json"])
                if {key: value for key, value in previous.items() if key != "evidence_refs"} == procedure and json.loads(old["base_json"]) == base:
                    return old["id"]
            db.execute("INSERT INTO strategy_candidates(id,job_id,content_hash,document_json,base_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (identity, job["id"], content_hash, canonical(draft), canonical(base), time.time(), time.time()))
        return identity

    def _sample(self, db, job, sample_type, payload, evidence, validation=None):
        split = evidence.get("split", "runtime")
        if split not in {"dev", "train", "val", "holdout", "release", "runtime"}:
            raise ValueError("未知数据分区")
        allowed = evidence.get("learning_allowed") is True and split not in {"holdout", "release"}
        lineage = str(evidence.get("lineage_id") or job["source_ref"])
        fingerprint = input_fingerprint(evidence.get("input"))
        old = db.execute("SELECT split FROM training_samples WHERE lineage_id=? OR input_fingerprint=?", (lineage, fingerprint)).fetchall()
        # 所有衍生样本沿用原分区；拒绝将同一需求换 ID 后从留出搬入训练。
        if any(row[0] != split for row in old):
            raise ValueError("同源或规范化重复需求不能跨数据分区")
        payload = {**payload, "source_run_id": evidence.get("run_id", evidence.get("source_run_id")), "source_case_id": evidence.get("case_id", evidence.get("source_case_id")),
                   "source_refs": evidence.get("source_refs", [job["source_ref"]]), "source_hash": job["source_hash"],
                   "versions": evidence.get("versions", {}), "processor_version": PROCESSOR_VERSION}
        content_hash = digest(payload)
        identity = digest([job["id"], sample_type, content_hash])
        db.execute("INSERT OR IGNORE INTO training_samples(id,job_id,sample_type,lineage_id,input_fingerprint,split,learning_allowed,content_hash,payload_json,status,validation_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (identity, job["id"], sample_type, lineage, fingerprint, split, int(allowed), content_hash, canonical(payload), "validated" if validation else "captured", canonical(validation), time.time(), time.time()))
        return identity

    def save_diagnostic(self, job, evidence):
        with self.transaction() as db:
            self._lease(db, job)
            return self._sample(db, job, "diagnostic", {"input": evidence.get("input"), "failed_output": evidence.get("output"), "failure_type": evidence.get("failure_type", "unverified_failure"), "checks": evidence.get("checks", {}), "target_output": None}, evidence)

    def prepare_training(self, diagnostic_id, *, sample_type, target=None, environment=None, validator_version, reviewer):
        """只用人工提供的冻结 fixture 验证修正；没有模型自动编造金标的入口。"""
        if sample_type not in {"sft", "rl_task"} or not validator_version.strip() or not reviewer.strip():
            raise ValueError("准备训练样本需要类型、验证器版本和操作员")
        environment = environment or {}
        if set(environment) != {"fixture_id", "fixture_version", "expected_output", "transaction_mode"} or environment["transaction_mode"] != "mock" or not environment["fixture_id"] or not environment["fixture_version"] or environment["expected_output"] is None:
            raise ValueError("必须提供独立冻结、禁止真实交易的验证环境")
        if sample_type == "sft" and (target is None or target != environment["expected_output"]):
            raise ValueError("修正答案未通过冻结环境的精确断言")
        if sample_type == "rl_task" and target is not None:
            raise ValueError("RL 任务不接受伪装为正确轨迹的 target")
        with self.transaction() as db:
            original = self._decode(db.execute("SELECT * FROM training_samples WHERE id=?", (diagnostic_id,)).fetchone())
            if original["sample_type"] != "diagnostic" or not original["learning_allowed"] or original["status"] == "withdrawn":
                raise ValueError("只能从允许学习的失败诊断准备训练样本")
            job = self._decode(db.execute("SELECT * FROM learning_jobs WHERE id=?", (original["job_id"],)).fetchone())
            evidence = {"split": original["split"], "learning_allowed": True, "lineage_id": original["lineage_id"], **original["payload"]}
            self._assert_learning_partition(db, job["id"], evidence)
            payload = {**original["payload"], "target_output": target if sample_type == "sft" else None, "environment": environment, "derived_from": diagnostic_id}
            validation = {"validator_version": validator_version, "fixture_hash": digest(environment), "prepared_by": reviewer, "check": "exact_output" if sample_type == "sft" else "mock_environment_contract"}
            return self._sample(db, job, sample_type, payload, evidence, validation)

    @staticmethod
    def _table(kind):
        if kind not in {"strategy", "sample"}:
            raise ValueError("未知候选类型")
        return "strategy_candidates" if kind == "strategy" else "training_samples"

    def get(self, kind, identity):
        table = self._table(kind)
        with self.transaction() as db:
            return self._decode(db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone())

    def validate_candidate(self, identity, *, expected_hash, checks, validator_version):
        validate_checks(checks, require_both=True)
        if not validator_version:
            raise ValueError("缺少验证器版本")
        with self.transaction() as db:
            changed = db.execute("UPDATE strategy_candidates SET status='validated',validation_json=?,updated_at=? WHERE id=? AND content_hash=? AND status='captured'", (canonical({"checks": checks, "validator_version": validator_version}), time.time(), identity, expected_hash)).rowcount
            if changed != 1:
                raise ValueError("候选已变化或不处于待验证状态")

    def review(self, kind, identity, *, expected_revision, expected_hash, accepted, reviewer, note):
        if type(accepted) is not bool or not reviewer.strip() or not note.strip():
            raise ValueError("审核需要明确决议、操作员和理由")
        table = self._table(kind)
        with self.transaction() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
            if not row or row["revision"] != expected_revision or row["content_hash"] != expected_hash or row["status"] not in {"captured", "validated"}:
                raise ValueError("候选版本/哈希已变化或不可重复审核")
            if accepted and (row["status"] != "validated" or (kind == "sample" and (not row["learning_allowed"] or row["sample_type"] == "diagnostic"))):
                raise ValueError("只有验证通过、允许学习的候选才能批准；诊断不能作为正例")
            source_job = db.execute("SELECT status FROM learning_jobs WHERE id=?", (row["job_id"],)).fetchone()
            if accepted and (not source_job or source_job[0] != "done"):
                raise ValueError("来源任务尚未完成，不可审核半成品")
            db.execute(f"UPDATE {table} SET status=?,reviewer=?,review_note=?,reviewed_hash=?,updated_at=? WHERE id=?", ("approved" if accepted else "rejected", reviewer, note, expected_hash, time.time(), identity))

    def withdraw(self, kind, identity, *, actor, reason):
        if not actor.strip() or not reason.strip():
            raise ValueError("撤回需要操作员及原因")
        table = self._table(kind)
        with self.transaction() as db:
            row = db.execute(f"SELECT status FROM {table} WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise LookupError("候选不存在")
            if kind == "strategy" and row["status"] == "published":
                raise ValueError("已发布策略请通过撤销服务同步撤销注册表版本")
            db.execute(f"UPDATE {table} SET status='withdrawn',review_note=COALESCE(review_note,'') || ?,updated_at=? WHERE id=?", ("\n撤回："+canonical({"actor": actor, "reason": reason}), time.time(), identity))
            if kind == "sample":
                # 原始失败证据撤回时，其修正与 RL 衍生样本也不得继续进入导出。
                for derived in db.execute("SELECT id,payload_json FROM training_samples WHERE job_id=(SELECT job_id FROM training_samples WHERE id=?)", (identity,)).fetchall():
                    if json.loads(derived["payload_json"]).get("derived_from") == identity:
                        db.execute("UPDATE training_samples SET status='withdrawn',review_note=COALESCE(review_note,'') || ?,updated_at=? WHERE id=?", ("\n来源撤回："+identity, time.time(), derived["id"]))

    def export_dataset(self, output: Path, *, dataset_version, split="train"):
        if split not in {"train", "dev", "val", "runtime"} or not dataset_version:
            raise ValueError("留出数据不可导出训练集，数据集必须有版本")
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM training_samples WHERE status='approved' AND learning_allowed=1 AND split=? AND sample_type!='diagnostic' AND reviewed_hash=content_hash ORDER BY lineage_id,id", (split,)).fetchall()
            samples = [self._decode(row) for row in rows]
            for sample in samples:
                evidence = {"input": sample["payload"].get("input"), "lineage_id": sample["lineage_id"], "split": sample["split"]}
                self._assert_learning_partition(db, sample["job_id"], evidence)
        result = {"dataset_version": dataset_version, "split": split, "samples": samples, "trainer_started": False}
        result["content_hash"] = digest(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(canonical(result)+"\n")
        return {"count": len(samples), "content_hash": result["content_hash"], "trainer_started": False}

    def record_exposure(self, *, run_id, buyer_id, session_id, strategies):
        with self.transaction() as db:
            for strategy in strategies:
                identity = digest([run_id, strategy["id"], strategy["version"], strategy["content_hash"]])
                db.execute("INSERT INTO strategy_feedback(id,run_id,buyer_id,session_id,strategy_id,version,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET exposure=exposure+1",
                           (identity, run_id, buyer_id, session_id, strategy["id"], strategy["version"], strategy["content_hash"], time.time()))

    def record_adoption(self, identity, *, buyer_id, session_id, adopted, outcome, evidence_ref):
        if type(adopted) is not bool or outcome not in {"success", "failure", "unknown"} or not evidence_ref:
            raise ValueError("采纳结论需要可核查的执行证据，不能由命中数推断")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM strategy_feedback WHERE id=?", (identity,)).fetchone()
            if not row or row["buyer_id"] != buyer_id or row["session_id"] != session_id:
                raise PermissionError("反馈归属不一致")
            if row["adopted"] is not None:
                raise ValueError("采纳审计不可覆盖")
            db.execute("UPDATE strategy_feedback SET adopted=?,outcome=?,evidence_ref=? WHERE id=?", (int(adopted), outcome, evidence_ref, identity))

    def summary(self):
        with self.transaction() as db:
            return {table: [dict(row) for row in db.execute(f"SELECT {group},count(*) AS count FROM {table} GROUP BY {group}")]
                    for table, group in (("learning_jobs", "status"), ("strategy_candidates", "status"), ("training_samples", "sample_type,status,split"))}

    def list_records(self, kind, limit=50):
        table = {"job": "learning_jobs", "strategy": "strategy_candidates", "sample": "training_samples", "feedback": "strategy_feedback"}.get(kind)
        if not table:
            raise ValueError("未知记录类型")
        with self.transaction() as db:
            return [self._decode(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?", (min(200, max(1, limit)),))]
