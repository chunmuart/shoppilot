# -*- coding: utf-8 -*-
"""worker：意图任务消费进程

用法：
    uv run python -m app.worker

与 API 进程共用同一个装配容器（app/composition.py），差别只在于：
API 进程负责收请求、入队、等结果；worker 进程负责把队列里的意图真正跑完。

削峰的实质是「worker 并发度」限制了同时在跑的 Agent 轮次，
与接口是不是同步无关。

优雅退出：收到 SIGTERM/SIGINT 后停止领新消息，等在途任务跑完再退；
未 ack 的消息由 Redis Stream 的 pending 机制重投，不会丢。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid

from app.application.agents.orchestrator import SubmitIntentInput
from app.composition import build_container
from app.domain.queue.ports.task_queue import IntentTask
from app.infrastructure.queue.redis_stream_queue import current_execution_lease
from app.infrastructure.tracing import install_log_correlation, trace_worker_task

install_log_correlation()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s request=%(request_id)s task=%(task_id)s session=%(session_id)s trace=%(trace_id)s prompt=%(prompt_version)s %(message)s")
logger = logging.getLogger("app.worker")


async def execute_intent_task(task: IntentTask, orchestrator, bus) -> str:
    """只执行意图；running/retrying/终态与 ACK 由持有租约的队列原子治理。"""
    with trace_worker_task(task):
        return await _execute_traced_task(task, orchestrator, bus)


async def _execute_traced_task(task: IntentTask, orchestrator, bus) -> str:
    lease = current_execution_lease()
    if lease is None or not lease.is_valid():
        raise RuntimeError("worker 未持有有效执行租约")
    bus.publish(task.shopping_session_id, "task.started", {"task_id": task.task_id})
    result = await orchestrator.handle_intent(
        SubmitIntentInput(
            shopping_session_id=task.shopping_session_id,
            buyer_id=task.buyer_id,
            locale=task.locale,
            currency=task.currency,
            raw_query=task.raw_query,
        ),
        fresh_session=True,
        persistence_guard=lease.is_valid,
    )
    # Orchestrator 会把常规异常封装成结果，不能把 [error] 误标成 done。
    if result.error:
        raise RuntimeError(result.error)
    return result.final_text


async def main() -> None:
    container = await build_container()
    if container.task_queue is None:
        raise RuntimeError("未启用队列（需配置 REDIS_URL 且 QUEUE_ENABLED 不为 0），worker 无事可做")

    consumer_name = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
    stopping = asyncio.Event()
    in_flight = 0

    def request_stop(*_args) -> None:
        if not stopping.is_set():
            logger.info("收到退出信号，停止领取新任务（在途 %d 个跑完后退出）", in_flight)
            stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop)

    async def handle(task: IntentTask) -> str:
        nonlocal in_flight
        in_flight += 1
        try:
            return await execute_intent_task(task, container.orchestrator, container.bus)
        finally:
            in_flight -= 1

    logger.info(
        "worker 启动：%s（并发度 %d）", consumer_name, container.settings.worker_concurrency,
    )
    await container.startup()
    try:
        await container.task_queue.consume(
            consumer_name=consumer_name,
            handler=handle,
            should_stop=stopping.is_set,
            concurrency=container.settings.worker_concurrency,
        )
    finally:
        logger.info("worker 退出：%s", consumer_name)
        await container.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
