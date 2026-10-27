# -*- coding: utf-8 -*-
"""真实 Redis + 轻量 API 容器验证排队身份和结果隔离，不调用模型。

Redis 进程由共享 fixture 隔离启动；缺少二进制会明确跳过。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.application.agents.orchestrator import SubmitIntentInput
from app.domain.queue.ports.task_queue import TaskStatus
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _STREAM
from app.presentation import server
from tests.test_queue_reliability import eventually, isolated_redis_url, real_redis


def intent(query="找一个周末旅行背包"):
    return SubmitIntentInput(shopping_session_id="api-session", buyer_id="api-buyer",
                             locale="zh-CN", currency="CNY", raw_query=query)


async def noop():
    pass


@pytest.fixture
def container(real_redis):
    return SimpleNamespace(task_queue=RedisStreamTaskQueue(real_redis), bus=TradeEventBus(),
        settings=SimpleNamespace(queue_priority_enabled=False, queue_wait_seconds=0.5),
        cache=SimpleNamespace(enabled=False), backplane=None, startup=noop, shutdown=noop)


@pytest.fixture
async def api_client(container, monkeypatch):
    async def build_container():
        return container
    monkeypatch.setattr(server, "build_container", build_container)
    monkeypatch.setattr(server, "build_container_origins", lambda: [])
    app = server.build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_explicit_request_id_reuses_task_and_binds_request_body(container, real_redis):
    ids = await asyncio.gather(*(server._enqueue(container, intent(), request_id="request-1") for _ in range(10)))
    assert len(set(ids)) == 1
    assert await real_redis.xlen(_STREAM) == 1
    with pytest.raises(ValueError, match="request_id"):
        await server._enqueue(container, intent("换成耳机"), request_id="request-1")
    assert await real_redis.xlen(_STREAM) == 1


async def test_same_query_without_request_id_is_a_new_submission(container, real_redis):
    first = await server._enqueue(container, intent())
    second = await server._enqueue(container, intent())
    assert first != second
    assert await real_redis.xlen(_STREAM) == 2


async def test_request_id_is_scoped_to_buyer_and_session(container, real_redis):
    first = await server._enqueue(container, intent(), request_id="request-1")
    buyer = await server._enqueue(container, replace(intent(), buyer_id="another-buyer"), request_id="request-1")
    session = await server._enqueue(container, replace(intent(), shopping_session_id="another-session"), request_id="request-1")
    assert len({first, buyer, session}) == 3
    assert await real_redis.xlen(_STREAM) == 3


@pytest.mark.parametrize("endpoint", ["/commerce/intents", "/commerce/intents/async"])
async def test_http_duplicate_request_returns_same_task_and_conflict_is_409(api_client, container, real_redis, endpoint):
    task_id = await server._enqueue(container, intent(), request_id="request-1")
    await container.task_queue.set_status(TaskStatus(task_id, "done", final_text="本次任务的结果"))
    body = {"shopping_session_id": "api-session", "buyer_id": "api-buyer", "locale": "zh-CN",
            "currency": "CNY", "raw_query": intent().raw_query, "request_id": "request-1"}
    repeated = await api_client.post(endpoint, json=body)
    assert repeated.status_code == 200
    if endpoint.endswith("/async"):
        assert repeated.json()["task_id"] == task_id
        assert repeated.json()["state"] == "done"
    else:
        assert repeated.json()["final_text"] == "本次任务的结果"
    conflict = await api_client.post(endpoint, json={**body, "raw_query": "请求 ID 相同但商品需求不同"})
    assert conflict.status_code == 409
    assert "request_id" in conflict.json()["detail"]
    assert await real_redis.xlen(_STREAM) == 1
    assert (await container.task_queue.get_status(task_id)).final_text == "本次任务的结果"


async def test_await_result_ignores_other_task_final_in_same_session(container):
    wanted = await server._enqueue(container, intent(), request_id="wanted")
    other = await server._enqueue(container, intent("另一件商品"), request_id="other")
    runner = asyncio.create_task(server._await_result(container, wanted, "api-session"))
    try:
        await eventually(lambda: bool(container.bus._subscribers.get("api-session")))
        await container.task_queue.set_status(TaskStatus(other, "done", final_text="另一任务的结果"))
        container.bus.publish("api-session", "final.result", {"task_id": other, "final_text": "另一任务的结果"})
        await asyncio.sleep(0.04)
        assert not runner.done(), "相同会话的 final.result 只能唤醒，不能直接用作当前任务的结果"
        await container.task_queue.set_status(TaskStatus(wanted, "done", final_text="当前任务的结果"))
        container.bus.publish("api-session", "final.result", {"task_id": wanted, "final_text": "事件内容也不能覆盖持久化结果"})
        assert await asyncio.wait_for(runner, 1) == "当前任务的结果"
        assert "api-session" not in container.bus._subscribers
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


async def test_await_result_failure_and_timeout_release_subscriptions(container):
    task_id = await server._enqueue(container, intent(), request_id="failed")
    await container.task_queue.set_status(TaskStatus(task_id, "failed", error="重投次数已用尽"))
    assert await server._await_result(container, task_id, "api-session") == "[error] 重投次数已用尽"
    waiting = await server._enqueue(container, intent(), request_id="waiting")
    container.settings.queue_wait_seconds = 0.03
    assert "处理超时" in await server._await_result(container, waiting, "api-session")
    assert "api-session" not in container.bus._subscribers
