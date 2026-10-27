# -*- coding: utf-8 -*-
"""初始化使用真实 SQLite 连接；连续 20 个新库、每轮 6 个并发实例。"""
import asyncio

import aiosqlite
import pytest

from app.infrastructure.ag_ui_journal import AGUIJournal
from tests.test_ag_ui_journal import body


@pytest.mark.parametrize("round_number", range(20))
async def test_six_instances_initialize_same_new_sqlite_and_reserve_once(tmp_path, round_number):
    journals = [AGUIJournal(tmp_path / f"round-{round_number}.db") for _ in range(6)]
    entries = await asyncio.wait_for(asyncio.gather(*[
        journal.reserve(body(), "b1", f"owner-{index}") for index, journal in enumerate(journals)
    ]), 12)
    assert sum(created for _, created in entries) == 1
    assert all(journal._initialized for journal in journals)
    states = await asyncio.gather(*(journal.run("r1", "b1") for journal in journals))
    assert all(state["status"] == "running" for state in states)
    restored = AGUIJournal(tmp_path / f"round-{round_number}.db")
    assert (await restored.run("r1", "b1"))["status"] == "running"


async def test_real_ddl_write_lock_is_retried_then_released(tmp_path):
    path = tmp_path / "locked.db"
    journal = AGUIJournal(path)
    async with aiosqlite.connect(path) as writer:
        async with writer.execute("PRAGMA journal_mode=WAL") as cursor:
            await cursor.fetchall()
        await writer.execute("BEGIN IMMEDIATE")
        await writer.execute("CREATE TABLE sentinel(value TEXT)")
        pending = asyncio.create_task(journal.initialize())
        try:
            # 超过初始化连接 500ms busy_timeout，真实触发 DDL busy 后再解除写锁。
            await asyncio.sleep(.7)
            assert not pending.done() and not journal._initialized
            await writer.commit()
            await asyncio.wait_for(pending, 3)
        finally:
            await writer.rollback()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert journal._initialized
    assert (await journal.reserve(body(), "b1", "owner"))[1] is True


async def test_invalid_database_is_not_retried_as_lock_contention(tmp_path):
    path = tmp_path / "invalid.db"
    path.write_bytes(b"not a SQLite database" * 100)
    journal = AGUIJournal(path)
    with pytest.raises(aiosqlite.DatabaseError, match="not a database"):
        await asyncio.wait_for(journal.initialize(), 1)
    assert not journal._initialized
