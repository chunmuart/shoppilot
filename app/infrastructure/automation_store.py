"""自动化权威记录；与 AG-UI 日志同库，短事务内领取和对账。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import time
import uuid
import aiosqlite
from app.domain.automation.models import AutomationDefinition


class AutomationConflict(ValueError):
    pass


class AutomationNotFound(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


ACTIVE = ("queued", "starting", "running", "awaiting_start", "awaiting_confirmation")
SCHEMA = """
CREATE TABLE IF NOT EXISTS automations (
 id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, definition TEXT NOT NULL,
 revision INTEGER NOT NULL, status TEXT NOT NULL, series_id TEXT NOT NULL,
 session_id TEXT NOT NULL, next_run_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 failures INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS automations_due ON automations(status,next_run_at);
CREATE TABLE IF NOT EXISTS automation_runs (
 id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
 definition TEXT NOT NULL, definition_revision INTEGER NOT NULL, scheduled_for REAL NOT NULL,
 trigger_kind TEXT NOT NULL, status TEXT NOT NULL, session_id TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, attempt_no INTEGER NOT NULL DEFAULT 0,
 error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
 catchup_from REAL, UNIQUE(automation_id,trigger_kind,scheduled_for));
CREATE INDEX IF NOT EXISTS automation_runs_status ON automation_runs(status,created_at);
CREATE INDEX IF NOT EXISTS automation_runs_session ON automation_runs(buyer_id,session_id);
CREATE TABLE IF NOT EXISTS automation_attempts (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
 agent_run_id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, fence INTEGER NOT NULL,
 lease_until REAL NOT NULL, model_json TEXT, started_at REAL, finished_at REAL,
 UNIQUE(run_id,attempt_no));
CREATE TABLE IF NOT EXISTS automation_commands (
 buyer_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(buyer_id,request_id));
CREATE TABLE IF NOT EXISTS automation_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, automation_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
 event TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS automation_workers (id TEXT PRIMARY KEY, heartbeat REAL NOT NULL);
"""


class AutomationStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA)
            await db.commit()

    @asynccontextmanager
    async def tx(self):
        async with aiosqlite.connect(self.path, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    async def one(db, sql, params=()):
        return await (await db.execute(sql, params)).fetchone()

    async def owned(self, db, table, identity, buyer):
        # table 仅由本模块固定字符串传入，不接收客户端表名。
        row = await self.one(
            db, f"SELECT * FROM {table} WHERE id=? AND buyer_id=?", (identity, buyer)
        )
        if row is None:
            raise AutomationNotFound("自动化或执行记录不存在")
        return row

    async def audit(self, db, task, event, payload):
        await db.execute(
            "INSERT INTO automation_audit(automation_id,buyer_id,event,payload,created_at) VALUES(?,?,?,?,?)",
            (task["id"], task["buyer_id"], event, encode(payload), self.clock()),
        )

    async def command(self, db, buyer, request_id, payload):
        row = await self.one(
            db,
            "SELECT * FROM automation_commands WHERE buyer_id=? AND request_id=?",
            (buyer, request_id),
        )
        if row and row["fingerprint"] != digest(payload):
            raise AutomationConflict("相同 request_id 已用于不同请求")
        return json.loads(row["result"]) if row else None

    async def remember(self, db, buyer, request_id, payload, result):
        await db.execute(
            "INSERT INTO automation_commands VALUES(?,?,?,?)",
            (buyer, request_id, digest(payload), encode(result)),
        )

    @staticmethod
    def public(row):
        value = dict(row)
        value["definition"] = json.loads(value["definition"])
        return value

    async def list(self, buyer, limit=50, offset=0):
        async with self.tx() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM automations WHERE buyer_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (buyer, limit, offset),
                )
            ).fetchall()
            count = await self.one(
                db, "SELECT count(*) n FROM automations WHERE buyer_id=?", (buyer,)
            )
            heartbeat = await self.one(
                db, "SELECT max(heartbeat) stamp FROM automation_workers"
            )
            stamp = heartbeat["stamp"]
            return {
                "items": [self.public(row) for row in rows],
                "total": count["n"],
                "worker": {
                    "online": stamp is not None and self.clock() - stamp < 20,
                    "last_seen": stamp,
                },
            }

    async def session_origins(self, buyer):
        """从实际开始执行的持久关联读取来源；归档、后续手动续聊不改变来源。"""
        async with aiosqlite.connect(self.path, timeout=10) as db:
            async with db.execute(
                "SELECT DISTINCT r.session_id FROM automation_runs r "
                "JOIN agui_sessions s ON s.session_id=r.session_id AND s.buyer_id=r.buyer_id "
                "WHERE r.buyer_id=? AND EXISTS (SELECT 1 FROM automation_attempts a "
                "WHERE a.run_id=r.id AND a.started_at IS NOT NULL)",
                (buyer,),
            ) as cursor:
                return [row[0] for row in await cursor.fetchall()]

    async def get(self, identity, buyer):
        async with self.tx() as db:
            return self.public(await self.owned(db, "automations", identity, buyer))

    async def save(
        self, buyer, definition, request_id, *, identity=None, expected_revision=None
    ):
        definition = AutomationDefinition.model_validate(definition)
        payload = {
            "op": "save",
            "id": identity,
            "revision": expected_revision,
            "definition": definition.model_dump(mode="json"),
        }
        now = self.clock()
        async with self.tx() as db:
            previous = await self.command(db, buyer, request_id, payload)
            if previous:
                return self.public(
                    await self.owned(db, "automations", previous["id"], buyer)
                )
            next_at = definition.schedule.next(now)
            if next_at is None:
                raise AutomationConflict("计划在有效期内没有未来执行时间")
            if identity:
                row = await self.owned(db, "automations", identity, buyer)
                if row["revision"] != expected_revision or row["status"] == "archived":
                    raise AutomationConflict("任务已更新或归档，请刷新")
                old = json.loads(row["definition"])
                series, session = row["series_id"], row["session_id"]
                if old["output_mode"] != definition.output_mode:
                    series, session = uuid.uuid4().hex, "auto-" + uuid.uuid4().hex
                await self.cancel_queued(db, identity, "任务设置已更新")
                await db.execute(
                    "UPDATE automations SET definition=?,revision=revision+1,series_id=?,session_id=?,next_run_at=?,updated_at=? WHERE id=?",
                    (
                        encode(payload["definition"]),
                        series,
                        session,
                        next_at,
                        now,
                        identity,
                    ),
                )
            else:
                identity = uuid.uuid4().hex
                await db.execute(
                    "INSERT INTO automations(id,buyer_id,definition,revision,status,series_id,session_id,next_run_at,created_at,updated_at) VALUES(?,?,?,1,?,?,?,?,?,?)",
                    (
                        identity,
                        buyer,
                        encode(payload["definition"]),
                        "active",
                        uuid.uuid4().hex,
                        "auto-" + uuid.uuid4().hex,
                        next_at,
                        now,
                        now,
                    ),
                )
            row = await self.owned(db, "automations", identity, buyer)
            # 每次提交表单都明确携带授权；保存版本、范围和哈希，正文不复制到审计。
            await self.audit(
                db,
                row,
                "definition_saved",
                {
                    "revision": row["revision"],
                    "hash": digest(payload["definition"]),
                    "auto_start": definition.auto_start,
                    "scope": "query_and_analyze;writes_require_confirmation",
                },
            )
            await self.remember(db, buyer, request_id, payload, {"id": identity})
            return self.public(row)

    async def cancel_queued(self, db, identity, reason):
        # 已经进入 AG-UI 的启动算正在执行，编辑或暂停不隐式停止。
        await db.execute(
            "UPDATE automation_runs SET status='cancelled',error=?,revision=revision+1,updated_at=?,finished_at=? WHERE automation_id=? AND status IN ('queued','awaiting_start','starting') AND NOT EXISTS (SELECT 1 FROM automation_attempts a JOIN agui_runs g ON g.run_id=a.agent_run_id WHERE a.run_id=automation_runs.id AND a.attempt_no=automation_runs.attempt_no AND g.status='running')",
            (reason, self.clock(), self.clock(), identity),
        )

    async def action(self, identity, buyer, action, revision, request_id):
        payload = {"op": action, "id": identity, "revision": revision}
        async with self.tx() as db:
            cached = await self.command(db, buyer, request_id, payload)
            row = await self.owned(db, "automations", identity, buyer)
            if cached:
                return self.public(row)
            if row["revision"] != revision or row["status"] == "archived":
                raise AutomationConflict("任务已更新或归档，请刷新")
            status = {"pause": "paused", "resume": "active", "archive": "archived"}[
                action
            ]
            next_at = (
                AutomationDefinition.model_validate_json(
                    row["definition"]
                ).schedule.next(self.clock())
                if status == "active"
                else row["next_run_at"]
            )
            if status == "active" and next_at is None:
                raise AutomationConflict("任务已到期，请编辑新的执行时间")
            if status != "active":
                await self.cancel_queued(db, identity, "任务已暂停或归档")
            await db.execute(
                "UPDATE automations SET status=?,revision=revision+1,next_run_at=?,updated_at=?,failures=0 WHERE id=?",
                (status, next_at, self.clock(), identity),
            )
            await self.audit(db, row, action, {"revision": revision})
            await self.remember(db, buyer, request_id, payload, {"id": identity})
            return self.public(await self.owned(db, "automations", identity, buyer))

    async def insert_run(
        self,
        db,
        task,
        scheduled,
        kind,
        *,
        manual=False,
        catchup_from=None,
        forced_status=None,
    ):
        definition = AutomationDefinition.model_validate_json(task["definition"])
        active = await self.one(
            db,
            "SELECT id FROM automation_runs WHERE automation_id=? AND status IN ('queued','starting','running','awaiting_start','awaiting_confirmation')",
            (task["id"],),
        )
        if active and manual:
            raise AutomationConflict("该自动化还有未结束的执行，请先处理")
        status = forced_status or (
            "skipped_overlap"
            if active
            else "queued"
            if manual or definition.auto_start
            else "awaiting_start"
        )
        cutoff = definition.schedule.cutoff()
        if cutoff is not None and self.clock() >= cutoff:
            status = "expired"
        identity = uuid.uuid4().hex
        session = (
            task["session_id"]
            if definition.output_mode == "merged"
            else "auto-" + uuid.uuid4().hex
        )
        now = self.clock()
        await db.execute(
            "INSERT OR IGNORE INTO automation_runs(id,automation_id,buyer_id,definition,definition_revision,scheduled_for,trigger_kind,status,session_id,created_at,updated_at,catchup_from,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                task["id"],
                task["buyer_id"],
                task["definition"],
                task["revision"],
                scheduled,
                kind,
                status,
                session,
                now,
                now,
                catchup_from,
                None if status in ACTIVE else now,
            ),
        )
        return await self.one(
            db,
            "SELECT * FROM automation_runs WHERE automation_id=? AND trigger_kind=? AND scheduled_for=?",
            (task["id"], kind, scheduled),
        )

    async def manual(self, identity, buyer, request_id, revision):
        payload = {"op": "run", "id": identity, "revision": revision}
        async with self.tx() as db:
            cached = await self.command(db, buyer, request_id, payload)
            if cached:
                return self.public(
                    await self.owned(db, "automation_runs", cached["id"], buyer)
                )
            task = await self.owned(db, "automations", identity, buyer)
            if task["status"] != "active" or task["revision"] != revision:
                raise AutomationConflict("任务已暂停、归档或更新，请刷新")
            row = await self.insert_run(
                db, task, self.clock(), "manual:" + request_id, manual=True
            )
            await self.remember(db, buyer, request_id, payload, {"id": row["id"]})
            return self.public(row)

    async def tick(self, worker):
        now = self.clock()
        async with self.tx() as db:
            await db.execute(
                "INSERT INTO automation_workers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",
                (worker, now),
            )
            rows = await (
                await db.execute(
                    "SELECT * FROM automations WHERE status='active' AND next_run_at<=? ORDER BY next_run_at LIMIT 100",
                    (now,),
                )
            ).fetchall()
            for task in rows:
                schedule = AutomationDefinition.model_validate_json(
                    task["definition"]
                ).schedule
                latest = schedule.next(now + 0.001, reverse=True)
                latest = max(task["next_run_at"], latest or task["next_run_at"])
                cutoff = schedule.cutoff()
                state = (
                    "expired"
                    if cutoff is not None and now >= cutoff
                    else "missed"
                    if now - latest > 3600
                    else None
                )
                await self.insert_run(
                    db,
                    task,
                    latest,
                    "schedule",
                    catchup_from=task["next_run_at"]
                    if latest > task["next_run_at"]
                    else None,
                    forced_status=state,
                )
                next_at = schedule.next(now)
                await db.execute(
                    "UPDATE automations SET next_run_at=?,status=?,updated_at=? WHERE id=?",
                    (next_at, "active" if next_at else "expired", now, task["id"]),
                )
            await db.execute(
                "UPDATE automation_runs SET status='expired',revision=revision+1,finished_at=?,updated_at=? WHERE status='awaiting_start' AND created_at<?",
                (now, now, now - 86400),
            )

    async def claim(self, owner, limit=2):
        now = self.clock()
        async with self.tx() as db:
            # 未进入 AG-UI 的过期领取可以换 owner；已开始的只对账，不重放。
            await db.execute(
                "UPDATE automation_runs SET status='queued' WHERE status='starting' AND EXISTS(SELECT 1 FROM automation_attempts a WHERE a.run_id=automation_runs.id AND a.attempt_no=automation_runs.attempt_no AND a.lease_until<=? AND NOT EXISTS(SELECT 1 FROM agui_runs g WHERE g.run_id=a.agent_run_id))",
                (now,),
            )
            n = await self.one(
                db,
                "SELECT count(*) n FROM automation_runs WHERE status IN ('starting','running')",
            )
            if n["n"] >= limit:
                return None
            row = await self.one(
                db,
                "SELECT r.* FROM automation_runs r JOIN automations t ON t.id=r.automation_id WHERE r.status='queued' AND t.status IN ('active','expired') ORDER BY r.created_at LIMIT 1",
            )
            if not row:
                return None
            cutoff = AutomationDefinition.model_validate_json(
                row["definition"]
            ).schedule.cutoff()
            if cutoff is not None and now >= cutoff:
                await self.finish(db, row, "expired", "任务已到期")
                return None
            attempt = await self.one(
                db,
                "SELECT * FROM automation_attempts WHERE run_id=? AND attempt_no=?",
                (row["id"], row["attempt_no"]),
            )
            if attempt is None or attempt["finished_at"] is not None:
                attempt_id, agent_id = uuid.uuid4().hex, "auto-run-" + uuid.uuid4().hex
                number = row["attempt_no"] + 1
                await db.execute(
                    "INSERT INTO automation_attempts(id,run_id,attempt_no,agent_run_id,owner,fence,lease_until) VALUES(?,?,?,?,?,1,?)",
                    (attempt_id, row["id"], number, agent_id, owner, now + 60),
                )
            else:
                attempt_id, number = attempt["id"], attempt["attempt_no"]
                await db.execute(
                    "UPDATE automation_attempts SET owner=?,fence=fence+1,lease_until=? WHERE id=?",
                    (owner, now + 60, attempt_id),
                )
            await db.execute(
                "UPDATE automation_runs SET status='starting',attempt_no=?,revision=revision+1,updated_at=? WHERE id=?",
                (number, now, row["id"]),
            )
            attempt = await self.one(
                db, "SELECT * FROM automation_attempts WHERE id=?", (attempt_id,)
            )
            return {
                "run": self.public(
                    await self.owned(db, "automation_runs", row["id"], row["buyer_id"])
                ),
                "attempt": dict(attempt),
            }

    async def prepared(self, attempt, model):
        async with self.tx() as db:
            cursor = await db.execute(
                "UPDATE automation_attempts SET model_json=? WHERE id=? AND owner=? AND fence=? AND lease_until>?",
                (
                    encode(model),
                    attempt["id"],
                    attempt["owner"],
                    attempt["fence"],
                    self.clock(),
                ),
            )
            if cursor.rowcount != 1:
                raise AutomationConflict("执行领取已失效")

    async def fail_start(self, claim, reason, status="failed"):
        async with self.tx() as db:
            attempt = claim["attempt"]
            current = await self.one(
                db, "SELECT * FROM automation_attempts WHERE id=?", (attempt["id"],)
            )
            row = await self.owned(
                db, "automation_runs", claim["run"]["id"], claim["run"]["buyer_id"]
            )
            if (
                current["owner"] == attempt["owner"]
                and current["fence"] == attempt["fence"]
                and row["status"] == "starting"
            ):
                await self.finish(db, row, status, reason)

    async def finish(self, db, row, status, error=None):
        if row["status"] not in ACTIVE:
            return
        now = self.clock()
        await db.execute(
            "UPDATE automation_runs SET status=?,error=?,finished_at=?,updated_at=?,revision=revision+1 WHERE id=?",
            (status, error, now, now, row["id"]),
        )
        await db.execute(
            "UPDATE automation_attempts SET finished_at=? WHERE run_id=? AND attempt_no=?",
            (now, row["id"], row["attempt_no"]),
        )
        if status in ("failed", "interrupted"):
            await db.execute(
                "UPDATE automations SET failures=failures+1 WHERE id=?",
                (row["automation_id"],),
            )
            await db.execute(
                "UPDATE automations SET status='paused',revision=revision+1 WHERE id=? AND failures>=3 AND status='active'",
                (row["automation_id"],),
            )
        elif status == "succeeded":
            await db.execute(
                "UPDATE automations SET failures=0 WHERE id=?", (row["automation_id"],)
            )

    async def reconcile(self, confirmations=None):
        # 订单确认在另一个存储中，读取后再核对当前 run；不声称跨库原子事务。
        async with self.tx() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM automation_runs WHERE status IN ('starting','running','awaiting_confirmation')"
                )
            ).fetchall()
        trade = {}
        if confirmations:
            for row in rows:
                trade[row["id"]] = await confirmations.list(
                    row["buyer_id"], row["session_id"]
                )
        async with self.tx() as db:
            for before in rows:
                row = await self.owned(
                    db, "automation_runs", before["id"], before["buyer_id"]
                )
                if row["status"] not in ACTIVE:
                    continue
                attempt = await self.one(
                    db,
                    "SELECT * FROM automation_attempts WHERE run_id=? AND attempt_no=?",
                    (row["id"], row["attempt_no"]),
                )
                if not attempt:
                    continue
                run = await self.one(
                    db,
                    "SELECT * FROM agui_runs WHERE run_id=?",
                    (attempt["agent_run_id"],),
                )
                if not run:
                    continue
                if run["status"] == "running":
                    await db.execute(
                        "UPDATE automation_runs SET status='running' WHERE id=?",
                        (row["id"],),
                    )
                    if self.clock() - run["created_at"] > 600:
                        await db.execute(
                            "UPDATE agui_runs SET stop_requested=1 WHERE run_id=?",
                            (run["run_id"],),
                        )
                    continue
                projection = json.loads(run["projection_json"])
                session = await self.one(
                    db,
                    "SELECT * FROM agui_sessions WHERE session_id=?",
                    (row["session_id"],),
                )
                state = (
                    json.loads(session["state_json"])
                    if session
                    else projection["state"]
                )
                pending = bool(state.get("toolApprovals")) or any(
                    x["status"] == "pending" and not x.get("expired")
                    for x in trade.get(row["id"], {}).get("confirmations", [])
                )
                if pending and run["status"] == "completed":
                    await db.execute(
                        "UPDATE automation_runs SET status='awaiting_confirmation' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                if (
                    row["status"] == "awaiting_confirmation"
                    and session
                    and session["last_run_id"] != run["run_id"]
                ):
                    resumed = await self.one(
                        db,
                        "SELECT status FROM agui_runs WHERE run_id=?",
                        (session["last_run_id"],),
                    )
                    if resumed and resumed["status"] == "running":
                        continue
                    if resumed and resumed["status"] != "completed":
                        await self.finish(
                            db, row, "failed", "审批恢复未完成，请查看会话"
                        )
                        continue
                status = {
                    "completed": "succeeded",
                    "stopped": "cancelled",
                    "interrupted": "interrupted",
                    "error": "failed",
                }[run["status"]]
                await self.finish(
                    db,
                    row,
                    status,
                    None
                    if status == "succeeded"
                    else "执行未完成，请查看会话中的错误记录",
                )

    async def runs(self, buyer, *, identity=None, status=None, limit=50, offset=0):
        where = "buyer_id=?"
        params = [buyer]
        if identity:
            where += " AND automation_id=?"
            params.append(identity)
        if status:
            where += " AND status=?"
            params.append(status)
        async with self.tx() as db:
            rows = await (
                await db.execute(
                    f"SELECT * FROM automation_runs WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )
            ).fetchall()
            total = await self.one(
                db, f"SELECT count(*) n FROM automation_runs WHERE {where}", params
            )
            return {"items": [self.public(row) for row in rows], "total": total["n"]}

    async def run(self, identity, buyer):
        async with self.tx() as db:
            row = await self.owned(db, "automation_runs", identity, buyer)
            attempts = await (
                await db.execute(
                    "SELECT * FROM automation_attempts WHERE run_id=? ORDER BY attempt_no",
                    (identity,),
                )
            ).fetchall()
            result = self.public(row)
            result["attempts"] = []
            for a in attempts:
                model = json.loads(a["model_json"]) if a["model_json"] else None
                from app.application.agents.model_selection import public_selection

                events = await (
                    await db.execute(
                        "SELECT event_json FROM agui_events WHERE run_id=? ORDER BY seq",
                        (a["agent_run_id"],),
                    )
                ).fetchall()
                # usage 只取实际事件；没有上报时明确未知。
                usages = [json.loads(e[0]) for e in events if "usage.summary" in e[0]]
                result["attempts"].append(
                    {
                        "id": a["id"],
                        "agent_run_id": a["agent_run_id"],
                        "attempt_no": a["attempt_no"],
                        "started_at": a["started_at"],
                        "finished_at": a["finished_at"],
                        "model": public_selection(model),
                        "usage": usages or None,
                    }
                )
            return result

    async def run_action(self, identity, buyer, action, revision, request_id):
        payload = {"op": action, "run": identity, "revision": revision}
        async with self.tx() as db:
            cached = await self.command(db, buyer, request_id, payload)
            row = await self.owned(db, "automation_runs", identity, buyer)
            if cached:
                return self.public(row)
            if row["revision"] != revision:
                raise AutomationConflict("执行状态已变化，请刷新")
            task = await self.owned(db, "automations", row["automation_id"], buyer)
            if action in ("start", "retry"):
                if (
                    task["status"] not in ("active", "expired")
                    or task["revision"] != row["definition_revision"]
                ):
                    raise AutomationConflict("任务设置已变化，请重新发起执行")
                cutoff = AutomationDefinition.model_validate_json(
                    row["definition"]
                ).schedule.cutoff()
                if cutoff is not None and self.clock() >= cutoff:
                    raise AutomationConflict("任务已到期")
                allowed = (
                    ("awaiting_start",)
                    if action == "start"
                    else ("failed", "interrupted", "cancelled")
                )
                if row["status"] not in allowed:
                    raise AutomationConflict("当前状态不支持此操作")
                other = await self.one(
                    db,
                    "SELECT id FROM automation_runs WHERE automation_id=? AND id<>? AND status IN ('queued','starting','running','awaiting_start','awaiting_confirmation')",
                    (task["id"], identity),
                )
                if other:
                    raise AutomationConflict("该任务还有未结束的执行")
                # 旧尝试保留；重试用新 attempt 编号，不重放原 runId。
                await db.execute(
                    "UPDATE automation_runs SET status='queued',error=NULL,finished_at=NULL,revision=revision+1,updated_at=? WHERE id=?",
                    (self.clock(), identity),
                )
            else:
                if row["status"] == "awaiting_confirmation":
                    raise AutomationConflict("请打开结果会话，明确批准或拒绝待确认操作")
                if row["status"] not in ACTIVE:
                    raise AutomationConflict("执行已经结束")
                await db.execute(
                    "UPDATE agui_runs SET stop_requested=1 WHERE run_id IN (SELECT agent_run_id FROM automation_attempts WHERE run_id=?) AND status='running'",
                    (identity,),
                )
                await self.finish(db, row, "cancelled", "买家已停止")
            await self.remember(db, buyer, request_id, payload, {"id": identity})
            await self.audit(db, task, "run_" + action, {"run_id": identity})
            return self.public(await self.owned(db, "automation_runs", identity, buyer))
