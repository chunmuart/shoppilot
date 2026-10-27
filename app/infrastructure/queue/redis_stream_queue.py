# -*- coding: utf-8 -*-
"""Redis Stream 可靠消费与跨进程事件背板。

处理任务前同时持有消息、task 与会话租约；只有租约有效且结果成功才确认。
重投语义为 at-least-once，业务写入仍须使用独立的确认及幂等约束。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import logging
from app.infrastructure.operational_metrics import observe_queue
import os
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from app.domain.queue.ports.task_queue import IntentTask, QueueDelivery, TaskQueue, TaskStatus
from app.infrastructure.eventbus import TradeEvent

logger = logging.getLogger(__name__)
_STREAM = "shoppilot:intents"
_LARGE_STREAM = "shoppilot:intents:large"
_DEAD_STREAM = "shoppilot:intents:dead"
_GROUP = "shoppilot-workers"
_STATUS_PREFIX = "shoppilot:task:"
_STATUS_TTL = 3600
_EVENT_CHANNEL_PREFIX = "shoppilot:events:"
_LEASE_PREFIX = "shoppilot:lease:"

_ENQUEUE_SCRIPT = """
local old = redis.call('GET', KEYS[1])
if old then
  if cjson.decode(old).payload_fingerprint ~= ARGV[4] then return -1 end
  return 0
end
local initial = cjson.decode(ARGV[1])
initial.payload_fingerprint = ARGV[4]
redis.call('XADD', KEYS[2], '*', 'payload', ARGV[2])
redis.call('SET', KEYS[1], cjson.encode(initial), 'EX', ARGV[3])
return 1
"""
_STATUS_SCRIPT = """
for i = 2, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[4] then return -1 end
end
local old = redis.call('GET', KEYS[1])
local updated = cjson.decode(ARGV[1])
if old then
  updated.payload_fingerprint = cjson.decode(old).payload_fingerprint
  local state = cjson.decode(old).state
  if state == 'done' or state == 'failed' then return 0 end
  if ARGV[2] == 'queued' and state ~= 'queued' then return 0 end
end
if ARGV[2] == 'done' or ARGV[2] == 'failed' then
  local now = redis.call('TIME')
  updated.terminal_at = tonumber(now[1]) + tonumber(now[2]) / 1000000
  redis.call('SET', KEYS[1], cjson.encode(updated))
else
  redis.call('SET', KEYS[1], cjson.encode(updated), 'EX', ARGV[3])
end
return 1
"""
_RENEW_SCRIPT = """
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[1] then return 0 end
end
if ARGV[3] ~= '' then
  local ids = redis.call('XCLAIM', ARGV[3], ARGV[4], ARGV[5], 0, ARGV[6], 'JUSTID')
  if #ids == 0 then return 0 end
end
for i = 1, #KEYS do redis.call('PEXPIRE', KEYS[i], ARGV[2]) end
return 1
"""
_RELEASE_SCRIPT = """
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then redis.call('DEL', KEYS[i]) end
end
return 1
"""
_ACK_SCRIPT = """
for i = 2, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[3] then return -1 end
end
return redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
"""
_DEAD_SCRIPT = """
local added = 0
for i = 4, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[8] then return -1 end
end
if redis.call('EXISTS', KEYS[3]) == 0 then
  local old = redis.call('GET', KEYS[2])
  if not old or cjson.decode(old).state ~= 'done' then
    redis.call('XADD', ARGV[1], '*', 'payload', ARGV[2], 'reason', ARGV[3],
      'stream', KEYS[1], 'message_id', ARGV[4], 'deliveries', ARGV[5], 'task_id', ARGV[6])
    added = 1
    if ARGV[6] ~= '' then
      local failed = cjson.decode(ARGV[7])
      if old then failed.payload_fingerprint = cjson.decode(old).payload_fingerprint end
      local now = redis.call('TIME')
      failed.terminal_at = tonumber(now[1]) + tonumber(now[2]) / 1000000
      redis.call('SET', KEYS[2], cjson.encode(failed))
    end
  end
  redis.call('SET', KEYS[3], '1')
end
redis.call('XACK', KEYS[1], ARGV[9], ARGV[4])
return added
"""


class QueueLeaseLost(RuntimeError):
    """执行权已过期，后续任务状态与会话快照不得提交。"""


class SessionLeaseTimeout(TimeoutError):
    """同一会话的前一个执行仍未结束。"""


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _lease_key(kind: str, identifier: str) -> str:
    digest = hashlib.sha256(identifier.encode()).hexdigest()
    return f"{_LEASE_PREFIX}{kind}:{digest}"


@dataclass
class ExecutionLease:
    session_id: str
    owner: str
    lease_ms: int
    consumer_name: str = ""
    delivery: QueueDelivery | None = None
    keys: list[str] = field(default_factory=list)
    expires_at: float = 0.0
    valid: bool = True
    ready: bool = False

    def is_valid(self) -> bool:
        """同步持久化护栏；deadline 从请求发出时计算，避免网络延迟虚增租期。"""
        return self.valid and self.ready and time.monotonic() < self.expires_at

    def invalidate(self) -> None:
        self.valid = False


_execution_lease: ContextVar[ExecutionLease | None] = ContextVar("shoppilot_execution_lease", default=None)


def current_execution_lease() -> ExecutionLease | None:
    return _execution_lease.get()


class RedisStreamTaskQueue(TaskQueue):
    def __init__(self, client: Any, archive: Any = None) -> None:
        self._client = client
        self._archive = archive
        self._claim_cursors: dict[str, str] = {_STREAM: "0-0", _LARGE_STREAM: "0-0"}

    async def ensure_group(self) -> None:
        for stream in (_STREAM, _LARGE_STREAM):
            try:
                await self._client.xgroup_create(stream, _GROUP, id="0", mkstream=True)
            except Exception as err:
                if "BUSYGROUP" not in str(err):
                    raise

    async def enqueue(self, task: IntentTask) -> None:
        """状态与消息原子可见；同一个 task_id 不会重复入队。

        Lua 不回滚运行时错误，因此先 XADD，避免流类型异常时留下幽灵 queued。
        """
        stream = _LARGE_STREAM if task.priority > 0 else _STREAM
        initial = self._status_payload(TaskStatus(task.task_id, "queued"))
        semantic_payload = {key: value for key, value in task.to_dict().items()
                            if key not in {"enqueued_at", "priority", "traceparent", "tracestate", "request_id"}}
        fingerprint = hashlib.sha256(json.dumps(semantic_payload, ensure_ascii=False,
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result = await self._client.eval(_ENQUEUE_SCRIPT, 2, f"{_STATUS_PREFIX}{task.task_id}", stream,
                                        initial, json.dumps(task.to_dict(), ensure_ascii=False),
                                        _STATUS_TTL, fingerprint)
        if result == -1:
            raise ValueError("相同 request_id 已用于不同的选购需求")
        if result == 1:
            observe_queue("enqueued")

    @staticmethod
    def _status_payload(status: TaskStatus) -> str:
        return json.dumps({"task_id": status.task_id, "state": status.state,
                           "final_text": status.final_text, "error": status.error,
                           "stream": status.stream, "message_id": status.message_id,
                           "deliveries": status.deliveries}, ensure_ascii=False)

    async def set_status(self, status: TaskStatus) -> None:
        lease = current_execution_lease()
        keys = [f"{_STATUS_PREFIX}{status.task_id}"]
        if lease is not None:
            if not lease.is_valid():
                raise QueueLeaseLost("执行租约已失效，拒绝写入任务状态")
            keys.extend(lease.keys)
            if lease.delivery:
                status = TaskStatus(status.task_id, status.state, status.final_text, status.error,
                                    stream=lease.delivery.stream, message_id=lease.delivery.message_id,
                                    deliveries=lease.delivery.deliveries)
        result = await self._client.eval(_STATUS_SCRIPT, len(keys), *keys,
                                         self._status_payload(status), status.state, _STATUS_TTL,
                                         lease.owner if lease else "")
        if result == -1:
            if lease:
                lease.invalidate()
            raise QueueLeaseLost("执行权已被其他实例接管")
        if result == 1 and status.state in {"running", "retrying", "done", "failed"}:
            observe_queue({"running": "started", "retrying": "retried", "done": "completed", "failed": "failed"}[status.state])

    async def get_status(self, task_id: str) -> Optional[TaskStatus]:
        raw = await self._client.get(f"{_STATUS_PREFIX}{task_id}")
        if raw is None:
            return None
        data = json.loads(raw)
        if data.get("archived"):
            if self._archive is None:
                raise RuntimeError("该任务已归档，但当前进程未配置归档读取，不能返回空的成功结果")
            restored = await self._archive.get_status(task_id)
            if restored is None:
                raise RuntimeError("任务归档丢失，请恢复归档库后读取结果；不会重新执行任务")
            data = restored
        return TaskStatus(task_id=data["task_id"], state=data["state"],
                          final_text=data.get("final_text", ""), error=data.get("error", ""),
                          queue_position=await self.depth() if data["state"] == "queued" else 0,
                          stream=data.get("stream", ""), message_id=data.get("message_id", ""),
                          deliveries=int(data.get("deliveries", 0)))

    async def depth(self) -> int:
        return sum([await self._stream_depth(stream) for stream in (_STREAM, _LARGE_STREAM)])

    async def _stream_depth(self, stream: str) -> int:
        try:
            groups = await self._client.xinfo_groups(stream)
        except Exception:
            return 0
        for group in groups:
            if _text(group.get("name", group.get(b"name", ""))) == _GROUP:
                lag = group.get("lag", group.get(b"lag", 0))
                pending = group.get("pending", group.get(b"pending", 0))
                return int(lag or 0) + int(pending or 0)
        return 0

    async def _acquire(self, lease: ExecutionLease, key: str) -> bool:
        started = time.monotonic()
        acquired = await asyncio.wait_for(self._client.set(key, lease.owner, nx=True, px=lease.lease_ms),
                                          timeout=lease.lease_ms / 1000)
        if acquired:
            lease.keys.append(key)
            expires = started + lease.lease_ms / 1000
            lease.expires_at = min(lease.expires_at, expires) if lease.expires_at else expires
        return bool(acquired)

    async def _heartbeat(self, lease: ExecutionLease, owner_task: asyncio.Task, interval_ms: int) -> None:
        try:
            while lease.valid:
                await asyncio.sleep(interval_ms / 1000)
                started = time.monotonic()
                if started >= lease.expires_at:
                    raise QueueLeaseLost("续租开始前租约已过期")
                delivery = lease.delivery
                renewed = await asyncio.wait_for(self._client.eval(
                    _RENEW_SCRIPT, len(lease.keys), *lease.keys, lease.owner, lease.lease_ms,
                    delivery.stream if delivery else "", _GROUP, lease.consumer_name,
                    delivery.message_id if delivery else ""),
                    timeout=max(0.001, lease.expires_at - started))
                if not renewed:
                    raise QueueLeaseLost("Redis 租约或 pending 所有权已丢失")
                lease.expires_at = started + lease.lease_ms / 1000
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("执行租约失效，取消当前任务：%s", err)
            lease.invalidate()
            owner_task.cancel()

    async def _release(self, lease: ExecutionLease) -> None:
        lease.invalidate()
        if not lease.keys:
            return
        try:
            await asyncio.wait_for(self._client.eval(_RELEASE_SCRIPT, len(lease.keys), *lease.keys, lease.owner), 2)
        except Exception as err:
            logger.warning("释放租约失败，等待 TTL 回收：%s", err)

    @asynccontextmanager
    async def session_lease(self, session_id: str, *, wait_timeout: float = 30.0,
                            lease_ms: int = 30000) -> AsyncIterator[ExecutionLease]:
        """API 与 worker 共用会话租约；worker 已持有同会话租约时直接复用。"""
        existing = current_execution_lease()
        if existing is not None:
            if existing.session_id != session_id or not existing.is_valid():
                raise QueueLeaseLost("当前执行上下文未持有该会话的有效租约")
            yield existing
            return
        if lease_ms < 100 or wait_timeout < 0:
            raise ValueError("lease_ms 至少为 100，wait_timeout 不能为负")
        lease = ExecutionLease(session_id, uuid.uuid4().hex, lease_ms)
        heartbeat: asyncio.Task | None = None
        token = None
        deadline = time.monotonic() + wait_timeout
        try:
            while not await self._acquire(lease, _lease_key("session", session_id)):
                if time.monotonic() >= deadline:
                    raise SessionLeaseTimeout("当前会话仍在处理中，请稍后重试")
                await asyncio.sleep(min(0.1, max(0.001, deadline - time.monotonic())))
            lease.ready = True
            token = _execution_lease.set(lease)
            heartbeat = asyncio.create_task(self._heartbeat(lease, asyncio.current_task(), max(10, lease_ms // 3)))
            yield lease
            if not lease.is_valid():
                raise QueueLeaseLost("会话执行完成前租约已失效")
        finally:
            lease.invalidate()
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if token is not None:
                _execution_lease.reset(token)
            await self._release(lease)

    async def _delivery_count(self, stream: str, message_id: str) -> int:
        pending = await self._client.xpending_range(stream, _GROUP, message_id, message_id, 1)
        if not pending:
            return 1
        return int(pending[0].get("times_delivered", pending[0].get(b"times_delivered", 1)))

    async def _delivery(self, stream: str, message_id: Any, fields: dict) -> QueueDelivery:
        message_id = _text(message_id)
        payload = fields.get("payload", fields.get(b"payload"))
        return QueueDelivery(stream, message_id, _text(payload) if payload is not None else "",
                             await self._delivery_count(stream, message_id))

    async def claim_stale(self, consumer_name: str, idle_ms: int = 60000,
                          count: int = 10) -> list[QueueDelivery]:
        """双流回收实际消息信封，不丢失 ACK 与审计所需的 stream/message_id。"""
        deliveries: list[QueueDelivery] = []
        for stream in (_STREAM, _LARGE_STREAM):
            if len(deliveries) >= count:
                break
            result = await self._client.xautoclaim(stream, _GROUP, consumer_name,
                min_idle_time=idle_ms, start_id=self._claim_cursors[stream], count=count - len(deliveries))
            self._claim_cursors[stream] = _text(result[0])
            for message_id, fields in result[1]:
                deliveries.append(await self._delivery(stream, message_id, fields))
        return deliveries

    async def consume(self, consumer_name: str, handler: Callable[[IntentTask], Awaitable[Any]],
                      should_stop: Callable[[], bool], block_ms: int = 2000,
                      max_deliveries: int = 3, concurrency: int = 1,
                      reclaim_idle_ms: int = 60000, lease_ms: int = 30000,
                      heartbeat_interval_ms: int | None = None) -> None:
        if concurrency < 1 or max_deliveries < 1 or lease_ms < 100 or reclaim_idle_ms < 30:
            raise ValueError("并发度/最大投递次数须为正，租期至少 100ms，回收间隔至少 30ms")
        heartbeat_ms = heartbeat_interval_ms or max(10, min(lease_ms, reclaim_idle_ms) // 3)
        if not 0 < heartbeat_ms < min(lease_ms, reclaim_idle_ms):
            raise ValueError("心跳间隔必须小于租期与回收等待时间")
        await self.ensure_group()
        in_flight: set[asyncio.Task] = set()
        try:
            while not should_stop():
                finished = {task for task in in_flight if task.done()}
                for task in finished:
                    with suppress(asyncio.CancelledError):
                        error = task.exception()
                        if error:
                            logger.warning("消费任务异常，保留 pending：%s", error)
                in_flight.difference_update(finished)
                free_slots = concurrency - len(in_flight)
                if free_slots <= 0:
                    await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                    continue
                try:
                    ready = await self.claim_stale(consumer_name, reclaim_idle_ms, free_slots)
                    # COUNT 对多 stream 是分别生效的；逐流领取，避免实际执行翻倍。
                    for stream in (_STREAM, _LARGE_STREAM):
                        remaining = free_slots - len(ready)
                        if remaining <= 0:
                            break
                        batches = await self._client.xreadgroup(_GROUP, consumer_name, {stream: ">"}, count=remaining)
                        for _stream, entries in batches:
                            for message_id, fields in entries:
                                ready.append(await self._delivery(_text(_stream), message_id, fields))
                except Exception as err:
                    logger.warning("队列读取失败，稍后重试：%s", err)
                    await asyncio.sleep(min(max(block_ms / 1000, 0.01), 1.0))
                    continue
                for delivery in ready:
                    in_flight.add(asyncio.create_task(self._handle_one(
                        delivery.stream, delivery.message_id, {"payload": delivery.payload}, handler,
                        max_deliveries, consumer_name=consumer_name, delivery=delivery,
                        lease_ms=lease_ms, heartbeat_interval_ms=heartbeat_ms)))
                if not ready:
                    await asyncio.sleep(min(max(block_ms / 1000, 0.01), 1.0))
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        except asyncio.CancelledError:
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    async def _ack(self, delivery: QueueDelivery, lease: ExecutionLease | None = None) -> None:
        if lease is None:
            await self._client.xack(delivery.stream, _GROUP, delivery.message_id)
            return
        if not lease.is_valid():
            raise QueueLeaseLost("租约失效，不能确认消息")
        result = await self._client.eval(_ACK_SCRIPT, 1 + len(lease.keys), delivery.stream, *lease.keys,
                                         _GROUP, delivery.message_id, lease.owner)
        if result == -1:
            lease.invalidate()
            raise QueueLeaseLost("ACK 前租约已丢失")

    async def _dead_letter(self, delivery: QueueDelivery, reason: str, task_id: str = "",
                           lease: ExecutionLease | None = None) -> None:
        if lease is not None and not lease.is_valid():
            raise QueueLeaseLost("租约失效，不能提交死信")
        status = TaskStatus(task_id, "failed", error=reason, stream=delivery.stream,
                            message_id=delivery.message_id, deliveries=delivery.deliveries)
        marker = _lease_key("dead", f"{delivery.stream}/{delivery.message_id}")
        keys = [delivery.stream, f"{_STATUS_PREFIX}{task_id}", marker, *(lease.keys if lease else [])]
        result = await self._client.eval(_DEAD_SCRIPT, len(keys), *keys, _DEAD_STREAM,
            delivery.payload, reason, delivery.message_id, delivery.deliveries, task_id,
            self._status_payload(status), lease.owner if lease else "", _GROUP)
        if result == -1:
            raise QueueLeaseLost("提交死信时执行权已丢失")
        if result == 1:
            observe_queue("dead_lettered")
            if task_id:
                observe_queue("failed")

    async def _handle_one(self, stream: str, message_id: str, fields: dict,
                          handler: Callable[[IntentTask], Awaitable[Any]], max_deliveries: int,
                          *, consumer_name: str = "manual", delivery: QueueDelivery | None = None,
                          lease_ms: int = 30000, heartbeat_interval_ms: int = 10000) -> None:
        delivery = delivery or await self._delivery(stream, message_id, fields)
        try:
            task = IntentTask.from_dict(json.loads(delivery.payload))
            if not task.task_id or not task.shopping_session_id:
                raise ValueError("task_id 与 shopping_session_id 不能为空")
        except Exception as err:
            await self._dead_letter(delivery, f"任务解析失败：{err}")
            return
        existing = await self.get_status(task.task_id)
        if existing and existing.state in {"done", "failed"}:
            await self._ack(delivery)
            return
        lease = ExecutionLease(task.shopping_session_id, uuid.uuid4().hex, lease_ms,
                               consumer_name=consumer_name, delivery=delivery)
        heartbeat: asyncio.Task | None = None
        token = None
        try:
            for key in (_lease_key("message", f"{stream}/{message_id}"), _lease_key("task", task.task_id)):
                if not await self._acquire(lease, key):
                    return
            heartbeat = asyncio.create_task(self._heartbeat(lease, asyncio.current_task(), heartbeat_interval_ms))
            while not await self._acquire(lease, _lease_key("session", task.shopping_session_id)):
                if not lease.valid or time.monotonic() >= lease.expires_at:
                    raise QueueLeaseLost("等待会话执行权时租约已失效")
                await asyncio.sleep(min(0.1, heartbeat_interval_ms / 1000))
            lease.ready = True
            token = _execution_lease.set(lease)
            existing = await self.get_status(task.task_id)
            if existing and existing.state in {"done", "failed"}:
                await self._ack(delivery, lease)
                return
            if delivery.deliveries > max_deliveries:
                await self._dead_letter(delivery, "消息超过最大投递次数", task.task_id, lease)
                return
            await self.set_status(TaskStatus(task.task_id, "running"))
            final_text = await handler(task)
            await self.set_status(TaskStatus(task.task_id, "done", final_text=final_text if isinstance(final_text, str) else ""))
            await self._ack(delivery, lease)
        except asyncio.CancelledError:
            lease.invalidate()
            raise
        except QueueLeaseLost as err:
            lease.invalidate()
            logger.warning("失去执行权，消息留在 pending：%s（%s）", task.task_id, err)
        except Exception as err:
            if not lease.is_valid():
                logger.warning("租约不可用，保留 pending：%s（%s）", task.task_id, err)
                return
            if delivery.deliveries >= max_deliveries:
                await self._dead_letter(delivery, str(err), task.task_id, lease)
            else:
                await self.set_status(TaskStatus(task.task_id, "retrying", error=str(err)))
                logger.warning("任务第 %d 次失败，等待重投：%s（%s）", delivery.deliveries, task.task_id, err)
        finally:
            lease.invalidate()
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if token is not None:
                _execution_lease.reset(token)
            await self._release(lease)


class RedisEventBackplane:
    """跨进程事件广播：worker 发布 → API 进程订阅 → 转发给本地 WS。

    必须带发送方标识并跳过自己发的消息：Pub/Sub 不会排除发布者，
    API 进程既发布又订阅同一频道，不过滤就会把自己的事件再投递一次
    （实测现象：前端收到两条 task.queued）。
    """

    def __init__(self, client: Any, origin: Optional[str] = None) -> None:
        self._client = client
        self._origin = origin or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

    @property
    def origin(self) -> str:
        return self._origin

    async def publish(self, event: TradeEvent) -> None:
        channel = f"{_EVENT_CHANNEL_PREFIX}{event.shopping_session_id}"
        envelope = {"origin": self._origin, "event": event.to_dict()}
        try:
            await self._client.publish(channel, json.dumps(envelope, ensure_ascii=False))
        except Exception as err:  # noqa: BLE001 —— 广播失败不影响本进程投递
            logger.warning("事件广播失败：%s（%s）", channel, err)

    async def listen(self) -> AsyncIterator[TradeEvent]:
        """订阅所有会话频道，逐条产出**其他进程**的事件。"""
        pubsub = self._client.pubsub()
        await pubsub.psubscribe(f"{_EVENT_CHANNEL_PREFIX}*")
        try:
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                try:
                    envelope = json.loads(message["data"])
                    if envelope.get("origin") == self._origin:
                        continue  # 自己发的，本地已投递过
                    yield TradeEvent.from_dict(envelope["event"])
                except Exception as err:  # noqa: BLE001
                    logger.warning("远端事件解析失败，跳过：%s", err)
        finally:
            await pubsub.punsubscribe()
            await pubsub.aclose()
