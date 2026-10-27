# -*- coding: utf-8 -*-
"""用真实 AgentScope 响应与包装器验证取消，网络边界使用本地桩。"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock
from agentscope.model import ChatResponse, FinishedReason

from app.infrastructure.llm import ThrottledChatModel
from app.infrastructure.throttle import GatewayThrottle


class ControlledSdkModel(ThrottledChatModel):
    """仅替换底层 API；保留 SDK 捕获取消并返回 INTERRUPTED 的实际行为。"""

    def __init__(self, behaviors):
        super().__init__(
            credential=OpenAICredential(api_key="test-key", base_url="http://127.0.0.1:9/v1"),
            model="local-test", throttle=GatewayThrottle(max_concurrency=1, min_interval_seconds=0),
            max_transient_retries=0, max_retries=0,
        )
        self.behaviors = list(behaviors)
        self.calls = 0

    async def _call_api(self, model, **kwargs):
        self.calls += 1
        value = self.behaviors.pop(0)
        return await value() if callable(value) else value


def response(text="已完成"):
    return ChatResponse(content=[TextBlock(text=text)], is_last=True)


async def test_real_nonstream_response_does_not_probe_missing_attributes_and_releases_slot():
    first, second = response(), response("下一轮")
    model = ControlledSdkModel([first, second])
    assert await model([]) is first
    assert await asyncio.wait_for(model([]), timeout=0.5) is second


async def test_request_cancellation_swallowed_by_sdk_is_restored_and_slot_released():
    started = asyncio.Event()

    async def blocked_api():
        started.set()
        await asyncio.Event().wait()

    next_response = response("下一轮")
    model = ControlledSdkModel([blocked_api, next_response])
    request = asyncio.create_task(model([]))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    request.cancel("用户停止，timeout 不应触发重试")
    with pytest.raises(asyncio.CancelledError):
        await request
    assert model.calls == 1
    assert await asyncio.wait_for(model([]), timeout=0.5) is next_response


async def test_stream_cancellation_does_not_yield_fake_final_response_and_releases_slot():
    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def chunks():
        try:
            yield ChatResponse(content=[TextBlock(text="草稿")], is_last=False)
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    next_response = response("下一轮")
    model = ControlledSdkModel([chunks(), next_response])
    received = []

    async def consume():
        stream = await model([])
        async for chunk in stream:
            received.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(waiting.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert len(received) == 1
    assert not any(chunk.finished_reason == FinishedReason.INTERRUPTED for chunk in received)
    assert await asyncio.wait_for(model([]), timeout=0.5) is next_response


async def test_explicit_interrupted_response_cannot_be_treated_as_success():
    model = ControlledSdkModel([
        ChatResponse(content=[], is_last=True, finished_reason=FinishedReason.INTERRUPTED),
        response(),
    ])
    with pytest.raises(asyncio.CancelledError):
        await model([])
    assert (await asyncio.wait_for(model([]), timeout=0.5)).is_last


async def test_cancel_message_matching_transient_error_is_never_retried_or_fallback(monkeypatch):
    model = ControlledSdkModel([])
    model._max_transient_retries = 2
    model._retry_base_seconds = 0
    fallback = AsyncMock(return_value=response())
    fallback.model = "fallback-test"
    model._fallback = fallback
    calls = 0

    async def cancelled(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError("timeout 429")

    monkeypatch.setattr(model, "_invoke_upstream", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await model([])
    assert calls == 1
    fallback.assert_not_awaited()


async def test_closing_started_stream_closes_upstream_before_releasing_slot(monkeypatch):
    closed = asyncio.Event()

    async def chunks():
        try:
            yield response("第一块")
            yield response("不会消费")
        finally:
            closed.set()

    stream = chunks()
    model = ControlledSdkModel([response("下一轮")])

    async def upstream(*args, **kwargs):
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(model, "_invoke_upstream", upstream)
        wrapped = await model([])
        await anext(wrapped)
        await wrapped.aclose()
    assert closed.is_set(), "不能只归还闸门而把上游流留到垃圾回收时再关闭"
    assert (await asyncio.wait_for(model([]), timeout=0.5)).is_last
