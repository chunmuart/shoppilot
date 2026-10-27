# -*- coding: utf-8 -*-
"""启动隔离的真实 Redis 进程验证队列；找不到二进制时明确跳过，不用替身冒充。

SHOPPILOT_REDIS_SERVER_BIN=/path/to/redis-server uv run pytest -q tests/test_queue_reliability.py
只清理本测试新建的 Unix socket Redis，绝不连接或清空用户已有 Redis 实例。
"""
from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest
import redis
import redis.asyncio as aioredis

from app.domain.queue.ports.task_queue import IntentTask, TaskStatus
from app.infrastructure.queue.redis_stream_queue import (
    RedisStreamTaskQueue, SessionLeaseTimeout, _STREAM, _LARGE_STREAM,
    _DEAD_STREAM, _GROUP, _STATUS_PREFIX, current_execution_lease,
)


@pytest.fixture(scope="module")
def isolated_redis_url():
    binary = os.environ.get("SHOPPILOT_REDIS_SERVER_BIN") or shutil.which("redis-server")
    if not binary:
        pytest.skip("真实 Redis 测试需要 redis-server；设置 SHOPPILOT_REDIS_SERVER_BIN 后重跑")
    with tempfile.TemporaryDirectory(prefix="gbx-redis-", dir="/tmp") as directory:
        socket = Path(directory) / "redis.sock"
        with open(Path(directory) / "redis.log", "w+") as log:
            process = subprocess.Popen([binary, "--port", "0", "--unixsocket", str(socket),
                "--unixsocketperm", "700", "--save", "", "--appendonly", "no", "--dir", directory],
                stdout=log, stderr=subprocess.STDOUT)
            probe = redis.Redis(unix_socket_path=str(socket), socket_timeout=0.2)
            try:
                deadline = time.monotonic() + 8
                while True:
                    try:
                        if probe.ping():
                            break
                    except redis.RedisError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            log.seek(0)
                            pytest.fail(f"真实 Redis 启动失败：{log.read()}")
                        time.sleep(0.02)
                yield f"unix://{socket}"
            finally:
                probe.close()
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)


@pytest.fixture
async def real_redis(isolated_redis_url):
    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    # 该 URL 来自上面新建的隔离进程，不接受外部服务 URL。
    await client.flushdb()
    yield client
    await client.aclose()


def task(task_id="task-1", *, session="session-1", priority=0):
    return IntentTask(task_id, session, "buyer-1", "zh-CN", "CNY", "查询旅行背包", priority=priority)


async def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if hasattr(value, "__await__"):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError("等待真实 Redis 状态变化超时")
        await asyncio.sleep(0.01)


def consume(queue, handler, stopping, *, consumer="test-worker", concurrency=1, **kwargs):
    return asyncio.create_task(queue.consume(consumer, handler, stopping.is_set, block_ms=10,
        concurrency=concurrency, reclaim_idle_ms=120, lease_ms=600, heartbeat_interval_ms=30, **kwargs),
        context=Context())


async def stop_consumer(runner, stopping):
    stopping.set()
    await asyncio.wait_for(runner, 3)


async def has_state(queue, task_id, state):
    status = await queue.get_status(task_id)
    return status is not None and status.state == state


async def test_enqueue_is_atomic_deduplicated_and_payload_bound(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    original = task()
    await asyncio.gather(*(queue.enqueue(replace(original, enqueued_at=str(index))) for index in range(12)))
    assert await real_redis.xlen(_STREAM) == 1
    assert (await queue.get_status(original.task_id)).state == "queued"
    # 传输重试可产生新 trace，不能把观测关联字段误当作不同选购内容。
    await queue.enqueue(replace(original, traceparent="00-" + "1" * 32 + "-" + "2" * 16 + "-01",
                                tracestate="vendor=retry", request_id="client-submit-1"))
    assert await real_redis.xlen(_STREAM) == 1
    with pytest.raises(ValueError, match="request_id"):
        await queue.enqueue(replace(original, raw_query="完全不同的商品需求"))
    await queue.set_status(TaskStatus(original.task_id, "running"))
    await queue.set_status(TaskStatus(original.task_id, "queued"))
    assert (await queue.get_status(original.task_id)).state == "running"
    await queue.set_status(TaskStatus(original.task_id, "done", final_text="已完成"))
    await queue.set_status(TaskStatus(original.task_id, "queued"))
    assert (await queue.get_status(original.task_id)).final_text == "已完成"
    assert await real_redis.ttl(f"{_STATUS_PREFIX}{original.task_id}") == -1
    await queue.enqueue(original)
    assert await real_redis.xlen(_STREAM) == 1
    with pytest.raises(ValueError):
        await queue.enqueue(replace(original, raw_query="另一段需求"))


async def test_dual_streams_obey_global_concurrency_limit(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    for index in range(8):
        await queue.enqueue(task(f"task-{index}", session=f"s-{index}", priority=index % 2))
    active = peak = completed = 0
    async def handler(_task):
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        completed += 1
        return "ok"
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping, concurrency=2)
    try:
        await eventually(lambda: completed == 8)
    finally:
        await stop_consumer(runner, stopping)
    assert peak == 2
    assert (await real_redis.xpending(_STREAM, _GROUP))["pending"] == 0
    assert (await real_redis.xpending(_LARGE_STREAM, _GROUP))["pending"] == 0


async def test_enqueue_stream_error_does_not_leave_a_phantom_queued_status(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await real_redis.set(_STREAM, "不是 Redis Stream")
    with pytest.raises(redis.ResponseError):
        await queue.enqueue(task())
    assert await queue.get_status("task-1") is None
    await real_redis.delete(_STREAM)
    await queue.enqueue(task())
    assert await real_redis.xlen(_STREAM) == 1
    assert (await queue.get_status("task-1")).state == "queued"


async def test_consume_reclaims_both_streams_after_worker_loss(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.ensure_group()
    for index in range(2):
        await queue.enqueue(task(f"recover-{index}", session=f"s-{index}", priority=index))
    for stream in (_STREAM, _LARGE_STREAM):
        await real_redis.xreadgroup(_GROUP, "dead-worker", {stream: ">"}, count=1)
    await asyncio.sleep(0.15)
    received = []
    async def handler(_task):
        received.append(current_execution_lease().delivery)
        return "恢复成功"
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping, concurrency=2)
    try:
        await eventually(lambda: len(received) == 2)
    finally:
        await stop_consumer(runner, stopping)
    assert {entry.stream for entry in received} == {_STREAM, _LARGE_STREAM}
    assert all(entry.message_id and entry.deliveries == 2 for entry in received)
    assert (await queue.get_status("recover-1")).stream == _LARGE_STREAM


async def test_failure_retries_then_atomic_dead_letter(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(task())
    calls = 0
    async def handler(_task):
        nonlocal calls
        calls += 1
        raise RuntimeError("模型暂时不可用")
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping, max_deliveries=3)
    try:
        await eventually(lambda: has_state(queue, "task-1", "failed"))
    finally:
        await stop_consumer(runner, stopping)
    assert calls == 3
    dead = await real_redis.xrange(_DEAD_STREAM)
    assert len(dead) == 1
    payload = dead[0][1]
    assert payload["stream"] == _STREAM and payload["message_id"]
    assert payload["deliveries"] == "3" and payload["task_id"] == "task-1"
    assert (await real_redis.xpending(_STREAM, _GROUP))["pending"] == 0
    assert (await queue.get_status("task-1")).error == "模型暂时不可用"


async def test_cancelled_consumer_leaves_pending_for_recovery(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(task())
    entered = asyncio.Event()
    async def handler(_task):
        entered.set()
        await asyncio.Event().wait()
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping)
    await asyncio.wait_for(entered.wait(), 2)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert (await real_redis.xpending(_STREAM, _GROUP))["pending"] == 1
    assert (await queue.get_status("task-1")).state != "done"
    await asyncio.sleep(0.15)
    recovered = consume(queue, lambda _: asyncio.sleep(0, result="恢复后完成"), stopping, consumer="new-worker")
    try:
        await eventually(lambda: has_state(queue, "task-1", "done"))
    finally:
        await stop_consumer(recovered, stopping)


async def test_heartbeat_prevents_active_task_reclaim(real_redis):
    first, second = RedisStreamTaskQueue(real_redis), RedisStreamTaskQueue(real_redis)
    await first.enqueue(task())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def slow(_task):
        calls.append("first")
        entered.set()
        await release.wait()
        return "done"
    async def duplicate(_task):
        calls.append("second")
    stop_a, stop_b = asyncio.Event(), asyncio.Event()
    a = consume(first, slow, stop_a, consumer="a")
    await asyncio.wait_for(entered.wait(), 2)
    b = consume(second, duplicate, stop_b, consumer="b")
    try:
        await asyncio.sleep(0.42)
        assert calls == ["first"]
        pending = await real_redis.xpending_range(_STREAM, _GROUP, "-", "+", 10)
        assert pending[0]["times_delivered"] == 1
        assert pending[0]["consumer"] == "a"
    finally:
        release.set()
        await stop_consumer(a, stop_a)
        await stop_consumer(b, stop_b)


async def test_two_workers_serialize_same_session(real_redis):
    queues = [RedisStreamTaskQueue(real_redis), RedisStreamTaskQueue(real_redis)]
    await queues[0].enqueue(task("first"))
    await queues[0].enqueue(task("second", priority=1))
    active = peak = completed = 0
    async def handler(_task):
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.15)
        active -= 1
        completed += 1
        return "ok"
    stops = [asyncio.Event(), asyncio.Event()]
    runners = [consume(q, handler, stop, consumer=f"worker-{i}") for i, (q, stop) in enumerate(zip(queues, stops))]
    try:
        await eventually(lambda: completed == 2)
    finally:
        for runner, stop in zip(runners, stops):
            await stop_consumer(runner, stop)
    assert peak == 1


async def test_api_session_lease_and_worker_share_exclusion(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    entered = asyncio.Event()
    async def handler(_task):
        entered.set()
        return "ok"
    stopping = asyncio.Event()
    async with queue.session_lease("session-1", lease_ms=300) as lease:
        assert lease.is_valid()
        async with queue.session_lease("session-1") as reused:
            assert reused is lease
        await queue.enqueue(task())
        runner = consume(queue, handler, stopping)
        await asyncio.sleep(0.2)
        assert not entered.is_set()
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        await stop_consumer(runner, stopping)


async def test_lost_lease_invalidates_guard_before_cancelling_handler(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(task())
    entered, cancelled, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    guards = []
    async def handler(_task):
        lease = current_execution_lease()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            guards.append(lease.is_valid())
            cancelled.set()
            stopping.set()
    runner = consume(queue, handler, stopping)
    await asyncio.wait_for(entered.wait(), 2)
    keys = await real_redis.keys("shoppilot:lease:session:*")
    await real_redis.set(keys[0], "another-owner", px=1000)
    await asyncio.wait_for(cancelled.wait(), 2)
    await asyncio.wait_for(runner, 2)
    assert guards == [False]
    assert (await real_redis.xpending(_STREAM, _GROUP))["pending"] == 1
    assert (await queue.get_status("task-1")).state != "done"
    assert await real_redis.get(keys[0]) == "another-owner", "旧 worker 不得删除新 owner 的租约"


async def test_duplicate_done_task_is_acked_without_handler_replay(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.ensure_group()
    original = task()
    await queue.enqueue(original)
    await queue.set_status(TaskStatus(original.task_id, "done", final_text="权威结果"))
    await real_redis.xadd(_LARGE_STREAM, {"payload": json.dumps(original.to_dict())})
    calls = []
    async def handler(_task):
        calls.append(_task.task_id)
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping, concurrency=2)
    try:
        await eventually(lambda: _no_backlog(queue))
    finally:
        await stop_consumer(runner, stopping)
    assert calls == []
    assert (await queue.get_status("task-1")).final_text == "权威结果"


async def _no_backlog(queue):
    return await queue.depth() == 0


async def test_duplicate_in_flight_task_executes_once(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    await queue.ensure_group()
    original = task()
    await queue.enqueue(original)
    await real_redis.xadd(_LARGE_STREAM, {"payload": json.dumps(original.to_dict())})
    calls = []
    async def handler(_task):
        calls.append(_task.task_id)
        await asyncio.sleep(0.16)
        return "only-once"
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping, concurrency=2)
    try:
        await eventually(lambda: _no_backlog(queue))
    finally:
        await stop_consumer(runner, stopping)
    assert calls == ["task-1"]


async def test_malformed_message_dead_letter_preserves_source(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    message_id = await real_redis.xadd(_LARGE_STREAM, {"payload": "not-json"})
    async def forbidden(_task):
        raise AssertionError("不应执行坏消息")
    stopping = asyncio.Event()
    runner = consume(queue, forbidden, stopping)
    try:
        await eventually(lambda: real_redis.xlen(_DEAD_STREAM))
    finally:
        await stop_consumer(runner, stopping)
    dead = (await real_redis.xrange(_DEAD_STREAM))[0][1]
    assert dead["stream"] == _LARGE_STREAM and dead["message_id"] == message_id
    assert (await real_redis.xpending(_LARGE_STREAM, _GROUP))["pending"] == 0


async def test_worker_error_result_is_failed_not_done_and_uses_fresh_state(real_redis):
    from app.worker import execute_intent_task
    queue = RedisStreamTaskQueue(real_redis)
    await queue.enqueue(task())
    observed = []
    class Orchestrator:
        async def handle_intent(self, _intent, **kwargs):
            observed.append((kwargs["fresh_session"], kwargs["persistence_guard"]()))
            return SimpleNamespace(error="结果明确失败", final_text="[error] 结果明确失败")
    class Bus:
        def publish(self, *_args):
            pass
    async def handler(intent):
        return await execute_intent_task(intent, Orchestrator(), Bus())
    stopping = asyncio.Event()
    runner = consume(queue, handler, stopping, max_deliveries=1)
    try:
        await eventually(lambda: has_state(queue, "task-1", "failed"))
    finally:
        await stop_consumer(runner, stopping)
    assert observed == [(True, True)]
    assert (await queue.get_status("task-1")).error == "结果明确失败"


async def test_session_lease_wait_is_bounded(real_redis):
    queue = RedisStreamTaskQueue(real_redis)
    async def competing():
        with pytest.raises(SessionLeaseTimeout):
            async with queue.session_lease("session-1", wait_timeout=0.05, lease_ms=300):
                raise AssertionError("不能抢占有效会话租约")
    async with queue.session_lease("session-1", lease_ms=300):
        await asyncio.create_task(competing(), context=Context())
