"""先验证完整性/质量，再评估累计收益；缺失值绝不填零。"""
from collections import defaultdict
import math
import random
from statistics import mean


def percentile(values, ratio=.95):
    values = sorted(v for v in values if type(v) in (int,float) and math.isfinite(v))
    return values[max(0, math.ceil(len(values)*ratio)-1)] if values else None


def total(rows, field, cache=False):
    values=[]
    for row in rows:
        for sample in row.get('usage') or [{}]:
            if field=='total_tokens':
                a,b=sample.get('input_tokens'),sample.get('output_tokens')
                values.append(a+b if type(a) in (int,float) and type(b) in (int,float) else None)
            else:values.append((sample.get('prompt_cache', {}) if cache else sample).get(field))
    known=[v for v in values if type(v) in (int,float) and math.isfinite(v) and v>=0]
    return {'total':sum(known) if values and len(known)==len(values) else None,
            'observed_sum':sum(known) if known else None, 'known':len(known), 'unknown':len(values)-len(known)}


def paired(rows, baseline, candidate, repetitions, field=None, seed=20260918):
    grouped=defaultdict(lambda:defaultdict(list))
    for row in rows:grouped[row['case_id']][row['strategy']].append(row)
    pairs=[]
    for group in grouped.values():
        a,b=group[baseline],group[candidate]
        if any(len(g)!=repetitions or {r['repetition'] for r in g}!=set(range(repetitions)) for g in (a,b)):continue
        values=[[int(r['passed']) if field is None else total([r],field,cache=field=='reported_cost')['total'] for r in g] for g in (a,b)]
        if any(v is None for g in values for v in g):continue
        pairs.append((mean(values[0]),mean(values[1])))
    if not pairs:return {'unit':'scenario','scenarios':0,'estimate':None,'ci95':None}
    def statistic(points):
        if field:
            denominator=sum(a for a,b in points)
            return 1-sum(b for a,b in points)/denominator if denominator else None
        return mean(b-a for a,b in points)
    estimate=statistic(pairs)
    if estimate is None:return {'unit':'scenario','scenarios':len(pairs),'estimate':None,'ci95':None}
    # 单场景即使重复多次，也不能估计场景总体的变异。
    if len(pairs)<2:return {'unit':'scenario','scenarios':len(pairs),'estimate':estimate,'ci95':None}
    rng=random.Random(seed)
    samples=[statistic(rng.choices(pairs,k=len(pairs))) for _ in range(4000)]
    samples=[s for s in samples if s is not None]
    return {'unit':'scenario','scenarios':len(pairs),'estimate':estimate,
            'ci95':[percentile(samples,.025),percentile(samples,.975)]}


def summarize(manifest, rows):
    expected={(c,s,r) for c in manifest['cases'] for s in manifest['strategies'] for r in range(manifest['repetitions'])}
    keys=[(r['case_id'],r['strategy'],r['repetition']) for r in rows]
    complete=set(keys)==expected and len(keys)==len(expected) and len(set(keys))==len(keys)
    strategies={}
    for name in manifest['strategies']:
        group=[r for r in rows if r['strategy']==name]
        usage=[u for r in group for u in r.get('usage',[])]
        rounds=[t for r in group for t in r.get('round_metrics',[])]
        strategies[name]={
            'runs':len(group),'passed':sum(r['passed'] for r in group),
            'success_rate':mean(int(r['passed']) for r in group) if group else None,
            'input_tokens':total(group,'input_tokens'),'output_tokens':total(group,'output_tokens'),
            'total_tokens':total(group,'total_tokens'),
            'cache_read_tokens':total(group,'cache_read_tokens',True),
            'cache_write_tokens':total(group,'cache_write_tokens',True),
            'reported_cost':total(group,'reported_cost',True),
            'physical_model_calls':len(usage),'summary_calls':sum(u['kind']=='summary' for u in usage),
            'lookup_calls':sum(r.get('lookup_calls',0) for r in group),
            'ordinary_round_p95_ms':percentile([t['elapsed_ms'] for t in rounds if not t['compacted']]),
            'compacted_round_p95_ms':percentile([t['elapsed_ms'] for t in rounds if t['compacted']]),
            'manual_compaction_p95_ms':percentile([t['elapsed_ms'] for r in group for t in r.get('compaction_metrics',[])]),
            'usage_by_kind':{kind:{field:total([{'usage':[u for u in usage if u.get('kind')==kind]}],field)
                for field in ('input_tokens','output_tokens')} for kind in sorted({u.get('kind','unknown') for u in usage})},
            'model_ttft_p95_ms':percentile([u.get('ttft_ms') for u in usage]),
            'model_ttft_known_calls':sum(u.get('ttft_ms') is not None for u in usage),
            'scenario_p95_ms':percentile([r['elapsed_ms'] for r in group]),
            'by_layer':{layer:{'runs':sum(r['layer']==layer for r in group),
                              'passed':sum(r['layer']==layer and r['passed'] for r in group)} for layer in ('context','agent','cache_replay','skill_replay')},
            'peak_actual_input':max((u['input_tokens'] for u in usage if u.get('input_tokens') is not None),default=None),
            'mean_final_context_estimated_tokens':mean([r['final_context_estimated_tokens'] for r in group if r.get('final_context_estimated_tokens') is not None]) if any(r.get('final_context_estimated_tokens') is not None for r in group) else None,
            'mean_final_product_duplicate_ratio':mean([r['final_product_duplicate_ratio'] for r in group if r.get('final_product_duplicate_ratio') is not None]) if any(r.get('final_product_duplicate_ratio') is not None for r in group) else None,
            'loop_notices':sum(e['payload'].get('harness')=='loop_detected' for r in group for e in r.get('events',[])),
            'error_runs':sum(r.get('error') is not None for r in group),
        }
        cache = [u.get('prompt_cache', {}) for u in usage]
        strategies[name]['model_contract'] = {
            'observed_calls':len(cache),
            'response_models':sorted({c['response_model'] for c in cache if c.get('response_model')}),
            'unknown_identity_calls':sum(c.get('response_model_matches') is None for c in cache),
            'mismatched_identity_calls':sum(c.get('response_model_matches') is False or c.get('response_model_conflict') is True for c in cache),
            'protocol_violation_calls':sum(c.get('protocol_status') not in (None,'unknown','valid') for c in cache),
            'unknown_protocol_calls':sum(c.get('protocol_status') in (None,'unknown') for c in cache),
        }
        comparable = [c for c in cache if c.get('prefix_comparison') not in (None, 'first_request')]
        read = strategies[name]['cache_read_tokens']['total']
        input_total = strategies[name]['input_tokens']['total']
        strategies[name]['cache_read_ratio'] = read / input_total if read is not None and input_total else None
        strategies[name]['prefix_diagnostics'] = {
            'observed_calls': sum('prefix_comparison' in c for c in cache),
            'comparable_calls': len(comparable),
            'system_changes': sum(c.get('prefix_system_changed') is True for c in comparable) if comparable else None,
            'tool_changes': sum(c.get('prefix_tools_changed') is True for c in comparable) if comparable else None,
            'history_rewrites': sum(c.get('prefix_comparison') == 'rewrite' for c in comparable) if comparable else None,
            'append_calls': sum(c.get('prefix_comparison') == 'append' for c in comparable) if comparable else None,
        }
        strategies[name]['replay_stage_ttft_p95_ms'] = {stage: percentile([u.get('ttft_ms') for u in usage if u.get('replay_stage') == stage])
            for stage in ('initial', 'followup', 'after_rebuild')}
        diagnostics = [d for r in group for d in r.get('context_diagnostics', [])]
        lookups = [d for d in diagnostics if d['type'] == 'lookup']
        states = [d for d in diagnostics if d['type'] == 'state']
        prunes = [d for d in diagnostics if d['type'] == 'prune']
        requests = [u['request_context'] for u in usage if u.get('request_context')]
        strategies[name]['efficiency_diagnostics'] = {
            'lookup_observed': len(lookups),
            'lookup_errors': sum(d['status'] != 'success' for d in lookups) if lookups else None,
            'lookup_normalized': sum(d.get('limit_capped', False) or d.get('fields_normalized', False) for d in lookups) if lookups else None,
            'lookup_pages_with_more': sum(d.get('has_more', False) for d in lookups) if lookups else None,
            'state_emitted': sum(d['emitted'] for d in states) if states else None,
            'state_skipped': sum(not d['emitted'] for d in states) if states else None,
            'prune_rewrites': sum(d['archived_results'] > 0 for d in prunes) if prunes else None,
            'archived_results': sum(d['archived_results'] for d in prunes) if prunes else None,
            'request_reasons': {reason: sum((r.get('reason') or 'unknown') == reason for r in requests)
                for reason in sorted({r.get('reason') or 'unknown' for r in requests})},
            # 旧版或失败调用可能只有恢复次数；缺失分区不能补零，也不能阻断原始失败报告。
            'estimated_sections': {section: sum(r[section + '_estimated_tokens'] for r in requests)
                if requests and all(type(r.get(section + '_estimated_tokens')) in (int, float)
                    and math.isfinite(r[section + '_estimated_tokens']) for r in requests) else None
                for section in ('system', 'state', 'product', 'history', 'tool_schema')},
            'measured_requests': len(requests),
            'visible_evidence_answers':sum(d.get('type')=='visible_evidence' and d.get('covered') is True for d in diagnostics),
            'model_elapsed_p95_ms': percentile([u.get('elapsed_ms') for u in usage]),
            'after_first_text_p95_ms': percentile([max(0, u['elapsed_ms'] - u['ttft_ms']) for u in usage
                if u.get('elapsed_ms') is not None and u.get('ttft_ms') is not None]),
        }
        coverage=len(group)==len(manifest['cases'])*manifest['repetitions'] and len({(r['case_id'],r['repetition']) for r in group})==len(group)
        strategies[name]['coverage_complete']=coverage
        if not coverage:
            strategies[name]['cache_read_ratio']=None
            # 中断报告只能给已完成部分的小计，不能把它显示成完整累计费用。
            for field in ('input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens','reported_cost'):
                strategies[name][field]['total']=None
    contracts=manifest.get('contracts',{})
    common=[]
    if not complete:common.append('场景矩阵不完整或重复')
    if manifest.get('source_stable') is not True:common.append('源码尚未确认稳定或运行中发生变化')
    if not contracts.get('passed'):common.append('确定性安全回归未通过或未执行')
    if manifest['profile']!='release':common.append('当前档位不是留出发布评测')
    if manifest.get('status')!='completed':common.append('运行未正常完成')
    identities=[s['model_contract'] for s in strategies.values()]
    if any(not d['observed_calls'] or d['unknown_identity_calls'] or d['mismatched_identity_calls'] for d in identities):
        common.append('响应模型标识未知或与请求不一致，实际模型一致性未验证')
    if any(d['protocol_violation_calls'] or d['unknown_protocol_calls'] for d in identities):
        common.append('存在工具协议违约或协议观测缺失')
    if len({tuple(d['response_models']) for d in identities})>1:
        common.append('不同策略的响应模型集合不一致')
    gates={}; intervals={}; policy=manifest['gates']; baseline='current'
    for name in manifest['strategies']:
        if name==baseline:continue
        interval=paired(rows,baseline,name,manifest['repetitions'],'input_tokens')
        all_tokens=paired(rows,baseline,name,manifest['repetitions'],'total_tokens')
        quality=paired(rows,baseline,name,manifest['repetitions'])
        intervals[name]={'input_reduction':interval,'total_token_reduction':all_tokens,'success_difference':quality}
        reasons=list(common)
        base=strategies[baseline];current=strategies[name]
        critical=[r for r in rows if r['strategy']==name and r.get('critical') and not r['passed']]
        if critical:reasons.append(f'{len(critical)} 次关键场景失败')
        if quality['estimate'] is None or quality['estimate'] < policy['success_rate_min_difference']:reasons.append('任务成功率未证明不低于基线')
        new_failures=[r['case_id'] for r in rows if r['strategy']==name and not r['passed'] and any(
            a['strategy']==baseline and a['case_id']==r['case_id'] and a['repetition']==r['repetition'] and a['passed'] for a in rows)]
        if new_failures:reasons.append('存在基线通过而候选失败的配对运行')
        inputs_known=all(strategies[s]['input_tokens']['total'] is not None for s in (baseline,name))
        if not inputs_known:reasons.append('累计实际 input usage 不完整')
        if interval['scenarios']!=len(manifest['cases']):reasons.append('完整成本配对未覆盖全部场景')
        if policy.get('objective') != 'prompt_cache':
            if interval['estimate'] is None or interval['estimate']<policy['actual_input_reduction']:reasons.append('累计实际输入降幅未达到目标')
            if not interval['ci95'] or interval['ci95'][0]<=0:reasons.append('输入降幅置信区间未排除无收益')
            if all_tokens['scenarios']!=len(manifest['cases']) or all_tokens['estimate'] is None or all_tokens['estimate']<policy.get('actual_total_token_reduction',0):reasons.append('包含输出的总 token 未证明改善，防止输入下降但输出暴涨')
        else:
            # 缓存命中不减少上下文容量；不以输入降幅替代账单收益，也不降低完整用量要求。
            if any(strategies[s]['total_tokens']['total'] is None for s in (baseline,name)):
                reasons.append('包含输出的实际用量不完整')
            pricing = manifest.get('pricing', {})
            if not all(pricing.get(k) for k in ('verified','currency','source','verified_at')):
                reasons.append('缓存目标缺少经过验证的计费单位与来源')
            bill = paired(rows,baseline,name,manifest['repetitions'],'reported_cost')
            intervals[name]['billing_against_current'] = bill
            if bill['scenarios'] != len(manifest['cases']) or bill['estimate'] is None or bill['estimate'] <= 0 or not bill['ci95'] or bill['ci95'][0] <= 0:
                reasons.append('缓存目标未证明完整计费收益')
        a,b=base['ordinary_round_p95_ms'],current['ordinary_round_p95_ms']
        latency_ratio=b/a if a and b is not None else None
        if latency_ratio is None or latency_ratio>policy['ordinary_p95_max_ratio']:reasons.append('普通轮 P95 未达标或未知')
        if manifest['policy_overrides'][name].get('prompt_cache_mode')=='explicit':
            def without_cache(config):return {k:v for k,v in config.items() if not k.startswith('prompt_cache_')}
            peers=[s for s in manifest['strategies'] if s!=name and manifest['policy_overrides'][s].get('prompt_cache_mode')=='passthrough'
                   and without_cache(manifest['policy_overrides'][s])==without_cache(manifest['policy_overrides'][name])]
            pricing=manifest.get('pricing',{})
            verified=all(pricing.get(k) for k in ('verified','currency','source','verified_at'))
            if not peers:reasons.append('CP 缺少仅缓存参数不同的消融对照')
            if not verified:reasons.append('CP 计费单位与来源未验证，不能以输入缩减替代')
            if peers:
                cost=paired(rows,peers[0],name,manifest['repetitions'],'reported_cost')
                intervals[name]['cache_cost_against_'+peers[0]]=cost
                if cost['scenarios']!=len(manifest['cases']) or cost['estimate'] is None or cost['estimate']<=0 or not cost['ci95'] or cost['ci95'][0]<=0:
                    reasons.append('CP 未证明完整计费收益')
        gates[name]={'status':'BLOCKED' if reasons else 'BENEFIT_VERIFIED', 'reasons':reasons,
                     'ordinary_p95_ratio':latency_ratio,'new_failure_cases':new_failures,
                     'production_deploy_authorized':False,'billing_reduction':None}
    return {'matrix_complete':complete,'missing':[list(x) for x in sorted(expected-set(keys))],
            'strategies':strategies,'paired_intervals':intervals,'gates':gates,
            'failure_count':sum(not r['passed'] for r in rows),'unknown_currency':manifest.get('pricing',{}).get('currency') is None}
