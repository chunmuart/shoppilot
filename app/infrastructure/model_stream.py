# -*- coding: utf-8 -*-
"""模型流的显式资源生命周期，独立于异步生成器是否已读取第一个分片。"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
current_stream_finalizer: ContextVar["StreamFinalizer | None"] = ContextVar("model_stream_finalizer", default=None)
_cleanup_tasks: set[asyncio.Task] = set()


class StreamFinalizer:
    def __init__(self, slot: Any, charge: Callable[[Any], None]) -> None:
        self.slot = slot
        self.last: Any = None
        self._charge = charge
        self._resources: list[Any] = []
        self._closed = False
        self._owner_task: asyncio.Task | None = None

    def add(self, stream: Any) -> None:
        if not any(item is stream for item in self._resources):
            self._resources.append(stream)

    def bind_current_task(self) -> None:
        if self._closed:
            raise RuntimeError("模型流已经关闭")
        if self._owner_task is not None:
            self._owner_task.remove_done_callback(self._owner_done)
        self._owner_task = asyncio.current_task()
        if self._owner_task is not None:
            self._owner_task.add_done_callback(self._owner_done)
        bind = getattr(self.slot, "bind_current_task", None)
        if bind is not None:
            bind()

    def _owner_done(self, owner: asyncio.Task) -> None:
        # 创建者正常结束可能把流交给另一个 task；仅取消/异常结束自动关闭。
        failed = owner.cancelled() or owner.cancelling() > 0
        if not failed:
            failed = owner.exception() is not None
        if failed and not self._closed:
            task = asyncio.create_task(self.close((asyncio.CancelledError, asyncio.CancelledError(), None)))
            _cleanup_tasks.add(task)
            task.add_done_callback(_cleanup_tasks.discard)
            task.add_done_callback(self._report_cleanup)

    @staticmethod
    def _report_cleanup(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("取消后的模型流清理失败：%s", task.exception())

    async def close(self, error: tuple = (None, None, None)) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owner_task is not None:
            self._owner_task.remove_done_callback(self._owner_done)
        cleanup_error: BaseException | None = None
        # 从外层包装器向真实 HTTP stream 逐层关闭；SDK 未启动的生成器不会自动传递 close。
        for resource in reversed(self._resources):
            close = getattr(type(resource), "aclose", None) or getattr(type(resource), "close", None)
            if close is None:
                continue
            try:
                result = close(resource)
                if inspect.isawaitable(result):
                    await result
            except BaseException as err:
                cleanup_error = cleanup_error or err
        try:
            self._charge(self.last)
        except BaseException as err:
            # 观测/结算异常也不能阻止归还闸门名额。
            cleanup_error = cleanup_error or err
        try:
            await self.slot.__aexit__(*error)
        except BaseException as err:
            cleanup_error = cleanup_error or err
        if cleanup_error is not None:
            if error[1] is None:
                raise cleanup_error
            logger.warning("模型流清理失败，保留原始取消或异常：%s", cleanup_error)
