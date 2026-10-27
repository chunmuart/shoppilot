# -*- coding: utf-8 -*-
"""保留 OpenAI/AgentScope 的完整读流链，只在 HTTP 传输层注入可关闭 SSE 流。"""
from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest
from agentscope.credential import OpenAICredential

from app.infrastructure.llm import ThrottledChatModel
from app.infrastructure.shared_throttle import RedisGatewayThrottle
from app.infrastructure.throttle import GatewayThrottle
from tests.test_queue_reliability import eventually, isolated_redis_url, real_redis


class ControlledWireStream(httpx.AsyncByteStream):
    def __init__(self):
        self.reading = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.reading.set()
        await asyncio.Event().wait()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed.set()


def wire_model(shared, wire):
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, stream=wire))
    client = httpx.AsyncClient(transport=transport)
    model = ThrottledChatModel(credential=OpenAICredential(api_key="test-key", base_url="http://test/v1"),
        model="wire-test", throttle=shared, stream=True, max_retries=0, max_transient_retries=0,
        client_kwargs={"http_client": client, "max_retries": 0})
    return model


def throttle(client, concurrency=1):
    # SDK 首次格式化有同步加载，生命周期测试不使用仅 240ms 的故障注入租期。
    return RedisGatewayThrottle(client, concurrency, 0, namespace="wire-model-tests",
        lease_ms=3000, heartbeat_interval_ms=100, wait_timeout_seconds=2)


async def no_quota(shared, client):
    return await client.zcard(shared._slots_key) == 0


async def test_close_before_first_read_closes_real_sdk_wire_and_shared_slot(real_redis):
    shared, wire = throttle(real_redis, concurrency=1), ControlledWireStream()
    model = wire_model(shared, wire)
    try:
        stream = await asyncio.wait_for(model([]), 3)
        assert inspect.isasyncgen(stream), "AgentScope 以 inspect.isasyncgen 判断流式响应"
        assert not wire.reading.is_set(), "启用清理逻辑不应等待/预读第一个 token"
        assert await real_redis.zcard(shared._slots_key) == 1
        await stream.aclose()
        await stream.aclose()  # 显式关闭幂等，不重复释放本地并发容量。
        assert wire.closed.is_set()
        assert await no_quota(shared, real_redis)
        async with shared.slot():
            assert await real_redis.zcard(shared._slots_key) == 1
    finally:
        await model.client.close()


async def test_cancel_between_getting_stream_and_first_read_closes_without_gc(real_redis):
    shared, wire = throttle(real_redis, concurrency=1), ControlledWireStream()
    model = wire_model(shared, wire)
    obtained = asyncio.Event()
    retained = []
    async def request():
        retained.append(await model([]))
        obtained.set()
        await asyncio.Event().wait()
    request_task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(obtained.wait(), 1)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        await asyncio.wait_for(wire.closed.wait(), 1)
        await eventually(lambda: no_quota(shared, real_redis))
        assert not wire.reading.is_set()
        assert retained, "保留强引用，确保释放依靠显式取消清理而非垃圾回收"
        await retained[0].aclose()
    finally:
        if not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await model.client.close()


async def test_cancel_while_waiting_first_wire_token_closes_shared_slot(real_redis):
    shared, wire = throttle(real_redis, concurrency=1), ControlledWireStream()
    model = wire_model(shared, wire)
    async def request():
        stream = await model([])
        return [chunk async for chunk in stream]
    request_task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(wire.reading.wait(), 1)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        assert wire.closed.is_set()
        assert await no_quota(shared, real_redis)
    finally:
        if not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await model.client.close()


async def test_close_before_first_read_releases_local_slot_too():
    local, wire = GatewayThrottle(1, 0), ControlledWireStream()
    model = wire_model(local, wire)
    try:
        stream = await model([])
        await stream.aclose()
        assert wire.closed.is_set()
        async with asyncio.timeout(0.5):
            async with local.slot():
                pass
    finally:
        await model.client.close()
