# -*- coding: utf-8 -*-
"""真实隔离 Redis 验证共享网关配额；模型测试保留实际 SDK/包装器，替换网络边界。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.infrastructure.shared_throttle import (
    RedisGatewayThrottle, GatewayQuotaLeaseLost, GatewayQuotaTimeout, GatewayQuotaUnavailable,
)
from tests.test_model_cancellation import ControlledSdkModel, response
from tests.test_phase4_model import _build
from tests.test_queue_reliability import eventually, isolated_redis_url, real_redis


def throttle(client, *, scope="https://example.invalid/v1\nmodel-a", concurrency=2, interval=0, **kwargs):
    return RedisGatewayThrottle(client, concurrency, interval, namespace=scope,
        lease_ms=240, heartbeat_interval_ms=30, wait_timeout_seconds=2, **kwargs)


async def test_multiple_instances_share_global_concurrency(real_redis):
    instances = [throttle(real_redis) for _ in range(4)]
    active = peak = 0
    async def work(index):
        nonlocal active, peak
        async with instances[index % 4].slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.04)
            active -= 1
    await asyncio.gather(*(work(index) for index in range(12)))
    assert peak == 2
    assert await real_redis.zcard(instances[0]._slots_key) == 0
    assert "example.invalid" not in instances[0]._slots_key
    assert "model-a" not in instances[0]._slots_key


async def test_multiple_instances_share_start_spacing(real_redis):
    instances = [throttle(real_redis, concurrency=8, interval=0.05) for _ in range(3)]
    starts = []
    async def work(index):
        async with instances[index % 3].slot():
            starts.append(time.monotonic())
            await asyncio.sleep(0.001)
    await asyncio.gather(*(work(index) for index in range(6)))
    starts.sort()
    assert all(right - left >= 0.045 for left, right in zip(starts, starts[1:]))


async def test_different_gateway_model_scopes_have_independent_quota(real_redis):
    first = throttle(real_redis, scope="gateway\nmodel-a", concurrency=1)
    second = throttle(real_redis, scope="gateway\nmodel-b", concurrency=1)
    async with first.slot():
        async with second.slot():
            assert await real_redis.zcard(first._slots_key) == 1
            assert await real_redis.zcard(second._slots_key) == 1
    assert first._slots_key != second._slots_key


async def test_active_slot_is_renewed_until_released(real_redis):
    first, second = throttle(real_redis, concurrency=1), throttle(real_redis, concurrency=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def holder():
        async with first.slot():
            entered.set()
            await release.wait()
    async def follower():
        async with second.slot():
            return "acquired"
    owner = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 1)
    waiting = asyncio.create_task(follower())
    try:
        await asyncio.sleep(0.55)
        assert not waiting.done(), "活动流超过原始租期后仍应持续占用名额"
        assert await real_redis.zcard(first._slots_key) == 1
        release.set()
        await owner
        assert await asyncio.wait_for(waiting, 1) == "acquired"
    finally:
        release.set()
        await asyncio.gather(owner, waiting, return_exceptions=True)


async def test_expired_crashed_owner_does_not_hold_quota_forever(real_redis):
    first, second = throttle(real_redis, concurrency=1), throttle(real_redis, concurrency=1)
    abandoned = first.slot()
    await abandoned.__aenter__()
    # 停止心跳模拟占用者消失；不用 fake 时间或假 Redis 替代真实租约过期。
    abandoned._heartbeat_task.cancel()
    await asyncio.gather(abandoned._heartbeat_task, return_exceptions=True)
    await asyncio.sleep(0.27)
    async with second.slot():
        with pytest.raises(GatewayQuotaLeaseLost):
            await abandoned.__aexit__(None, None, None)
        assert await real_redis.zcard(second._slots_key) == 1, "旧 owner 释放不能移除新调用的名额"


@pytest.mark.parametrize("failure", ["removed", "redis-command-error"])
async def test_lease_loss_or_redis_failure_cancels_active_request(real_redis, failure):
    shared = throttle(real_redis, concurrency=1)
    entered = asyncio.Event()
    valid_in_cleanup = []
    slot = shared.slot()
    async def request():
        async with slot:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                valid_in_cleanup.append(slot.is_valid())
    runner = asyncio.create_task(request())
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "removed":
        await real_redis.delete(shared._slots_key)
    else:
        await real_redis.set(shared._slots_key, "错误类型用于触发真实 Redis 命令失败")
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, 1)
        assert valid_in_cleanup == [False]
    finally:
        await real_redis.delete(shared._slots_key)
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


async def test_redis_acquire_failure_is_closed_and_local_slot_recovers(real_redis):
    shared = throttle(real_redis, concurrency=1)
    await real_redis.set(shared._slots_key, "不是 sorted set")
    entered = False
    with pytest.raises(GatewayQuotaUnavailable):
        async with shared.slot():
            entered = True
    assert not entered
    await real_redis.delete(shared._slots_key)
    async with shared.slot():
        assert await real_redis.zcard(shared._slots_key) == 1


async def test_quota_wait_is_bounded_and_configuration_conflict_is_closed(real_redis):
    first = throttle(real_redis, concurrency=1)
    waiting = RedisGatewayThrottle(real_redis, 1, 0, namespace="https://example.invalid/v1\nmodel-a",
                                   wait_timeout_seconds=0.05)
    conflict = throttle(real_redis, concurrency=2)
    async with first.slot():
        with pytest.raises(GatewayQuotaTimeout):
            async with waiting.slot():
                pytest.fail("配额已占满，不应进入请求")
        with pytest.raises(GatewayQuotaUnavailable, match="配置不一致"):
            async with conflict.slot():
                pytest.fail("共享配额配置不同不能扩大容量")


async def test_actual_model_wrapper_keeps_global_slot_until_stream_end(real_redis):
    first = _build([("stream", 18)], throttle=throttle(real_redis, concurrency=1))
    second = _build(["next"], throttle=throttle(real_redis, concurrency=1))
    stream = await first([])
    # 模型已返回流对象，此时 Redis 名额必须仍被占用。
    assert await real_redis.zcard(first._throttle._slots_key) == 1
    waiting = asyncio.create_task(second([]))
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 18
    assert await asyncio.wait_for(waiting, 1) == "next"
    assert await real_redis.zcard(first._throttle._slots_key) == 0


async def test_stream_handoff_lost_lease_cancels_reader_and_closes_sdk_stream(real_redis):
    shared = throttle(real_redis, concurrency=1)
    reading, closed = asyncio.Event(), asyncio.Event()
    async def upstream():
        try:
            yield response("第一段")
            reading.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
    model = ControlledSdkModel([upstream(), response("下一次请求")])
    model._throttle = shared
    # 创建流的 task 已完成，消费在另一 task；失租不能只取消旧创建者。
    creator = asyncio.create_task(model([]))
    stream = await creator
    async def read():
        return [chunk async for chunk in stream]
    reader = asyncio.create_task(read())
    await asyncio.wait_for(reading.wait(), 1)
    await real_redis.delete(shared._slots_key)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reader, 1)
        assert creator.done() and not creator.cancelled()
        assert closed.is_set()
        assert (await asyncio.wait_for(model([]), 1)).content[0].text == "下一次请求"
        assert await real_redis.zcard(shared._slots_key) == 0
    finally:
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
