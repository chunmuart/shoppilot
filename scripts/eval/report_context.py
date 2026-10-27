"""按场景配对的上下文评测报告；正文保留供复核，未知usage不当成0。"""
from __future__ import annotations
import argparse,json,math,random,re,statistics
from collections import defaultdict
from pathlib import Path
from scripts.eval.run_context import check_case


def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None



def association_check(case_id,answer,rules=None):
    """核对属性归属；不允许把存在于答案中的数字随意配到别的SKU或时间。"""
    contracts={
        'hold-old-price':[(r'历史|第一批|第\s*1\s*批','129','139'),(r'当前|现价|现在','139','129')],
        'hold-pair':[(r'P1001-S2','199','129'),(r'P1003-S1','129','199')],
        'hold-sku-stock':[(r'石墨黑|P1003-S1','80','60'),(r'雾霾蓝|P1003-S2','60','80')],
        'hold-blue':[(r'雾霾蓝|P1003-S2','60','80')],
        'hold-tax':[(r'运费|配送费','25','0'),(r'税费|税额','0','25')],
    }
    if rules is None and case_id not in contracts:return {'applicable':False,'status':'not_applicable'}
    clean=re.sub(r'[*_`]','',answer)
    rules=rules if rules is not None else contracts[case_id]
    anchors=sorted((m.start(),m.end(),index) for index,(pattern,_,_) in enumerate(rules) for m in re.finditer(pattern,clean,re.I))
    outcomes=[]
    for index,(_,expected,wrong) in enumerate(rules):
        evidence=[]
        for start,end,group in anchors:
            if group!=index:continue
            postfix=re.search(r'(?<![0-9])('+re.escape(expected)+'|'+re.escape(wrong)+r')(?:\.0+)?[^0-9]{0,2}[（(]$',clean[max(0,start-24):start])
            if postfix and clean[end:end+1] in {')','）'}:
                evidence.append(postfix.group(1)==expected);continue
            boundary=min((pos for pos,_,other in anchors if pos>=end and other!=index),default=len(clean))
            text=re.sub(r'P\d+(?:-S\d+)?','',clean[end:min(boundary,end+500)])
            # 属性先按锚点分区，再取本区首个目标数值，标题中的逗号不会拆断SKU与价格。
            match=re.search(r'(?<![0-9])('+re.escape(expected)+'|'+re.escape(wrong)+r')(?:\.0+)?(?![0-9.])',text)
            if match:evidence.append(match.group(1)==expected)
        outcomes.append(False if False in evidence else True if True in evidence else None)
    if case_id.endswith('hold-blue') and re.search(r'当前库存[^0-9]{0,12}'+re.escape(rules[0][1])+r'(?![0-9])',clean):outcomes=[False]
    return {'applicable':True,'status':'failed' if False in outcomes else 'passed' if all(x is True for x in outcomes) else 'unknown','fields':outcomes}


def score(rows,cases):
    for row in rows:
        case=cases[row['case_id']]
        # 评测要求币种语义为CNY，人民币/RMB等价，不能误判为事实丢失。
        answer=re.sub(r'人民币|RMB','CNY',row['answer'],flags=re.I)
        if 'CNY' in case['contains'] and re.search(r'\d+(?:\.\d+)?\s*元',answer) and not re.search('日元|美元|欧元',answer):answer+=' CNY'
        if '不' in case['contains'] and re.search(r'无(?:任何|相关|长期)?.{0,12}偏好|(?:该|此)?约束已取消',answer):answer+=' 不再要求'
        if '不' in case['contains'] and re.search(r'已无[“"\s]*长期排除塑料[”"\s]*(?:的)?要求',answer):answer+=' 不再要求'
        if '太贵' in case['contains'] and re.search(r'价格超出预算|超预算|价格过高|价格偏高',answer):answer+=' 太贵'
        if 'CANCELLED' in case['contains'] and '已取消' in answer:answer+=' CANCELLED'
        checks=check_case(case,answer,[True]*row['current_calls'])
        if row['mode']=='long':checks['two_compactions']=len(row['compactions'])==2 and all(r.get('summary_changed') for r in row['compactions'])
        associations=association_check(row['case_id'],row['answer'],case.get('association_rules'));row['association_check']=associations
        if associations['applicable']:checks['fact_associations']=associations['status']=='passed'
        row['original_checks']=row['checks'];row['checks']=checks
        row['passed']=all(checks.values()) and row['error'] is None
    return rows


def paired_interval(rows,a,b,seed=20260909):
    grouped=defaultdict(lambda:defaultdict(list))
    for row in rows:grouped[row['case_id']][row['strategy']].append(int(row['passed']))
    diffs=[statistics.mean(v[b])-statistics.mean(v[a]) for v in grouped.values() if a in v and b in v]
    if not diffs:return None
    rng=random.Random(seed)
    samples=[statistics.mean(rng.choices(diffs,k=len(diffs))) for _ in range(2000)]
    return {'unit':'scenario','difference':statistics.mean(diffs),'ci95':[percentile(samples,.025),percentile(samples,.975)]}



def paired_cost_interval(rows,a,b,seed=20260909):
    grouped=defaultdict(lambda:defaultdict(list))
    for row in rows:
        if row['mode']=='long' and row['strategy'] in {a,b}:
            if row['input_tokens'] is None:return None
            grouped[row['case_id']][row['strategy']].append(row['input_tokens'])
    pairs=[(statistics.mean(v[a]),statistics.mean(v[b])) for v in grouped.values() if a in v and b in v]
    if not pairs:return None
    def reduction(sample):
        baseline=sum(x for x,y in sample)
        return 1-sum(y for x,y in sample)/baseline if baseline else None
    rng=random.Random(seed)
    samples=[reduction(rng.choices(pairs,k=len(pairs))) for _ in range(2000)]
    if any(x is None for x in samples):return None
    return {'unit':'scenario','scenarios':len(pairs),'reduction':reduction(pairs),'ci95':[percentile(samples,.025),percentile(samples,.975)]}


def summarize(rows):
    result={}
    for strategy in sorted({r['strategy'] for r in rows}):
        subset=[r for r in rows if r['strategy']==strategy]
        known=all(r['input_tokens'] is not None for r in subset)
        longs=[r for r in subset if r['mode']=='long']
        business_latency=[u['elapsed_ms'] for r in subset for u in r['usage'] if u['kind']=='business']
        result[strategy]={'runs':len(subset),'passed':sum(r['passed'] for r in subset),'success_rate':statistics.mean(int(r['passed']) for r in subset),
            'input_tokens':sum(r['input_tokens'] for r in subset) if known else None,
            'long_input_tokens':sum(r['input_tokens'] for r in longs) if longs and all(r['input_tokens'] is not None for r in longs) else None,
            'model_calls':sum(r['model_calls'] for r in subset),'lookup_calls':sum(r['lookup_calls'] for r in subset),
            'scenario_latency_p50_ms':percentile([r['elapsed_ms'] for r in subset],.5),
            'scenario_latency_p95_ms':percentile([r['elapsed_ms'] for r in subset],.95),
            'business_model_latency_p95_ms':percentile(business_latency,.95),
            'ordinary_round_p95_ms':percentile([m['elapsed_ms'] for r in subset for m in r.get('round_metrics',[]) if not m['compacted']],.95),
            'mean_final_context_estimated_tokens':statistics.mean(r['final_context_estimated_tokens'] for r in subset) if all('final_context_estimated_tokens' in r for r in subset) else None,
            'mean_final_product_duplicate_ratio':statistics.mean(r['final_product_duplicate_ratio'] for r in subset if r.get('final_product_duplicate_ratio') is not None) if any(r.get('final_product_duplicate_ratio') is not None for r in subset) else None,
            'unknown_usage_runs':sum(r['input_tokens'] is None for r in subset)}
    return result



def validate_run_grid(rows,manifest):
    expected={(case,strategy,repetition) for case in manifest['cases'] for strategy in manifest['strategies'] for repetition in range(manifest['repetitions'])}
    keys=[(r['case_id'],r['strategy'],r['repetition']) for r in rows]
    if len(keys)!=len(set(keys)) or set(keys)!=expected or manifest.get('completed')!=len(rows):
        raise ValueError('场景/策略/重复运行记录不完整或存在重复，不生成正式报告')


def main(args):
    case_path=args.input/'cases.json'
    if not case_path.exists():case_path=Path('eval/context/cases.json')
    cases={c['id']:c for c in json.loads(case_path.read_text())}
    rows=json.loads((args.input/'results.json').read_text())
    manifest=json.loads((args.input/'manifest.json').read_text())
    validate_run_grid(rows,manifest)
    rows=score(rows,cases)
    summary=summarize(rows);split=manifest['split']
    report={'grading_version':'context-facts-v5-associations-and-aliases','manifest':manifest,'summary':summary,'failures':[r for r in rows if not r['passed']], 'gate':'INCOMPLETE', 'unmeasured':{'first_recommendation_accuracy':None,'explanation_blind_review':None,'production_ordinary_round_latency':None},'metric_scope':'SKU/材质场景验证固定历史续答的信息保留；首次实际推荐正确率和盲评未测，不能据此宣称首次选品无损。'}
    if split=='dev':
        # 正确率相同时，实际token优先；接近（一个场景的一次重复内）默认压力触发。
        best=max(s['success_rate'] for s in summary.values())
        pressure=summary.get('pressure')
        critical={'dev-sku','dev-cost','dev-current','dev-budget','dev-country'}
        safe=[name for name in summary if not any(r['strategy']==name and (r['case_id'] in critical or cases[r['case_id']].get('critical')) and not r['passed'] for r in rows)]
        eligible=safe or list(summary)
        best=max(summary[name]['success_rate'] for name in eligible)
        selected='pressure' if 'pressure' in eligible and pressure and best-pressure['success_rate']<=1/36+1e-12 else min(eligible,key=lambda k:(-summary[k]['success_rate'],summary[k]['input_tokens'] or float('inf'),summary[k]['scenario_latency_p95_ms']))
        report['timing_selection_rule']='先排除SKU/金额/当前状态/预算/国家关键事实失败；再比成功率，接近且无关键失败时默认pressure'
        report.update(selected_timing=selected,gate='DEV_COMPLETE' if manifest['completed']==108 and manifest.get('code_stable') else 'INCOMPLETE')
        report['paired_intervals']={s:paired_interval(rows,'pressure',s) for s in summary if s!='pressure'}
    else:
        a,c=summary.get('legacy',{}),summary.get('layered',{})
        ratio=1-c['long_input_tokens']/a['long_input_tokens'] if a.get('long_input_tokens') and c.get('long_input_tokens') else None
        new_failures=[]
        for row in rows:
            if row['strategy']=='layered' and not row['passed']:
                peers=[r for r in rows if r['strategy']=='legacy' and r['case_id']==row['case_id'] and r['repetition']==row['repetition']]
                if any(r['passed'] for r in peers):new_failures.append({'case_id':row['case_id'],'repetition':row['repetition']})
        latency_ratio=c['ordinary_round_p95_ms']/a['ordinary_round_p95_ms'] if a.get('ordinary_round_p95_ms') and c.get('ordinary_round_p95_ms') else None
        report.update(long_input_paired_interval=paired_cost_interval(rows,'legacy','layered'),long_input_reduction=ratio,ordinary_round_latency_ratio=latency_ratio,new_failures=new_failures,paired_interval=paired_interval(rows,'legacy','layered'))
        # 场景夹具不是完整交易/线上延迟验收，任何关键回归阻断；不伪造通过。
        eligible=manifest['completed']==252 and manifest.get('code_stable') and c.get('success_rate',0)>=a.get('success_rate',1) and not new_failures and ratio is not None and ratio>=.25 and latency_ratio is not None and latency_ratio<=1.15
        report['gate']='NEEDS_PRODUCTION_ACCEPTANCE' if eligible else 'BLOCK'
    scene_rows=[]
    for case_id in sorted({r['case_id'] for r in rows}):
        per={}
        for name in summary:
            selected=[r for r in rows if r['case_id']==case_id and r['strategy']==name]
            per[name]={'passed':sum(r['passed'] for r in selected),'runs':len(selected),'mean_input_tokens':statistics.mean(r['input_tokens'] for r in selected) if selected and all(r['input_tokens'] is not None for r in selected) else None}
        scene_rows.append({'case_id':case_id,'question':cases[case_id]['question'],'strategies':per})
    report['scenario_pairs']=scene_rows
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# 上下文评测报告',f"\n阶段：{split}；门禁：{report['gate']}。",'\n|策略|通过/运行|累计输入token|模型调用|历史回查|场景P95毫秒|','|---|---:|---:|---:|---:|---:|']
    for name,s in summary.items():lines.append(f"|{name}|{s['passed']}/{s['runs']}|{s['input_tokens'] if s['input_tokens'] is not None else '未知'}|{s['model_calls']}|{s['lookup_calls']}|{s['scenario_latency_p95_ms']}|")
    lines+=['\n## 验证范围','真实模型与AgentScope执行；目录和当前状态为隔离、冻结夹具。快照场景从固定历史续答，长对话实际运行20轮、两次整理、一次数据库重建。交易写路径、真实检索和浏览器恢复另做集成回归。', '\n成本包含摘要与回查后模型调用；无美元单价时不声称节省金额。延迟包含网关排队，场景P95不能冒充买家普通轮P95。币种人民币/RMB规范化为CNY，其余精确字段按规则断言。', '\n失败样本及逐场景差异见 report.json。未通过门禁时保留 legacy。']
    if 'long_input_reduction' in report:lines.append(f"\n长对话输入下降：{report['long_input_reduction']}；按场景配对区间：{report['paired_interval']}。")
    def pct(value):return '未知' if value is None else f'{value*100:.2f}%'
    lines+=['\n## 消耗与延迟明细','|策略|长对话实际输入token|普通轮P95毫秒|结束时上下文估算均值|未提供usage的运行|','|---|---:|---:|---:|---:|']
    for name,values in summary.items():
        cell=lambda key: '未知' if values.get(key) is None else f"{values[key]:,.0f}"
        lines.append(f"|{name}|{cell('long_input_tokens')}|{cell('ordinary_round_p95_ms')}|{cell('mean_final_context_estimated_tokens')}|{values['unknown_usage_runs']}|")
    if split=='dev':
        lines += [f"\n选定时机：`{report['selected_timing']}`。规则：{report['timing_selection_rule']}。",'\n入口有损组仅供实验。开发与留出实现阶段不同，源码各自冻结；开发集普通轮计时和结束时商品重复率未采集，记为未知。']
    else:
        ratio=report['long_input_reduction'];latency=report['ordinary_round_latency_ratio']
        lines += ['\n## 启用门禁','|验收项|结果|状态|','|---|---|---|',
          f"|C任务成功率不低于A|C {pct(c.get('success_rate'))}，A {pct(a.get('success_rate'))}|{'通过' if c.get('success_rate',0)>=a.get('success_rate',1) else '失败'}|",
          f"|不新增失败运行|{len(report['new_failures'])} 次（按相同场景及重复编号与A配对）|{'通过' if not report['new_failures'] else '失败'}|",
          f"|长对话实际输入下降至少25%|下降 {pct(ratio)}；负值表示增加|{'通过' if ratio is not None and ratio>=.25 else '失败或未知'}|",
          f"|普通轮P95不劣化超过15%|C/A = {latency if latency is not None else '未知'}|{'通过' if latency is not None and latency<=1.15 else '失败或未知'}|",
          f"\n任务成功率差异（C−A），按场景配对95%区间：`{report['paired_interval']}`。",
          f"\n长对话实际输入下降，按场景配对95%区间：`{report['long_input_paired_interval']}`。",
          '\n## 失败样本','|场景|策略|重复编号|检查|','|---|---|---:|---|']
        for row in report['failures']:
            failed=', '.join(k for k,v in row['checks'].items() if not v)
            lines.append(f"|{row['case_id']}|{row['strategy']}|{row['repetition']}|{failed or row.get('error')}|")
        if not report['failures']:lines.append('|无|—|—|—|')
    lines += ['\n## 逐场景配对','|场景|'+'|'.join(summary)+'|','|---|'+'|'.join('---:' for _ in summary)+'|']
    for item in scene_rows:
        cells=[]
        for name in summary:
            value=item['strategies'][name];cost='未知' if value['mean_input_tokens'] is None else f"{value['mean_input_tokens']:,.0f}"
            cells.append(f"{value['passed']}/{value['runs']}；输入均值 {cost}")
        lines.append('|'+item['case_id']+'|'+'|'.join(cells)+'|')
    lines += ['\n## 判分及未测项','判分同时检查关键值及其所属SKU、历史/当前时间和费用字段。错误互换不能因为两个数字都出现而通过。无法解析的字段归属标为unknown并阻断通过；明确同义表达统一归一，原始判分与答案保留。',
      '\n首次实际推荐正确率、独立解释盲评和生产线上普通轮延迟未测。固定历史的SKU/材质续答并不等价于首次推荐评测。静态搜索批次是历史夹具，当前权威工具提供更新后的价格/库存；真实检索与交易端到端由独立业务回归验证。',
      '\n取消的开发/留出预检位于 ../preflight，不混入正式360次场景统计；中断时未落盘的在途调用消耗未知，不把这些未知消耗记零。']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ['failures','manifest','scenario_pairs']},ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
