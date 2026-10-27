# -*- coding: utf-8 -*-
"""只从服务端已观察的业务回合 usage 汇总；缺失字段不能推成零成本。"""
from __future__ import annotations
import math


def _numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def collect_case_metrics(events: list[dict], *, expected_turns: int, elapsed_ms: float) -> dict:
    summaries = [event.get('payload', {}) for event in events if event.get('type') == 'usage.summary']
    complete = len(summaries) == expected_turns and all(row.get('usage_complete') is True and
        all(type(row.get(key)) is int and row[key] >= 0 for key in ('input_tokens', 'output_tokens')) for row in summaries)
    cost_complete = len(summaries) == expected_turns and all(_numeric(row.get('cost_usd')) for row in summaries)
    return {'elapsed_ms': elapsed_ms, 'latency_scope': 'agent_dialogue_and_http_actions_excluding_judge',
            'input_tokens': sum(row['input_tokens'] for row in summaries) if complete else None,
            'output_tokens': sum(row['output_tokens'] for row in summaries) if complete else None,
            'cost_usd': sum(row['cost_usd'] for row in summaries) if cost_complete else None,
            'usage_complete': complete, 'observed_turns': len(summaries), 'expected_turns': expected_turns,
            'cost_source': 'configured_price_table' if cost_complete else 'unknown'}


def release_metrics(results: list[dict]) -> dict:
    count = len(results)
    metrics = [result.get('metrics', {}) for result in results]
    latency_complete = bool(results) and all(_numeric(row.get('elapsed_ms')) for row in metrics)
    usage_complete = bool(results) and all(row.get('usage_complete') is True for row in metrics)
    elapsed = sorted(row['elapsed_ms'] for row in metrics) if latency_complete else []
    return {'task_success_rate': sum(result.get('verdict') == 'PASS' for result in results) / count if count else None,
            'hard_constraint_pass_rate': sum(result.get('p0_pass') is True for result in results) / count if count else None,
            'latency_p95_ms': elapsed[max(0, math.ceil(count * .95)-1)] if latency_complete else None,
            'input_tokens': sum(row['input_tokens'] for row in metrics) if usage_complete else None,
            'output_tokens': sum(row['output_tokens'] for row in metrics) if usage_complete else None,
            'cost_usd': sum(row['cost_usd'] for row in metrics) if count and all(_numeric(row.get('cost_usd')) for row in metrics) else None,
            'scope': 'full_selected_cases; latency_excludes_judge; hard_constraint=P0_all_pass',
            'usage_complete': usage_complete}
