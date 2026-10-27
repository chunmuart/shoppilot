# -*- coding: utf-8 -*-
"""Redis 队列归档。先持久化完整记录，再允许删除已确认的 stream 消息。"""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import time

import aiosqlite


class QueueArchive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._ready = False
        self._lock = asyncio.Lock()

    async def initialize(self):
        async with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.path, timeout=10) as db:
                await db.execute("PRAGMA busy_timeout=10000")
                await db.executescript("""
                CREATE TABLE IF NOT EXISTS queue_archived_tasks (
                  task_id TEXT PRIMARY KEY, status_json TEXT NOT NULL, archived_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS queue_archived_messages (
                  stream TEXT NOT NULL, message_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                  task_id TEXT NOT NULL, archived_at REAL NOT NULL, PRIMARY KEY(stream,message_id));
                """)
                await db.commit()
            self._ready = True

    async def put(self, stream: str, message_id: str, payload: dict, status: dict):
        await self.initialize()
        task_id = status.get("task_id", "")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        status_json = json.dumps(status, ensure_ascii=False, sort_keys=True)
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT payload_json FROM queue_archived_messages WHERE stream=? AND message_id=?", (stream, message_id)) as cursor:
                previous = await cursor.fetchone()
            if previous and previous[0] != payload_json:
                raise ValueError("归档中已存在相同消息 ID 的不同内容，拒绝覆盖")
            await db.execute("INSERT OR IGNORE INTO queue_archived_messages VALUES(?,?,?,?,?)", (stream, message_id, payload_json, task_id, time.time()))
            if task_id:
                async with db.execute("SELECT status_json FROM queue_archived_tasks WHERE task_id=?", (task_id,)) as cursor:
                    previous = await cursor.fetchone()
                if previous and previous[0] != status_json:
                    raise ValueError("归档中已存在相同任务的不同终态，拒绝覆盖")
                await db.execute("INSERT OR IGNORE INTO queue_archived_tasks VALUES(?,?,?)", (task_id, status_json, time.time()))
            await db.commit()

    async def get_status(self, task_id: str) -> dict | None:
        await self.initialize()
        async with aiosqlite.connect(self.path, timeout=10) as db:
            async with db.execute("SELECT status_json FROM queue_archived_tasks WHERE task_id=?", (task_id,)) as cursor:
                row = await cursor.fetchone()
                return json.loads(row[0]) if row else None

    async def message_count(self) -> int:
        await self.initialize()
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM queue_archived_messages") as cursor:
                return (await cursor.fetchone())[0]
