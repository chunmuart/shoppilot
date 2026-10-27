# -*- coding: utf-8 -*-
"""Redis 共享网关配额：并发租约和请求起点间隔在同一 Lua 中判定。

只在明确没有 Redis 的部署使用本地 GatewayThrottle；共享模式故障时拒绝请求，
不能退回每个 worker 各自拥有一份配额。该租约独立于购物会话执行锁。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
import uuid
from typing import Any

from app.infrastructure.throttle import GatewayThrottle

logger = logging.getLogger(__name__)

_ACQUIRE = """
local time = redis.call('TIME')
local now = time[1] * 1000 + math.floor(time[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local config = redis.call('GET', KEYS[3])
if config and config ~= ARGV[5] then return {-2, 0} end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, math.max(1, tonumber(first[2]) - now)}
end
local last = tonumber(redis.call('GET', KEYS[2]) or '0')
if last + tonumber(ARGV[3]) > now then return {0, last + tonumber(ARGV[3]) - now} end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[4]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[6])
redis.call('SET', KEYS[2], now, 'PX', ARGV[6])
redis.call('SET', KEYS[3], ARGV[5], 'PX', ARGV[6])
return {1, tonumber(ARGV[4])}
"""
_RENEW = """
local time = redis.call('TIME')
local now = time[1] * 1000 + math.floor(time[2] / 1000)
local expires = tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1]) or '0')
if expires <= now then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[3] then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
redis.call('PEXPIRE', KEYS[2], ARGV[4])
return 1
"""
_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('ZCARD', KEYS[1]) == 0 then redis.call('DEL', KEYS[1]) end
return 1
"""


class GatewayQuotaUnavailable(RuntimeError):
    """共享配额状态不可确认，拒绝无保护调用上游。"""


class GatewayQuotaLeaseLost(GatewayQuotaUnavailable):
    """配额租约失效，当前模型请求必须中止。"""


class GatewayQuotaTimeout(TimeoutError):
    """配额等待达到有限截止时间。"""


class RedisGatewayThrottle(GatewayThrottle):
    """同网关/模型的实例传入相同 namespace，Redis key 仅保留其散列。"""

    def __init__(self, client: Any, max_concurrency: int, min_interval_seconds: float, *,
                 namespace: str, lease_ms: int = 30000, heartbeat_interval_ms: int | None = None,
                 wait_timeout_seconds: float = 120.0, operation_timeout_seconds: float = 5.0) -> None:
        if max_concurrency < 1 or min_interval_seconds < 0 or not math.isfinite(min_interval_seconds):
            raise ValueError("共享网关并发度须为正，起点间隔须为非负有限数")
        heartbeat_ms = heartbeat_interval_ms if heartbeat_interval_ms is not None else lease_ms // 3
        if lease_ms < 100 or not 0 < heartbeat_ms < lease_ms:
            raise ValueError("租期至少 100ms，心跳间隔须为正且小于租期")
        if not namespace or wait_timeout_seconds <= 0 or operation_timeout_seconds <= 0:
            raise ValueError("配额 namespace、等待截止时间和操作超时必须有效")
        super().__init__(max_concurrency, min_interval_seconds)
        self._client = client
        self._max_concurrency = max_concurrency
        self._interval_ms = math.ceil(min_interval_seconds * 1000)
        self._lease_ms = lease_ms
        self._heartbeat_ms = heartbeat_ms
        self._wait_timeout = wait_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._key_ttl_ms = max(lease_ms * 2, self._interval_ms * 2 + 1000)
        self._config = f"{max_concurrency}:{self._interval_ms}"
        # 不把网关 URL、模型名、API key 写进可枚举的 Redis key；同 hash tag 可放同一槽。
        scope = hashlib.sha256(namespace.encode()).hexdigest()
        prefix = f"shoppilot:quota:{{{scope}}}"
        self._slots_key = f"{prefix}:slots"
        self._last_key = f"{prefix}:last"
        self._config_key = f"{prefix}:config"

    def slot(self) -> "SharedQuotaSlot":
        return SharedQuotaSlot(self)


class SharedQuotaSlot:
    """可将持有权从模型创建协程移交给真正消费流的协程。"""

    def __init__(self, throttle: RedisGatewayThrottle) -> None:
        self._throttle = throttle
        self._owner = uuid.uuid4().hex
        self._owner_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._expires_at = 0.0
        self._valid = False
        self._local_acquired = False
        self._closed = False

    def is_valid(self) -> bool:
        return self._valid and not self._closed and time.monotonic() < self._expires_at

    def bind_current_task(self) -> None:
        """在开始读流时调用；丢租时应取消读流 task，不能误取消原来的创建 task。"""
        if not self.is_valid():
            raise GatewayQuotaLeaseLost("共享模型配额租约已失效")
        self._owner_task = asyncio.current_task()

    async def __aenter__(self) -> None:
        t = self._throttle
        deadline = time.monotonic() + t._wait_timeout
        try:
            await asyncio.wait_for(t._semaphore.acquire(), t._wait_timeout)
            self._local_acquired = True
            while True:
                started = time.monotonic()
                remaining = deadline - started
                if remaining <= 0:
                    raise GatewayQuotaTimeout("共享模型配额等待超时，请稍后重试")
                try:
                    response = await asyncio.wait_for(t._client.eval(_ACQUIRE, 3,
                        t._slots_key, t._last_key, t._config_key, self._owner,
                        t._max_concurrency, t._interval_ms, t._lease_ms, t._config, t._key_ttl_ms),
                        min(remaining, t._operation_timeout, t._lease_ms / 1000))
                except Exception as err:
                    raise GatewayQuotaUnavailable("无法确认共享模型配额，已停止请求") from err
                if int(response[0]) == -2:
                    raise GatewayQuotaUnavailable("同一网关配额范围的并发/间隔配置不一致")
                if int(response[0]) == 1:
                    self._expires_at = started + t._lease_ms / 1000
                    self._valid = True
                    self.bind_current_task()
                    self._heartbeat_task = asyncio.create_task(self._heartbeat())
                    return None
                await asyncio.sleep(min(0.1, max(0.005, int(response[1]) / 1000), remaining))
        except asyncio.TimeoutError as err:
            await self._close()
            raise GatewayQuotaTimeout("共享模型配额等待超时，请稍后重试") from err
        except BaseException:
            await self._close()
            raise

    async def _heartbeat(self) -> None:
        t = self._throttle
        try:
            while self._valid:
                await asyncio.sleep(t._heartbeat_ms / 1000)
                started = time.monotonic()
                if started >= self._expires_at:
                    raise GatewayQuotaLeaseLost("模型配额续租前租约已过期")
                renewed = await asyncio.wait_for(t._client.eval(_RENEW, 2,
                    t._slots_key, t._config_key, self._owner, t._lease_ms, t._config, t._key_ttl_ms),
                    min(t._operation_timeout, self._expires_at - started))
                if not renewed:
                    raise GatewayQuotaLeaseLost("共享模型配额所有权已丢失")
                self._expires_at = started + t._lease_ms / 1000
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._valid = False
            logger.warning("共享模型配额失效，中止上游请求：%s", type(err).__name__)
            if self._owner_task is not None and not self._owner_task.done():
                self._owner_task.cancel()

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        lost = not self.is_valid()
        await self._close()
        if exc_type is None and lost:
            raise GatewayQuotaLeaseLost("共享模型配额已失效，不能把请求标记为成功")
        return False

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._valid = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        t = self._throttle
        try:
            # 即使 acquire 响应丢失，也用唯一 owner 尝试释放，不会移除其他调用的名额。
            await asyncio.wait_for(t._client.eval(_RELEASE, 1, t._slots_key, self._owner),
                                   min(2.0, t._operation_timeout))
        except Exception as err:
            logger.warning("共享模型配额释放失败，等待租约到期：%s", type(err).__name__)
        finally:
            if self._local_acquired:
                self._local_acquired = False
                t._semaphore.release()
