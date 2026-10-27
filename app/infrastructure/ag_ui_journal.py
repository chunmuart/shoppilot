# -*- coding: utf-8 -*-
"""AG-UI 持久事件日志：SQLite 事务内追加序号、更新投影并绑定买家归属。"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import aiosqlite

from app.domain.learning import PROCESSOR_VERSION, canonical, digest
from app.infrastructure.learning_store import LEARNING_SCHEMA, job_identity, terminal_source
from app.infrastructure.tool_product_view import tool_product_view


class JournalConflict(ValueError):
    pass


class JournalForbidden(PermissionError):
    pass


class JournalNotFound(LookupError):
    pass


class JournalLeaseLost(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _close_activity_state(state: dict, *, stopped: bool) -> dict:
    """崩溃恢复也收口展示投影；不能让工具或 Thinking 永久显示进行中。"""
    state = copy.deepcopy(state)
    state["status"] = "cancelled" if stopped else "error"
    for activity in state.setdefault("activities", []):
        if activity.get("status") in {"preparing", "running", "waiting"}:
            activity.update(status="cancelled" if stopped else "interrupted", finishedAt=int(time.time() * 1000))
    for entry in state.get("progress", []):
        if entry.get("status") == "running":
            entry["status"] = "error"
    for entry in state.get("skillUsages", []):
        if entry.get("status") == "reading":
            entry.update(status="error", error="本轮中断，未收到方案读取成功的结果。")
    return state


class AGUIJournal:
    def __init__(self, path: str | Path, *, learning_enabled=False, learning_versions=None):
        self.path = Path(path)
        self.learning_enabled = learning_enabled
        self.learning_versions = learning_versions or {}
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self):
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 初始化短等待；总体期限避免 busy_timeout × 重试次数形成长时间挂起。
            deadline = time.monotonic() + 8.0
            for attempt in range(20):
                try:
                    # 每次失败都关闭连接，不能带着上一轮未完成游标/事务重试。
                    async with aiosqlite.connect(self.path, timeout=0.5) as db:
                        async with db.execute("PRAGMA busy_timeout=500") as cursor:
                            await cursor.fetchall()
                        # PRAGMA 会返回结果行；显式耗尽并关闭，避免游标持锁阻碍后续 DDL。
                        async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                            await cursor.fetchall()
                        await db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS agui_sessions (
                  session_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, title TEXT NOT NULL,
                  messages_json TEXT NOT NULL, state_json TEXT NOT NULL, last_run_id TEXT NOT NULL,
                  updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS agui_buyer_sessions ON agui_sessions(buyer_id,updated_at);
                CREATE TABLE IF NOT EXISTS agui_runs (
                  run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, input_json TEXT NOT NULL, projection_json TEXT NOT NULL,
                  status TEXT NOT NULL, owner TEXT NOT NULL, lease_until REAL NOT NULL,
                  last_seq INTEGER NOT NULL DEFAULT 0, stop_requested INTEGER NOT NULL DEFAULT 0,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS agui_run_session ON agui_runs(session_id,status);
                CREATE TABLE IF NOT EXISTS agui_events (
                  run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL,
                  PRIMARY KEY(run_id,seq));
                        """ + LEARNING_SCHEMA + "COMMIT;")
                    self._initialized = True
                    return
                except aiosqlite.OperationalError as err:
                    if (not any(marker in str(err).lower() for marker in ("locked", "busy"))
                            or attempt == 19 or time.monotonic() >= deadline):
                        raise
                    await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))

    @asynccontextmanager
    async def _db(self, write=False):
        await self.initialize()
        async with aiosqlite.connect(self.path, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            if write:
                await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    await db.commit()
            except BaseException:
                if write:
                    await db.rollback()
                raise

    @staticmethod
    async def _one(db, sql, params=()):
        async with db.execute(sql, params) as cursor:
            return await cursor.fetchone()

    @staticmethod
    def _owned(row, buyer_id, session_id=None):
        if row is None:
            raise JournalNotFound("运行或会话不存在")
        if row["buyer_id"] != buyer_id or (session_id is not None and row["session_id"] != session_id):
            raise JournalForbidden("无权访问该买家的会话或运行")
        return row

    @staticmethod
    def _run(row):
        projection = json.loads(row["projection_json"])
        projection["state"].setdefault("activities", [])
        return {"runId": row["run_id"], "threadId": row["session_id"], "status": row["status"],
                "cursor": row["last_seq"], "stopRequested": bool(row["stop_requested"]),
                "input": json.loads(row["input_json"]), "messages": projection["messages"],
                "state": projection["state"], "updatedAt": int(row["updated_at"] * 1000)}

    async def latest_destination(self,session_id,buyer_id):
        """仅从本会话已保存的商品报价恢复目的地，不迁移旧 Agent/Skill 正文。"""
        async with self._db() as db:
            session=await self._one(db,"SELECT * FROM agui_sessions WHERE session_id=?",(session_id,))
            self._owned(session,buyer_id)
            async with db.execute("SELECT projection_json FROM agui_runs WHERE session_id=? AND buyer_id=? ORDER BY created_at DESC LIMIT 30",(session_id,buyer_id)) as cursor:
                rows=await cursor.fetchall()
            for row in rows:
                products=json.loads(row["projection_json"]).get("state",{}).get("products",[])
                destinations={(p.get("landed_price") or {}).get("ship_to") for p in products}
                destinations.discard(None)
                if len(destinations)==1:
                    country=destinations.pop()
                    if isinstance(country,str) and len(country)==2 and country.isascii() and country.isalpha():return country.upper()
        return None

    async def reserve(self, body: dict, buyer_id: str, owner: str, lease_seconds=30, *, automation_ticket=None) -> tuple[dict, bool]:
        now, run_id, session_id = time.time(), body["runId"], body["threadId"]
        user = body["messages"][-1]
        props = body.get("forwardedProps") or {}
        identity = {"threadId": session_id, "buyerId": buyer_id, "message": user,
                    "locale": props.get("locale", "zh-CN"), "currency": props.get("currency", "CNY")}
        # 未选择时保持旧运行指纹；有明确选择时版本/hash均属于本次请求身份。
        if "selectedSkill" in props:
            identity["selectedSkill"] = props["selectedSkill"]
        if body.get("resume"):
            identity["resume"] = body["resume"]
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self._db(True) as db:
            await self._recover(db, now)
            previous = await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,))
            if previous:
                self._owned(previous, buyer_id, session_id)
                if previous["fingerprint"] != fingerprint:
                    raise JournalConflict("相同 runId 已用于不同请求")
                return self._run(previous), False
            if automation_ticket is not None:
                # 与 reserve 同库同事务；暂停/编辑/失租和真正启动之间没有检查窗口。
                ticket = await self._one(db, """SELECT a.*,r.status run_status,r.session_id,r.buyer_id,
                    r.definition_revision,t.revision task_revision,t.status task_status
                    FROM automation_attempts a JOIN automation_runs r ON r.id=a.run_id
                    JOIN automations t ON t.id=r.automation_id WHERE a.id=?""", (automation_ticket['id'],))
                if (not ticket or ticket['owner'] != automation_ticket['owner']
                    or ticket['fence'] != automation_ticket['fence'] or ticket['lease_until'] <= now
                    or ticket['run_status'] != 'starting' or ticket['task_status'] not in ('active','expired')
                    or ticket['definition_revision'] != ticket['task_revision']
                    or ticket['agent_run_id'] != run_id or ticket['session_id'] != session_id or ticket['buyer_id'] != buyer_id):
                    raise JournalConflict("自动化配置或执行领取已失效")
            session = await self._one(db, "SELECT * FROM agui_sessions WHERE session_id=?", (session_id,))
            if session:
                self._owned(session, buyer_id)
            running = await self._one(db, "SELECT run_id FROM agui_runs WHERE session_id=? AND status='running'", (session_id,))
            if running:
                raise JournalConflict("该会话仍有执行中的运行，请先恢复或明确停止它")
            previous_state=json.loads(session["state_json"]) if session else {}
            if automation_ticket is not None and previous_state.get('toolApprovals'):
                raise JournalConflict("该会话有待确认操作，请先处理")
            if body.get("resume"):
                pending={p['id'] for p in previous_state.get('toolApprovals',[])}
                requested=[p['interruptId'] for p in body['resume']]
                if not requested or len(set(requested))!=len(requested) or not set(requested)<=pending:
                    raise JournalConflict("确认已处理或与当前待执行操作不一致，请刷新会话")
            trusted_state={k:previous_state[k] for k in ('products','searchCompleted','skillUsages') if k in previous_state} if body.get('resume') else {}
            messages = json.loads(session["messages_json"]) if session else []
            # 客户端历史/state 不是事实来源；只接受本轮 user，旧历史由日志恢复。
            messages = [*messages[-99:], {"id": user["id"], "role": "user", "content": user["content"], "runId": run_id}]
            body = {**body, "messages": messages, "state": trusted_state}
            projection = {"messages": messages, "state": {"activities": []}, "openMessages": [], "openTools": []}
            await db.execute("INSERT INTO agui_runs(run_id,session_id,buyer_id,fingerprint,input_json,projection_json,status,owner,lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,'running',?,?,?,?)",
                (run_id, session_id, buyer_id, fingerprint, _json(body), _json(projection), owner, now + lease_seconds, now, now))
            if automation_ticket is not None:
                await db.execute("UPDATE automation_attempts SET started_at=? WHERE id=?", (now, automation_ticket['id']))
                await db.execute("UPDATE automation_runs SET status='running',updated_at=? WHERE id=?", (now, ticket['run_id']))
            title = session["title"] if session else str(user["content"])[:48]
            await db.execute("INSERT INTO agui_sessions VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET messages_json=excluded.messages_json,state_json=excluded.state_json,last_run_id=excluded.last_run_id,updated_at=excluded.updated_at",
                (session_id, buyer_id, title, _json(messages), "{}", run_id, now))
            return self._run(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,))), True

    @staticmethod
    def _project(projection, event, run_id):
        kind = event["type"]
        if kind == "MESSAGES_SNAPSHOT":
            owners = {m["id"]: m.get("runId") for m in projection["messages"]}
            messages = []
            for message in event["messages"]:
                if message.get("role") not in {"user", "assistant"}:
                    continue
                # 老日志尚无 runId 时保留未知，session 按首次持久出现的轮次补齐；
                # 不能把本轮携带的所有旧历史重新归属到当前运行。
                owner = owners[message["id"]] if message["id"] in owners else run_id
                item = {key: value for key, value in message.items() if key != "runId"}
                if owner:
                    item["runId"] = owner
                messages.append(item)
            projection["messages"] = messages
        elif kind == "STATE_SNAPSHOT":
            projection["state"] = event["snapshot"]
        elif kind == "CUSTOM" and event.get("name") == "activity.upsert":
            activity = event["value"]
            activities = projection["state"].setdefault("activities", [])
            previous = next((i for i, item in enumerate(activities) if item["id"] == activity["id"]), None)
            if previous is None:
                activities.append(copy.deepcopy(activity))
            else:
                activities[previous] = copy.deepcopy(activity)
        elif kind == "TEXT_MESSAGE_START":
            message_id = event["messageId"]
            if not any(m["id"] == message_id for m in projection["messages"]):
                projection["messages"].append({"id": message_id, "role": "assistant", "content": "", "runId": run_id})
            if message_id not in projection["openMessages"]:
                projection["openMessages"].append(message_id)
        elif kind == "TEXT_MESSAGE_CONTENT":
            message = next((m for m in projection["messages"] if m["id"] == event["messageId"]), None)
            if message is None:
                raise JournalConflict("消息增量必须在对应 START 之后")
            message["content"] += event["delta"]
        elif kind == "TEXT_MESSAGE_END":
            projection["openMessages"] = [i for i in projection["openMessages"] if i != event["messageId"]]
        elif kind == "TOOL_CALL_START":
            projection["openTools"].append(event["toolCallId"])
        elif kind == "TOOL_CALL_END":
            projection["openTools"] = [i for i in projection["openTools"] if i != event["toolCallId"]]

    async def _append(self, db, row, events):
        projection, seq, status = json.loads(row["projection_json"]), row["last_seq"], row["status"]
        for event in events:
            if event["type"] == "CUSTOM" and event.get("name") == "activity.upsert":
                activity = event.get("value")
                if (not isinstance(activity, dict) or activity.get("runId") != row["run_id"]
                        or not isinstance(activity.get("id"), str) or not activity["id"].startswith(row["run_id"] + ":")):
                    raise JournalConflict("活动必须属于当前运行")
            self._project(projection, event, row["run_id"])
            seq += 1
            await db.execute("INSERT INTO agui_events VALUES(?,?,?)", (row["run_id"], seq, _json(event)))
            if event["type"] == "RUN_FINISHED":
                status = "completed"
            elif event["type"] == "RUN_ERROR":
                status = "stopped" if event.get("code") == "CANCELLED" else "interrupted" if event.get("code") == "SERVER_RESTART" else "error"
        now = time.time()
        await db.execute("UPDATE agui_runs SET projection_json=?,last_seq=?,status=?,updated_at=? WHERE run_id=?",
                         (_json(projection), seq, status, now, row["run_id"]))
        await db.execute("UPDATE agui_sessions SET messages_json=?,state_json=?,updated_at=? WHERE session_id=? AND last_run_id=?",
                         (_json(projection["messages"][-100:]), _json(projection["state"]), now, row["session_id"], row["run_id"]))
        if self.learning_enabled and status != "running" and row["status"] == "running":
            # 终态和 outbox 同一数据库事务，不能出现页面已完成、任务却未入队的窗口。
            source_hash = digest(terminal_source(row, projection, seq, status))
            identity = job_identity("agui", row["run_id"], source_hash, row["buyer_id"], row["session_id"])
            payload = {"last_seq": seq, "status": status, "processor_version": PROCESSOR_VERSION,
                       "versions": self.learning_versions}
            await db.execute("INSERT OR IGNORE INTO learning_jobs(id,source_kind,source_ref,buyer_id,session_id,source_hash,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (identity, "agui", row["run_id"], row["buyer_id"], row["session_id"], source_hash, canonical(payload), now, now))

    async def append(self, run_id, owner, events):
        async with self._db(True) as db:
            row = await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,))
            if not row or row["owner"] != owner or row["status"] != "running" or row["lease_until"] <= time.time():
                raise JournalLeaseLost("运行日志执行租约已失效")
            await self._append(db, row, events)

    async def renew(self, run_id, owner, lease_seconds):
        async with self._db(True) as db:
            cursor = await db.execute("UPDATE agui_runs SET lease_until=? WHERE run_id=? AND owner=? AND status='running' AND stop_requested=0 AND lease_until>?",
                (time.time() + lease_seconds, run_id, owner, time.time()))
            return cursor.rowcount == 1

    async def _recover(self, db, now):
        async with db.execute("SELECT * FROM agui_runs WHERE status='running' AND lease_until<=?", (now,)) as cursor:
            expired = await cursor.fetchall()
        for row in expired:
            projection = json.loads(row["projection_json"])
            stopped = bool(row["stop_requested"])
            events = [{"type": "TEXT_MESSAGE_END", "messageId": i} for i in projection["openMessages"]]
            if row["last_seq"] == 0:
                events.insert(0, {"type": "RUN_STARTED", "threadId": row["session_id"], "runId": row["run_id"]})
            events += [{"type": "TOOL_CALL_END", "toolCallId": i} for i in projection["openTools"]]
            events += [{"type": "STATE_SNAPSHOT", "snapshot": _close_activity_state(projection["state"], stopped=stopped)},
                       {"type": "RUN_ERROR", "message": "本轮已明确停止" if stopped else "服务重启或执行租约失效，本轮未完成；已恢复保存的内容，可重新提交需求。", "code": "CANCELLED" if stopped else "SERVER_RESTART"}]
            await self._append(db, row, events)

    async def recover_expired(self):
        async with self._db(True) as db:
            await self._recover(db, time.time())

    async def run(self, run_id, buyer_id, session_id=None):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            return self._run(self._owned(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,)), buyer_id, session_id))

    async def events(self, run_id, buyer_id, after=0, limit=200):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            row = self._owned(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,)), buyer_id)
            if after < 0 or after > row["last_seq"]:
                raise JournalConflict("事件游标超出该运行范围")
            async with db.execute("SELECT seq,event_json FROM agui_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after, limit)) as cursor:
                events = [{"seq": item["seq"], "event": json.loads(item["event_json"])} for item in await cursor.fetchall()]
            return events, row["status"], row["last_seq"]

    async def run_session_id(self, run_id, buyer_id):
        """只读取得已校验的会话归属，不恢复租约或装载整轮消息、商品。"""
        async with self._db() as db:
            row = self._owned(await self._one(db,
                "SELECT session_id,buyer_id FROM agui_runs WHERE run_id=?", (run_id,)), buyer_id)
            return row["session_id"]

    async def tool_products(self, run_id, tool_call_id, buyer_id):
        """按原始调用 ID 读取已存结果；不借用本轮最新商品，也不重新执行工具。"""
        async with self._db() as db:
            row = self._owned(await self._one(db,
                "SELECT run_id,session_id,buyer_id,status,lease_until FROM agui_runs WHERE run_id=?", (run_id,)), buyer_id)
            async with db.execute(
                "SELECT seq,json_extract(event_json,'$.toolCallName') AS tool_name FROM agui_events "
                "WHERE run_id=? AND json_extract(event_json,'$.type')='TOOL_CALL_START' "
                "AND json_extract(event_json,'$.toolCallId')=? ORDER BY seq LIMIT 2",
                (run_id, tool_call_id),
            ) as cursor:
                starts = await cursor.fetchall()
            if len(starts) != 1 or starts[0]["tool_name"] != "product_search_tool":
                raise JournalNotFound("该调用没有可查看的商品结果")
            result = await self._one(db,
                "SELECT event_json FROM agui_events WHERE run_id=? AND seq>? "
                "AND json_extract(event_json,'$.type')='TOOL_CALL_RESULT' "
                "AND json_extract(event_json,'$.toolCallId')=? ORDER BY seq DESC LIMIT 1",
                (run_id, starts[0]["seq"], tool_call_id))
            identity = {"runId": run_id, "toolCallId": tool_call_id}
            if result is None:
                pending = row["status"] == "running" and row["lease_until"] > time.time()
                return {**identity, "status": "pending" if pending else "unavailable", "products": [],
                        "count": 0, "totalCount": None, "truncated": False,
                        "notice": "工具结果尚未保存，请稍后查看。" if pending else "本次调用没有保存完整的商品结果。"}
            return {**identity, **tool_product_view(json.loads(result["event_json"]).get("content"))}

    async def request_stop(self, run_id, buyer_id):
        async with self._db(True) as db:
            row = self._owned(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,)), buyer_id)
            if row["status"] == "running":
                await db.execute("UPDATE agui_runs SET stop_requested=1 WHERE run_id=?", (run_id,))
        return await self.run(run_id, buyer_id)

    async def end_owned(self, run_id, owner, *, stopped=False):
        """生产协程在首次调度前就被取消时也要收口；终态不覆盖，其他 owner 不可写。"""
        async with self._db(True) as db:
            row = await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (run_id,))
            if not row or row["owner"] != owner or row["status"] != "running":
                return
            projection = json.loads(row["projection_json"])
            events = [{"type": "TEXT_MESSAGE_END", "messageId": i} for i in projection["openMessages"]]
            if row["last_seq"] == 0:
                events.insert(0, {"type": "RUN_STARTED", "threadId": row["session_id"], "runId": row["run_id"]})
            events += [{"type": "TOOL_CALL_END", "toolCallId": i} for i in projection["openTools"]]
            events += [{"type": "STATE_SNAPSHOT", "snapshot": _close_activity_state(projection["state"], stopped=stopped)},
                       {"type": "RUN_ERROR", "message": "本轮已明确停止" if stopped else "服务已关闭，本轮已保存为中断，可恢复查看已有内容。", "code": "CANCELLED" if stopped else "SERVER_RESTART"}]
            await self._append(db, row, events)

    async def sessions(self, buyer_id):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            # 这里只返回轻量目录，不能静默隐藏第 101 个及更早的会话。
            async with db.execute("SELECT session_id,title,updated_at,last_run_id FROM agui_sessions WHERE buyer_id=? ORDER BY updated_at DESC", (buyer_id,)) as cursor:
                return [{"id": row["session_id"], "title": row["title"], "updatedAt": int(row["updated_at"] * 1000), "runId": row["last_run_id"], "source": "server"} for row in await cursor.fetchall()]

    async def session(self, session_id, buyer_id):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            row = self._owned(await self._one(db, "SELECT * FROM agui_sessions WHERE session_id=?", (session_id,)), buyer_id)
            run = self._run(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=?", (row["last_run_id"],)))
            # sessions.messages_json 是下一轮使用的滑动窗口，不是页面完整历史。
            # 旧版本已保留每轮最终投影；按持久顺序合并即可恢复，无需重写原始数据。
            messages, product_history, activities, pending_activities = {}, [], [], {}
            async with db.execute(
                "SELECT run_id,projection_json,input_json,created_at,updated_at FROM agui_runs "
                "WHERE session_id=? AND buyer_id=? ORDER BY created_at,rowid",
                (session_id, buyer_id),
            ) as cursor:
                async for saved in cursor:
                    projection = json.loads(saved["projection_json"])
                    # 只用已受归属和请求指纹校验的原生 resume 决议收口旧确认卡。
                    # “已批准”仅表示决议，实际执行结果保留在后续运行；不改写历史事件。
                    for decision in json.loads(saved["input_json"]).get("resume") or []:
                        approved = decision.get("status") != "cancelled" and (decision.get("payload") or {}).get("approved") is True
                        for activity in pending_activities.pop(decision["interruptId"], []):
                            activity.update(status="approved" if approved else "cancelled",
                                            finishedAt=int(saved["created_at"] * 1000),
                                            resultSummary="已批准，执行结果见后续步骤。" if approved else "你已拒绝此操作。")
                    for message in projection["messages"]:
                        original_run = messages.get(message["id"], {}).get("runId") or saved["run_id"]
                        messages[message["id"]] = {**message, "runId": message.get("runId") or original_run}
                    # 原生公开活动已经过适配器白名单处理，不回查原始参数/工具正文。
                    run_activities = sorted(
                        (item for item in projection["state"].get("activities", []) if item.get("runId") == saved["run_id"]),
                        key=lambda item: item["sequence"],
                    )
                    activities.extend(run_activities)
                    for approval in projection["state"].get("toolApprovals", []):
                        activity = next((item for item in run_activities if item["id"] == approval.get("toolCallId") and item["status"] == "waiting"), None)
                        if activity is not None:
                            pending_activities.setdefault(approval["id"], []).append(activity)
                    products = projection["state"].get("products", [])
                    if products and saved["run_id"] != row["last_run_id"]:
                        product_history.append({"runId": saved["run_id"], "products": products,
                                                "updatedAt": int(saved["updated_at"] * 1000)})
            # 只回传最近 80 条事件的安全元数据，不暴露工具参数、结果或推理原文。
            async with db.execute(
                "SELECT r.run_id,e.seq,json_extract(e.event_json,'$.type') AS type,"
                "json_extract(e.event_json,'$.timestamp') AS timestamp "
                "FROM agui_events e JOIN agui_runs r ON r.run_id=e.run_id "
                "WHERE r.session_id=? AND r.buyer_id=? "
                "AND json_extract(e.event_json,'$.type') NOT IN ('TEXT_MESSAGE_CONTENT','TOOL_CALL_ARGS') "
                "ORDER BY r.created_at DESC,r.rowid DESC,e.seq DESC LIMIT 80",
                (session_id, buyer_id),
            ) as cursor:
                events = [{"id": f"{item['run_id']}:{item['seq']}", "type": item["type"],
                           "timestamp": item["timestamp"]} for item in await cursor.fetchall()]
            state = json.loads(row["state_json"])
            state.setdefault("activities", [])
            return {"id": session_id, "title": row["title"], "messages": list(messages.values()),
                    "activities": activities,
                    "productHistory": product_history, "events": events[::-1],
                    "state": state, "run": run, "updatedAt": int(row["updated_at"] * 1000)}
