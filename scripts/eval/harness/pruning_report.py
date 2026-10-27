"""裁剪实验的精简报告；费用未知时不能用Token降幅替代收益。"""
from html import escape
import json
import statistics
from scripts.eval.harness.contracts import write_json


def total(values):
    values = list(values)
    return sum(values) if values and all(isinstance(v, (float, int)) for v in values) else None


def percentile(values, q=.95):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo = int(pos)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (pos - lo)


def aggregate(rows):
    samples = [s for r in rows for s in r['usage']]
    count_complete = len(samples) == len(rows)
    return {'calls': len(rows), 'passed': sum(r['passed'] for r in rows),
        'input_tokens': total(s.get('input_tokens') for s in samples) if count_complete else None,
        'output_tokens': total(s.get('output_tokens') for s in samples) if count_complete else None,
        'cache_read_tokens': total(s.get('prompt_cache', {}).get('cache_read_tokens') for s in samples) if count_complete else None,
        'cache_write_tokens': total(s.get('prompt_cache', {}).get('cache_write_tokens') for s in samples) if count_complete else None,
        'ttft_p95_ms': percentile([s.get('ttft_ms') for s in samples]),
        'latency_p95_ms': percentile([r['elapsed_ms'] for r in rows]),
        'elapsed_ms': total(r['elapsed_ms'] for r in rows),
        'input_side_cost': None, 'total_cost': None,
        'response_models': sorted({s.get('prompt_cache', {}).get('response_model')
                                   for s in samples if s.get('prompt_cache', {}).get('response_model')}),
        'identity_valid': bool(samples) and all(s.get('prompt_cache', {}).get('response_model_matches') is True for s in samples),
        'protocol_valid': bool(samples) and all(s.get('prompt_cache', {}).get('protocol_status') == 'valid' for s in samples)}


def fmt(value, ms=False):
    if value is None:
        return '未知'
    return f'{value/1000:.2f}s' if ms else f'{value:,.0f}'


def render(output, manifest, rows, trajectories):
    business = [r for r in rows if r['phase'] == 'business']
    groups = {a: aggregate([r for r in business if r['arm'] == a]) for a in 'ABC'}
    summary = {'status': manifest['status'], 'groups': groups,
               'preparation': aggregate([r for r in rows if r['phase'] == 'capture']),
               'complete': len(business) == manifest['planned_business_requests'],
               'release_allowed': False, 'conclusion': '诊断观察；模型身份与费用口径未核实，不能判断BP真实收益或上线。'}
    write_json(output / 'summary.json', summary)
    names = {'A': '不开BP · 隐式缓存', 'B': '固定BP', 'C': '裁剪感知BP'}
    table = ''
    for a, g in groups.items():
        table += f'<tr><td>{a} {names[a]}</td><td>{g["passed"]}/{g["calls"]}</td><td>{fmt(g["input_tokens"])}</td><td>{fmt(g["cache_read_tokens"])}</td><td>{fmt(g["cache_write_tokens"])}</td><td>{fmt(g["latency_p95_ms"],True)}</td><td>未知</td></tr>'
    stages = ''
    for case in trajectories:
        for step in range(6):
            cells = []
            for a in 'ABC':
                g = aggregate([r for r in business if r['case'] == case and r['step'] == step and r['arm'] == a])
                cells.append(f'<td>{fmt(g["cache_read_tokens"])} / {fmt(g["cache_write_tokens"])} / {fmt(g["ttft_p95_ms"],True)}</td>')
            event = next((f['archived_results'] for f in trajectories[case] if f['step'] == step), 0)
            stages += f'<tr><td>{escape(case)} Q{step}{" · 归档"+str(event)+"块" if event else ""}</td>' + ''.join(cells) + '</tr>'
    details, graph = '', ''
    chosen = next((c for c in ('middle', 'continuous') if c in trajectories), None)
    if chosen:
        # 图使用当前真实C组Q1/Q2最终HTTP消息索引，不把本地相同前缀当作缓存命中。
        paired = [next((r for r in business if r['case'] == chosen and r['arm'] == 'C'
                        and r['repetition'] == 0 and r['step'] == s), None) for s in (1, 2)]
        bodies = [json.loads((output / r['request_file']).read_text()) if r and (output / r['request_file']).exists() else None for r in paired]
        if all(bodies):
            from app.infrastructure.prompt_cache import normalized_cache_messages
            left, right = [normalized_cache_messages(b['messages']) for b in bodies]
            first = next((i for i, (x,y) in enumerate(zip(left,right)) if x != y), min(len(left),len(right)))
            lines = ['flowchart LR', '  subgraph BEFORE["Q1：裁剪前"]']
            for column, (row, body) in enumerate(zip(paired,bodies)):
                prefix = 'B' if column == 0 else 'A'
                if column:
                    lines += ['  end', '  subgraph AFTER["Q2：裁剪后"]']
                items = [0, *[m['message_index'] for m in row['markers']], first, len(body['messages'])-1]
                items = sorted(set(i for i in items if i < len(body['messages'])))
                previous = None
                for i in items:
                    marker = next((m['role'] for m in row['markers'] if m['message_index']==i), '')
                    label = f'm{i} '+body['messages'][i]['role']+(f' · BP {marker}' if marker else '')+(' · 最早变化' if i==first else '')
                    lines.append(f'    {prefix}{i}["{label}"]')
                    if previous is not None:
                        lines.append(f'    {prefix}{previous} --> {prefix}{i}')
                    previous=i
            lines += ['  end', '  B0 -. "前缀仍相同 ≠ 已命中缓存" .-> A0']
            mermaid = '\n'.join(lines)
            (output / 'boundaries.mmd').write_text(mermaid)
            graph = f'<p>本次最早改写位置：消息 <b>m{first}</b>（索引从0开始）。边界是否此前发送、TTL状态见原始标记账本；不能跨过这一位置复用后面的旧前缀。</p><pre class="mermaid">{escape(mermaid)}</pre>'
            for row, body in zip(paired,bodies):
                lines = []
                for i, msg in enumerate(body['messages']):
                    snippet = json.dumps(msg.get('content'), ensure_ascii=False)
                    lines.append(f'm{i} {msg["role"]}'+(' [BP]' if i in row['marker_indexes'] else '')+' '+snippet[:220]+('…' if len(snippet)>220 else ''))
                details += f'<details><summary>C组 Q{row["step"]} 消息列表（长内容省略）</summary><pre>'+escape('\n'.join(lines))+'</pre><a href="'+escape(row['request_file'])+'">完整最终HTTP请求</a></details>'
    failures = [r for r in rows if not r['passed']]
    observed_models = sorted({m for g in groups.values() for m in g['response_models']})
    html = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>裁剪后 BP 对照</title><style>
body{{font:16px/1.65 system-ui;background:#f6f7f5;color:#182820;margin:0}}
main{{max-width:1150px;margin:auto;padding:42px 28px}}h1{{font-size:32px;margin:0 0 12px}}h2{{margin-top:32px;font-size:21px}}
.notice{{border-left:4px solid #b5802a;background:#fff8e9;padding:16px 20px}}table{{border-collapse:collapse;width:100%;background:white}}
td,th{{padding:12px;text-align:left;border-bottom:1px solid #dce2dc}}th{{background:#e9efea}}pre{{white-space:pre-wrap;word-break:break-word;background:white;padding:18px;font-size:12px}}
details{{margin:14px 0}}.small{{font-size:14px;color:#56685d}}a{{color:#14683e}}</style><main>
<h1>工具裁剪后，BP 有增量收益吗？</h1>
<div class="notice"><b>结论：尚不能证明真实收益，保持默认关闭。</b><br>本报告是 {len(business)}/{manifest["planned_business_requests"]} 次业务请求的诊断。
费用、缓存写入语义与模型身份仍需核实。状态：{escape(manifest["status"])}。</div>
<p class="small">同一条原生治理轨迹，三组只改变标记与等长隔离标识。请求模型 {escape(manifest["model"])}；实际返回标识 {escape(", ".join(observed_models) or "未知")}。全部工具执行数为0。</p>
<table><tr><th>策略</th><th>正确/请求</th><th>实际输入Token</th><th>缓存读Token</th><th>缓存写Token</th><th>请求P95</th><th>输入侧费用</th></tr>{table}</table>
<p class="small">输入Token包含缓存部分时，不会因为命中而自动减少；不能用输入Token降幅代替费用。P95包含全部请求，样本小，仅作诊断。</p>
<h2>裁剪前后发生了什么</h2><table><tr><th>阶段</th><th>A 读 / 写 / 首字P95</th><th>B 读 / 写 / 首字P95</th><th>C 读 / 写 / 首字P95</th></tr>{stages}</table>
<h2>BP 位置与实际消息</h2>{graph}{details}
<details><summary>原始失败：{len(failures)}条</summary><pre>{escape(json.dumps(failures,ensure_ascii=False,indent=2))}</pre></details>
<details><summary>实验范围与冻结配置</summary><pre>{escape(json.dumps({k:v for k,v in manifest.items() if k!="source"},ensure_ascii=False,indent=2))}</pre></details>
<p>采集准备调用：{summary["preparation"]["calls"]}次，独立计量；不是A/B/C额外预热。完整费用未知。源代码指纹：{manifest["source"]["sha256"][:16]}。</p>
<p><a href="results.json">完整调用账本</a> · <a href="manifest.json">源码与配置</a> · <a href="summary.json">汇总数据</a> · <a href="boundaries.mmd">Mermaid源图</a></p>
</main><script type="module">import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';mermaid.initialize({{startOnLoad:true,securityLevel:'strict',theme:'neutral'}});</script></html>'''
    (output / 'report.html').write_text(html, encoding='utf-8')

