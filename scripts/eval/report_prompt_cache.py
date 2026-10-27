"""离线汇总冻结的缓存对照结果；不调用模型、不改写原始失败。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
from statistics import mean

from scripts.eval.prompt_cache import CASES, STRATEGIES, p95


def measured_total(rows, key, *, cache=False):
    values = []
    for row in rows:
        for call in row['calls']:
            # 调用连 usage 样本都没留下，同样不能当成零消耗。
            for sample in call.get('usage') or [{}]:
                values.append((sample.get('prompt_cache', {}) if cache else sample).get(key))
    known = [v for v in values if type(v) in (int, float)]
    return {'total': sum(known) if values and len(known) == len(values) else None,
            'observed_sum': sum(known) if known else None,
            'known_attempts': len(known), 'unknown_attempts': len(values) - len(known)}


def paired(rows, strategy, metric):
    differences = []
    for case in CASES:
        groups = [[r for r in rows if r['case'] == case and r['strategy'] == s] for s in ('A', strategy)]
        if any({r['repeat'] for r in group} != {0, 1, 2} or len(group) != 3 for group in groups):
            continue
        values = [[metric(r) for r in group] for group in groups]
        if any(v is None for group in values for v in group):
            continue
        differences.append(mean(values[1]) - mean(values[0]))
    if not differences:
        return {'scenario_count': 0, 'mean_difference': None, 'bootstrap_95_ci': None}
    # 重复运行先在场景内取均值；重采样的单位始终是场景。
    rng = random.Random(20260918)
    samples = sorted(mean(rng.choices(differences, k=len(differences))) for _ in range(10000))
    return {'scenario_count': len(differences), 'mean_difference': mean(differences),
            'bootstrap_95_ci': [samples[249], samples[9749]]}


def build_report(raw):
    rows = raw['rows']
    expected = {(c, s, r) for c in CASES for s in STRATEGIES for r in range(3)}
    keys = [(r['case'], r['strategy'], r['repeat']) for r in rows]
    layout_complete = len(keys) == len(expected) and set(keys) == expected
    failures = []
    result = {'layout_complete': layout_complete, 'scenario_runs': len(rows),
              'strategies': {}, 'paired': {}, 'cost_currency': None, 'enablement': {}}
    for strategy in STRATEGIES:
        selected = [r for r in rows if r['strategy'] == strategy]
        calls = [c for r in selected for c in r['calls']]
        for row in selected:
            expected_phases = ['cold', 'warm', 'summary', 'changed'] if row['case'] == 'summary_replaced' else ['cold', 'warm', 'changed']
            layout_complete &= [c['phase'] for c in row['calls']] == expected_phases
            for call in row['calls']:
                if not call['passed']:
                    failures.append({'case': row['case'], 'repeat': row['repeat'], 'strategy': strategy,
                                     'phase': call['phase'], 'reason': call.get('error_type', 'answer_assertion_failed')})
        result['strategies'][strategy] = {
            'runs': len(selected), 'passed_runs': sum(r['passed'] for r in selected),
            'model_calls': len(calls), 'physical_attempts': sum(len(c.get('usage', [])) for c in calls),
            'passed_calls': sum(c['passed'] for c in calls),
            **{key: measured_total(selected, key, cache=key.startswith('cache_') or key == 'reported_cost')
               for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reported_cost')},
            'ordinary_all_p95_ms': p95([c['elapsed_ms'] for c in calls if c['kind'] == 'business']),
            'ordinary_completed_p95_ms': p95([c['elapsed_ms'] for c in calls if c['kind'] == 'business' and not c.get('error_type')]),
            'summary_p95_ms': p95([c['elapsed_ms'] for c in calls if c['kind'] == 'summary']),
        }
    result['layout_complete'] = layout_complete
    result['failure_counts'] = dict(Counter(f['reason'] for f in failures))
    result['failures'] = failures
    for strategy in ('B', 'C'):
        result['paired'][strategy + '_minus_A'] = {
            'success_rate': paired(rows, strategy, lambda r: float(r['passed'])),
            'scenario_elapsed_ms': paired(rows, strategy, lambda r: sum(c['elapsed_ms'] for c in r['calls'])),
            'provider_amount': paired(rows, strategy, lambda r: measured_total([r], 'reported_cost', cache=True)['total']),
        }
        baseline = result['strategies']['A']
        current = result['strategies'][strategy]
        p95_a = baseline['ordinary_completed_p95_ms']
        p95_b = current['ordinary_completed_p95_ms']
        ratio = p95_b / p95_a if p95_a and p95_b is not None else None
        complete_cost = baseline['reported_cost']['total'] is not None and current['reported_cost']['total'] is not None
        result['enablement'][strategy] = {
            'status': 'NOT_APPROVED',
            'all_scenarios_passed': layout_complete and current['passed_runs'] == 24,
            'ordinary_p95_ratio_to_A': ratio, 'latency_within_15_percent': ratio is not None and ratio <= 1.15,
            'provider_amount_complete': complete_cost,
            'billing_currency_verified': False,
            'production_cost_reduction': None,
        }
    result['scope'] = '合成上下文回放；回查为固定证据，不执行数据库工具。诊断与恢复复核不合并进本报告。'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    content = args.results.read_bytes()
    report = build_report(json.loads(content))
    report['source_sha256'] = hashlib.sha256(content).hexdigest()
    # 新路径才能写，防止无意覆盖上次验收结论。
    with args.output.open('x') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
