# -*- coding: utf-8 -*-
"""真实 Redis + SQLite 验证归档后裁剪，不以 XTRIM 绕过 pending。"""
import json

import pytest

from app.domain.queue.ports.task_queue import TaskStatus
from app.infrastructure.queue.archive import QueueArchive
from app.infrastructure.queue.maintenance import maintain_queue
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _STREAM, _LARGE_STREAM, _DEAD_STREAM, _GROUP, _STATUS_PREFIX
from tests.test_queue_reliability import isolated_redis_url, real_redis, task


async def completed(client, archive, task_id="t1", priority=0):
    queue = RedisStreamTaskQueue(client, archive)
    await queue.ensure_group()
    item = task(task_id, priority=priority)
    await queue.enqueue(item)
    stream = _LARGE_STREAM if priority else _STREAM
    batches = await client.xreadgroup(_GROUP, "worker", {stream: ">"}, count=1)
    message_id, fields = batches[0][1][0]
    async def handler(_):
        return "完整原始结果" * 1000
    await queue._handle_one(stream, message_id, fields, handler, 3, consumer_name="worker")
    return queue, item, message_id


async def test_archive_before_delete_preserves_result_and_permanent_idempotency(real_redis, tmp_path):
    archive = QueueArchive(tmp_path / "archive.db")
    queue, original, _ = await completed(real_redis, archive)
    await completed(real_redis, archive, "t2", priority=1)
    dry = await maintain_queue(real_redis, archive, older_than_seconds=0)
    assert dry["eligible"] == 2 and dry["deleted"] == 0
    assert await archive.message_count() == 0
    outcome = await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True)
    assert outcome["deleted"] == 2
    assert await real_redis.xlen(_STREAM) == await real_redis.xlen(_LARGE_STREAM) == 0
    raw = await real_redis.get(f"{_STATUS_PREFIX}t1")
    assert len(raw) < 600 and json.loads(raw)["archived"]
    assert await real_redis.ttl(f"{_STATUS_PREFIX}t1") == -1
    restored = RedisStreamTaskQueue(real_redis, QueueArchive(tmp_path / "archive.db"))
    assert (await restored.get_status("t1")).final_text == "完整原始结果" * 1000
    await queue.enqueue(original)
    assert await real_redis.xlen(_STREAM) == 0
    assert await archive.message_count() == 2


async def test_never_delete_unread_pending_or_unacknowledged_other_group(real_redis, tmp_path):
    archive = QueueArchive(tmp_path / "archive.db")
    queue = RedisStreamTaskQueue(real_redis, archive)
    await queue.ensure_group()
    await queue.enqueue(task("t1"))
    await queue.set_status(TaskStatus("t1", "done", final_text="已完成"))
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    messages = await real_redis.xreadgroup(_GROUP, "worker", {_STREAM: ">"}, count=1)
    message_id = messages[0][1][0][0]
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    await real_redis.xack(_STREAM, _GROUP, message_id)
    await real_redis.xgroup_create(_STREAM, "audit", id="0")
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    await real_redis.xreadgroup("audit", "reader", {_STREAM: ">"}, count=1)
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    await real_redis.xack(_STREAM, "audit", message_id)
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 1


async def test_archive_failure_never_deletes_redis_message(real_redis, tmp_path):
    class UnavailableArchive(QueueArchive):
        async def put(self, *_):
            raise OSError("模拟归档磁盘不可写")
    archive = UnavailableArchive(tmp_path / "archive.db")
    await completed(real_redis, archive)
    with pytest.raises(OSError):
        await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True)
    assert await real_redis.xlen(_STREAM) == 1
    assert not json.loads(await real_redis.get(f"{_STATUS_PREFIX}t1")).get("archived")


async def test_pending_race_after_archive_is_rechecked_in_lua(real_redis, tmp_path):
    class RacingArchive(QueueArchive):
        async def put(self, stream, message_id, fields, status):
            await super().put(stream, message_id, fields, status)
            await real_redis.xclaim(stream, _GROUP, "late-consumer", 0, [message_id], force=True)
    archive = RacingArchive(tmp_path / "archive.db")
    await completed(real_redis, archive)
    result = await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True)
    assert result["archived"] == 1 and result["deleted"] == 0
    assert (await real_redis.xpending(_STREAM, _GROUP))["pending"] == 1
    assert await real_redis.xlen(_STREAM) == 1


async def test_malformed_dead_letter_and_source_are_archived_with_audit_group_guard(real_redis, tmp_path):
    archive = QueueArchive(tmp_path / "archive.db")
    queue = RedisStreamTaskQueue(real_redis, archive)
    await queue.ensure_group()
    message_id = await real_redis.xadd(_STREAM, {"payload": "broken-json"})
    await real_redis.xreadgroup(_GROUP, "worker", {_STREAM: ">"})
    await queue._handle_one(_STREAM, message_id, {"payload": "broken-json"}, None, 3)
    await real_redis.xgroup_create(_DEAD_STREAM, "reviewer", id="0")
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    dead = await real_redis.xreadgroup("reviewer", "review", {_DEAD_STREAM: ">"})
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    await real_redis.xack(_DEAD_STREAM, "reviewer", dead[0][1][0][0])
    result = await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True)
    assert result["deleted"] == 1
    assert await real_redis.xlen(_STREAM) == await real_redis.xlen(_DEAD_STREAM) == 0
    assert await archive.message_count() == 2


async def test_legacy_terminal_without_age_is_kept_and_missing_archive_fails_closed(real_redis, tmp_path):
    archive = QueueArchive(tmp_path / "archive.db")
    _, _, _ = await completed(real_redis, archive)
    key = f"{_STATUS_PREFIX}t1"
    data = json.loads(await real_redis.get(key))
    data.pop("terminal_at")
    await real_redis.set(key, json.dumps(data))
    assert (await maintain_queue(real_redis, archive, older_than_seconds=0, apply=True))["deleted"] == 0
    data["archived"] = True
    await real_redis.set(key, json.dumps(data))
    with pytest.raises(RuntimeError, match="归档丢失"):
        await RedisStreamTaskQueue(real_redis, archive).get_status("t1")
