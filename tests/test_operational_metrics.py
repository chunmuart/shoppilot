# -*- coding: utf-8 -*-
"""指标真实聚合、并发上下文隔离及缺失usage不能冒充零成本。"""
import asyncio
import pytest
from app.infrastructure import operational_metrics as metrics
from app.infrastructure.eventbus import TradeEventBus


async def test_parallel_turns_do_not_mix_usage_and_metrics_never_collect_text(monkeypatch):
    registry = metrics.MetricsRegistry()
    monkeypatch.setattr(metrics, 'registry', registry)
    async def turn(tokens):
        observation = metrics.begin_request()
        await asyncio.sleep(0)
        metrics.observe_model(input_tokens=tokens, output_tokens=4, ttft_ms=3, elapsed_ms=10)
        TradeEventBus().publish('private-buyer-session', 'tool.result', {'tool': 'create_order_tool', 'error': 'private-address'})
        return metrics.finish_request(observation)
    first, second = await asyncio.gather(turn(20), turn(30))
    assert [first['input_tokens'], second['input_tokens']] == [20, 30]
    assert first['cost_usd'] is None
    assert first['tool_errors'] == 1
    text = registry.prometheus()
    assert 'shoppilot_input_tokens_observed_total 50' in text
    assert 'private-' not in text
    assert registry.snapshot()['window_count'] == 2


def test_unknown_usage_remains_incomplete_and_repeated_finish_is_rejected():
    observation = metrics.begin_request()
    metrics.observe_model(input_tokens=5, output_tokens=6, cost_usd=.001)
    metrics.observe_model(input_tokens=None, output_tokens=None)
    result = metrics.finish_request(observation, 'cancelled')
    assert result['input_tokens'] is None and not result['usage_complete']
    assert result['observed_input_tokens'] == 5
    assert result['cost_usd'] is None
    assert result['unknown_usage_calls'] == 1
    assert result['status'] == 'cancelled'
    with pytest.raises(ValueError, match='重复'):
        metrics.finish_request(observation)


def test_alerts_need_enough_samples_and_have_explicit_scope(monkeypatch):
    registry = metrics.MetricsRegistry(window_size=20)
    monkeypatch.setattr(metrics, 'registry', registry)
    for _ in range(19):
        observation = metrics.begin_request()
        TradeEventBus().publish('ignored', 'model.fallback', {'reason': 'not stored'})
        metrics.finish_request(observation, 'error')
    assert registry.alerts() == []
    metrics.finish_request(metrics.begin_request(), 'error')
    assert {alert['name'] for alert in registry.alerts()} == {'error_rate', 'fallback_rate'}
    assert all(alert['scope'] == 'process_local_business_turns' for alert in registry.alerts())


def test_invalid_values_cannot_poison_metrics_or_create_label_cardinality(monkeypatch):
    registry = metrics.MetricsRegistry()
    monkeypatch.setattr(metrics, 'registry', registry)
    observation = metrics.begin_request()
    metrics.observe_model(input_tokens=True, output_tokens=2, ttft_ms=float('nan'), cost_usd=-1)
    metrics.observe_event('tool.result', {'tool': ['untrusted'], 'error': 'ignored'})
    metrics.observe_queue('private-session', count=1)
    metrics.observe_queue('dead_lettered', count=2)
    result = metrics.finish_request(observation)
    assert result['input_tokens'] is None and result['ttft_p95_ms'] is None
    assert result['tool_calls'] == 0
    assert 'shoppilot_queue_dead_lettered_total 2' in registry.prometheus()
    assert 'private-session' not in registry.prometheus()


async def test_late_stream_cleanup_never_reports_zero_cost_success(monkeypatch):
    registry = metrics.MetricsRegistry()
    monkeypatch.setattr(metrics, 'registry', registry)
    observation = metrics.begin_request()
    metrics.observe_model_started()
    cleanup_ready = asyncio.Event()
    async def delayed_close():
        await cleanup_ready.wait()
        metrics.observe_model(input_tokens=12, output_tokens=3)
    task = asyncio.create_task(delayed_close())
    result = metrics.finish_request(observation, 'cancelled')
    cleanup_ready.set(); await task
    assert result['model_calls'] == 1
    assert result['active_attempts_at_finish'] == 1
    assert result['input_tokens'] is None and result['cost_usd'] is None
    assert result['unknown_usage_calls'] == 1 and result['usage_complete'] is False
    assert registry.snapshot()['usage_unknown_rate'] == 1
