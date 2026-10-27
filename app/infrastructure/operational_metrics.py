# -*- coding: utf-8 -*-
"""进程内业务回合指标。只接收数值和白名单枚举，不采集对话、地址或身份。"""
from __future__ import annotations

from collections import Counter, deque
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import math
from threading import Lock
from time import perf_counter
from typing import Any

_TOOL_NAMES = {'product_search_tool', 'category_insight_tool', 'create_order_tool', 'query_order_tool',
               'cancel_order_tool', 'remember_preference_tool', 'landed_price_tool', 'web_search_tool',
               'lookup_strategy_memory_tool', 'load_agent_skill_tool'}
_STATUSES = {'success', 'error', 'cancelled'}
_BUCKETS = (100, 500, 1000, 5000, 15000, 30000, 60000, 120000)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _tokens(value: Any) -> bool:
    return type(value) is int and value >= 0


def percentile(values: list[float], fraction: float = .95) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


@dataclass
class RequestObservation:
    started: float = field(default_factory=perf_counter)
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    active_attempts: int = 0
    unknown_usage_calls: int = 0
    ttft_ms: list[float] = field(default_factory=list)
    model_elapsed_ms: list[float] = field(default_factory=list)
    tool_calls: int = 0
    tool_errors: int = 0
    fallbacks: int = 0
    cost_usd: float = 0
    unknown_cost_calls: int = 0
    finished: bool = False
    token: Token | None = field(default=None, repr=False)


_current: ContextVar[RequestObservation | None] = ContextVar('shoppilot_operational_metrics', default=None)


class MetricsRegistry:
    """有界样本用于滚动告警；计数器和直方图自进程启动累计。多 worker 需采集端聚合。"""
    def __init__(self, window_size: int = 1000):
        self._lock = Lock()
        self.counters: Counter = Counter()
        self.histogram: Counter = Counter()
        self.samples: deque = deque(maxlen=window_size)

    def record(self, result: dict) -> None:
        with self._lock:
            self.samples.append(dict(result))
            self.counters['requests_total', result['status']] += 1
            self.counters['request_duration_ms_sum', ''] += result['elapsed_ms']
            for bound in _BUCKETS:
                if result['elapsed_ms'] <= bound:
                    self.histogram[bound] += 1
            for key in ('model_calls', 'unknown_usage_calls', 'tool_calls', 'tool_errors', 'fallbacks', 'unknown_cost_calls'):
                self.counters[key + '_total', ''] += result[key]
            self.counters['input_tokens_observed_total', ''] += result['observed_input_tokens']
            self.counters['output_tokens_observed_total', ''] += result['observed_output_tokens']

    def snapshot(self) -> dict:
        with self._lock:
            rows = list(self.samples)
            total = len(rows)
            return {'scope': 'process_local_business_turns', 'window_count': total,
                    'latency_p95_ms': percentile([row['elapsed_ms'] for row in rows]),
                    'error_rate': sum(row['status'] == 'error' for row in rows) / total if total else None,
                    'cancel_rate': sum(row['status'] == 'cancelled' for row in rows) / total if total else None,
                    'fallback_rate': sum(row['fallbacks'] > 0 for row in rows) / total if total else None,
                    'usage_unknown_rate': sum(row['unknown_usage_calls'] > 0 for row in rows) / total if total else None}

    def prometheus(self) -> str:
        with self._lock:
            lines = ['# 业务回合指标；进程内累计，不是 HTTP 请求或跨 worker 聚合。']
            for (name, status), value in sorted(self.counters.items()):
                label = '{status="' + status + '"}' if status else ''
                lines.append(f'shoppilot_{name}{label} {value}')
            total = sum(value for (name, _), value in self.counters.items() if name == 'requests_total')
            for bound in _BUCKETS:
                lines.append(f'shoppilot_request_duration_ms_bucket{{le="{bound}"}} {self.histogram[bound]}')
            lines.extend([f'shoppilot_request_duration_ms_bucket{{le="+Inf"}} {total}', f'shoppilot_request_duration_ms_count {total}'])
            return '\n'.join(lines) + '\n'

    def alerts(self, *, minimum_samples: int = 20, latency_p95_ms: float = 60000,
               error_rate: float = .05, fallback_rate: float = .1) -> list[dict]:
        snapshot = self.snapshot()
        if snapshot['window_count'] < minimum_samples:
            return []
        limits = {'latency_p95_ms': latency_p95_ms, 'error_rate': error_rate, 'fallback_rate': fallback_rate}
        return [{'name': key, 'actual': snapshot[key], 'threshold': limit, 'window_count': snapshot['window_count'],
                 'scope': snapshot['scope']} for key, limit in limits.items() if snapshot[key] is not None and snapshot[key] > limit]


registry = MetricsRegistry()


def begin_request() -> RequestObservation:
    observation = RequestObservation()
    observation.token = _current.set(observation)
    return observation


def observe_model_started() -> None:
    """真实开始请求时登记未结算 attempt，覆盖取消后后台晚清理的生命周期。"""
    observation = _current.get()
    if observation is not None and not observation.finished:
        observation.active_attempts += 1


def observe_model(*, input_tokens: int | None, output_tokens: int | None, ttft_ms: float | None = None,
                  elapsed_ms: float | None = None, cost_usd: float | None = None) -> None:
    """每个真实上游 attempt 仅结算一次；未知 usage/cost 不按零成本冒充完整统计。"""
    observation = _current.get()
    if observation is None or observation.finished:
        return
    observation.model_calls += 1
    observation.active_attempts = max(0, observation.active_attempts - 1)
    if _tokens(input_tokens) and _tokens(output_tokens):
        observation.input_tokens += input_tokens
        observation.output_tokens += output_tokens
    else:
        observation.unknown_usage_calls += 1
    if _number(ttft_ms):
        observation.ttft_ms.append(float(ttft_ms))
    if _number(elapsed_ms):
        observation.model_elapsed_ms.append(float(elapsed_ms))
    if _number(cost_usd):
        observation.cost_usd += cost_usd
    else:
        observation.unknown_cost_calls += 1


def observe_event(event_type: str, payload: Any) -> None:
    observation = _current.get()
    if observation is None or observation.finished or not isinstance(payload, dict):
        return
    if event_type == 'tool.result':
        # 只读取工具名和结构化失败位；不保存 arguments/result/error 自由文本。
        tool = payload.get('tool') or payload.get('tool_name') or payload.get('name')
        if isinstance(tool, str) and tool in _TOOL_NAMES:
            observation.tool_calls += 1
            observation.tool_errors += int(bool(payload.get('error')) or payload.get('success') is False or payload.get('ok') is False)
    elif event_type == 'model.fallback':
        observation.fallbacks += 1


def finish_request(observation: RequestObservation, status: str = 'success') -> dict:
    if status not in _STATUSES:
        raise ValueError('未知业务回合状态')
    if observation.finished:
        raise ValueError('业务回合指标不能重复结算')
    observation.finished = True
    if observation.token is not None:
        _current.reset(observation.token)
    unknown_usage = observation.unknown_usage_calls + observation.active_attempts
    unknown_cost = observation.unknown_cost_calls + observation.active_attempts
    complete = unknown_usage == 0
    result = {'status': status, 'elapsed_ms': (perf_counter() - observation.started) * 1000,
              'model_calls': observation.model_calls + observation.active_attempts, 'input_tokens': observation.input_tokens if complete else None,
              'output_tokens': observation.output_tokens if complete else None,
              'observed_input_tokens': observation.input_tokens, 'observed_output_tokens': observation.output_tokens,
              'unknown_usage_calls': unknown_usage, 'active_attempts_at_finish': observation.active_attempts, 'usage_complete': complete,
              'ttft_p95_ms': percentile(observation.ttft_ms), 'model_elapsed_ms': sum(observation.model_elapsed_ms),
              'tool_calls': observation.tool_calls, 'tool_errors': observation.tool_errors, 'fallbacks': observation.fallbacks,
              'cost_usd': observation.cost_usd if unknown_cost == 0 else None,
              'unknown_cost_calls': unknown_cost}
    registry.record(result)
    return result


def observe_queue(event: str, *, elapsed_ms: float | None = None, count: int = 1) -> None:
    """队列事件独立累计；未知枚举/无效计数忽略，绝不以任务或买家身份作标签。"""
    if event not in {'enqueued', 'started', 'retried', 'completed', 'failed', 'dead_lettered', 'archived', 'deleted'} or not _tokens(count):
        return
    with registry._lock:
        registry.counters[f'queue_{event}_total', ''] += count
        if _number(elapsed_ms):
            registry.counters['queue_processing_duration_ms_sum', ''] += elapsed_ms
            registry.counters['queue_processing_duration_ms_count', ''] += 1
