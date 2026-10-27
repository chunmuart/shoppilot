"""可离线打开的 HTML；所有模型文本转义，报告无需 CDN。"""
import json
import re
from html import escape
from pathlib import Path

from scripts.eval.harness.contracts import write_json
from scripts.eval.harness.metrics import summarize


def cell(value, unit=''):
    if value is None:return '<span class="unknown">未知</span>'
    if isinstance(value,float):value=f'{value:,.2f}'
    elif isinstance(value,int):value=f'{value:,}'
    return escape(str(value)+unit)


def render(output: Path, manifest, rows):
    report=summarize(manifest,rows)
    write_json(output/'summary.json',report)
    write_json(output/'results.json',rows)
    e=escape
    status='进行中' if manifest['status']=='running' else '已中断' if manifest['status'] in ('cancelled','failed') else '已完成'
    gate='实验收益已验证' if report['gates'] and all(g['status']=='BENEFIT_VERIFIED' for g in report['gates'].values()) else '未满足上线门禁'
    parts=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
      '<title>ShopPilot · Harness 评测报告</title><style>',
      ':root{color-scheme:light;--ink:#203c3a;--muted:#637573;--line:#dce4de;--accent:#147d70;--paper:#f5f7f2}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 system-ui,-apple-system,sans-serif}main{max-width:1240px;margin:auto;padding:44px 28px}a{color:var(--accent)}.eyebrow{letter-spacing:.13em;font-size:12px;color:var(--muted)}h1{font-size:36px;line-height:1.25;margin:12px 0}h2{font-size:22px;margin:0 0 18px}h3{font-size:17px}p{margin:8px 0}.muted,.unknown{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:30px 0}.card,section{background:white;border:1px solid var(--line);border-radius:16px;padding:22px}.card b{font-size:25px;display:block;margin-top:10px}.card small{color:var(--muted)}section{margin:22px 0}.pill{display:inline-block;padding:4px 12px;border-radius:30px;background:#e4eee8;margin:4px}.warn{background:#fff0df;color:#834515}.ok{background:#def2e8;color:#216847}.bad{color:#a33731}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}td.num{font-variant-numeric:tabular-nums}.bar{background:#e8eee9;height:8px;border-radius:9px;margin-top:6px}.bar i{display:block;background:var(--accent);height:8px;border-radius:9px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace;background:#f2f5f1;padding:14px;border-radius:8px}details{border-top:1px solid var(--line);padding:15px 0}summary{cursor:pointer}select,input{font:inherit;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:white;color:var(--ink)}.filters{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}code{font-size:13px;overflow-wrap:anywhere}.note{border-left:3px solid #d1a958;padding-left:12px;color:#745327}.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}ul{padding-left:22px}footer{color:var(--muted);font-size:13px;margin-top:30px}@media(max-width:750px){main{padding:24px 14px}.cards,.grid{grid-template-columns:1fr 1fr}h1{font-size:28px}.card,section{padding:16px}}@media(max-width:420px){.cards,.grid{grid-template-columns:1fr}}',
      '</style></head><body><main><div class="eyebrow">SHOPPILOT / ENGINEERING EVALUATION</div>',
      '<h1>Harness 上下文治理评测</h1>',
      f'<p class="muted">运行 {e(manifest["run_id"])} · {e(manifest["profile"])} · {e(manifest.get("model","未知"))}</p>',
      f'<span class="pill">{status}</span><span class="pill warn">{gate}</span><span class="pill">不自动发布</span>',
      '<div class="cards">',
      f'<div class="card"><small>场景运行</small><b>{len(rows)} / {len(manifest["cases"])*len(manifest["strategies"])*manifest["repetitions"]}</b><small>按场景 × 策略 × 重复次数</small></div>',
      f'<div class="card"><small>确定性安全回归</small><b>{cell(manifest.get("contracts",{}).get("passed_count"))}</b><small>仅契约验证，不冒充模型效果</small></div>',
      f'<div class="card"><small>失败场景运行</small><b>{report["failure_count"]}</b><small>保留最初失败与异常</small></div>',
      f'<div class="card"><small>统计单位</small><b>{len(manifest["cases"])} 个场景</b><small>重复运行先在场景内聚合</small></div></div>',
      '<section><h2>先看结论</h2>']
    if manifest.get('error'):
        parts.append('<p class="note">评测未完成：'+e(manifest['error'])+'</p>')
    if manifest.get('missing_configuration'):
        parts.append('<p class="note">缺少配置：'+e('、'.join(manifest['missing_configuration']))+
                     '。GitHub Actions 需配置对应的 HARNESS_LLM_API_KEY / HARNESS_LLM_BASE_URL secrets。</p>')
    for name,g in report['gates'].items():
        parts.append(f'<h3>{e(name)}：{e(g["status"])}</h3><ul>'+''.join('<li>'+e(reason)+'</li>' for reason in g['reasons'])+'</ul>')
    parts.append('<h3>服务模型与工具合同</h3><table><tr><th>策略</th><th>响应模型</th><th>标识不符 / 未知</th><th>工具协议异常 / 未知</th><th>已有证据直接回答</th></tr>')
    for name,s in report['strategies'].items():
        d=s['model_contract']
        parts.append('<tr><td>'+e(name)+'</td><td>'+e(', '.join(d['response_models']) or '未知')+'</td><td>'+cell(d['mismatched_identity_calls'])+' / '+cell(d['unknown_identity_calls'])+'</td><td>'+cell(d['protocol_violation_calls'])+' / '+cell(d['unknown_protocol_calls'])+'</td><td>'+cell(s['efficiency_diagnostics']['visible_evidence_answers'])+'</td></tr>')
    parts.append('</table><p class="muted">响应model只是服务端标识，不等于已验证实际权重。标识不符或未知阻断收益门禁；历史报告缺字段记未知，不补零。</p>')
    parts += ['<p class="note">缓存读取量、输入 token、账单金额是不同指标。金额币种或 usage 不完整时，费用收益显示未知；单测通过与冒烟通过均不授予上线结论。</p></section>',
      '<section><h2>质量、完整消耗与体验</h2><div class="scroll"><table><thead><tr><th>策略</th><th>通过 / 运行</th><th>累计实际输入</th><th>摘要 / 全部调用</th><th>回查次数</th><th>普通轮 P95</th><th>模型首字 P95</th><th>费用总量</th></tr></thead><tbody>']
    for name,s in report['strategies'].items():
        ratio=100*(s['success_rate'] or 0)
        pricing=manifest.get('pricing',{})
        cost=cell(s['reported_cost']['total'],' '+str(pricing.get('currency'))) if all(pricing.get(k) for k in ('verified','currency','source','verified_at')) else '未知'
        parts.append(f'<tr><td>{e(name)}</td><td>{s["passed"]} / {s["runs"]}<div class="bar"><i style="width:{ratio:.2f}%"></i></div></td><td>{cell(s["input_tokens"]["total"])}<br><small class="muted">含输出总量 {cell(s["total_tokens"]["total"])}<br>未知调用 {s["input_tokens"]["unknown"]}</small></td><td>{s["summary_calls"]} / {s["physical_model_calls"]}</td><td>{s["lookup_calls"]}</td><td>{cell(s["ordinary_round_p95_ms"]," ms")}</td><td>{cell(s["model_ttft_p95_ms"]," ms")}<br><small class="muted">有效调用 {s["model_ttft_known_calls"]}</small></td><td>{cost}</td></tr>')
    parts += ['</tbody></table></div><p class="muted">累计 token 包括摘要、重试和回查后的模型输入。普通轮耗时包含工具与排队；模型首字时间不含外层排队，非流式不伪造首字时间。完整金额口径尚未确认，网关数值仅保存在原始 JSON。</p></section>',
      '<section><h2>按场景配对的收益区间</h2><div class="scroll"><table><tr><th>对比 current</th><th>输入降幅 / 95% CI</th><th>完整成本场景对数</th><th>成功率差 / 95% CI</th></tr>']
    def interval(data):
        if data['estimate'] is None:return '未知'
        if data['ci95'] is None:return f'{data["estimate"]*100:.2f}% / 场景数不足，区间未知'
        low,high=data['ci95']
        return f'{data["estimate"]*100:.2f}% / [{low*100:.2f}%, {high*100:.2f}%]'
    for name,pair in report['paired_intervals'].items():
        parts.append(f'<tr><td>{e(name)}</td><td>{interval(pair["input_reduction"])}</td><td>{pair["input_reduction"]["scenarios"]}</td><td>{interval(pair["success_difference"])}</td></tr>')
    parts+=['</table></div><p class="muted">4,000 次 bootstrap，以场景为单位。缺失配对不补零；完整配对不足会阻断门禁。单场景不计算区间，负降幅代表消耗增加。</p></section>']
    parts+=['<section><h2>缓存与前缀诊断</h2><div class="scroll"><table><tr><th>策略</th><th>实际缓存读取 / 输入占比</th><th>可比较调用</th><th>系统变化 / 工具变化</th><th>历史改写 / 追加</th><th>首次 / 后续 / 重建后首字 P95</th></tr>']
    for name,s in report['strategies'].items():
        d=s['prefix_diagnostics']; t=s['replay_stage_ttft_p95_ms']
        ratio=s['cache_read_ratio']
        parts.append('<tr><td>'+e(name)+'</td><td>'+cell(s['cache_read_tokens']['total'])+' / '+cell(ratio*100 if ratio is not None else None,'%')+'</td><td>'+cell(d['comparable_calls'])+'</td><td>'+cell(d['system_changes'])+' / '+cell(d['tool_changes'])+'</td><td>'+cell(d['history_rewrites'])+' / '+cell(d['append_calls'])+'</td><td>'+' / '.join(cell(t[k],' ms') for k in ('initial','followup','after_rebuild'))+'</td></tr>')
    parts+=['</table></div><p class="muted">前缀比较使用同模型、同买家会话、同调用类型的规范 JSON；只有带随机密钥的摘要与计数，不记录买家正文。重建/模型重建可能正常改写前缀或开始新比较。完整消息字节前缀不是供应商缓存 token，也不证明未命中的原因。passthrough 仍可能命中隐式缓存。固定轨迹的首次调用不保证供应商完全冷缓存；其合成摘要没有真实摘要成本，不能代替长对话评测。</p></section>']
    parts+=['<section><h2>调用增量与治理归因</h2><div class="scroll"><table><tr><th>策略</th><th>回查错误 / 观测</th><th>参数归一化 / 待续页</th><th>状态写入 / 跳过重复</th><th>整理改写批次 / 归档结果</th><th>模型耗时 P95 / 首字后 P95</th></tr>']
    for name, summary in report['strategies'].items():
        d=summary['efficiency_diagnostics']
        parts.append('<tr><td>'+e(name)+'</td><td>'+cell(d['lookup_errors'])+' / '+cell(d['lookup_observed'])+'</td><td>'+cell(d['lookup_normalized'])+' / '+cell(d['lookup_pages_with_more'])+'</td><td>'+cell(d['state_emitted'])+' / '+cell(d['state_skipped'])+'</td><td>'+cell(d['prune_rewrites'])+' / '+cell(d['archived_results'])+'</td><td>'+cell(d['model_elapsed_p95_ms'],' ms')+' / '+cell(d['after_first_text_p95_ms'],' ms')+'</td></tr>')
    parts+=['</table></div><p class="muted">没有诊断数据的项目显示未知。首字后耗时只统计具备真实流式首字记录的调用，不能代表全部调用的解码时间。参数归一化不会扩大页大小或绕过买家归属。</p>']
    for name, summary in report['strategies'].items():
        d=summary['efficiency_diagnostics']
        parts.append('<details><summary>'+e(name)+' · 请求原因与分区估算</summary><pre>'+e(json.dumps({'observed_requests':d['measured_requests'],'request_reasons':d['request_reasons'],'estimated_sections':d['estimated_sections']},ensure_ascii=False,indent=2))+'</pre></details>')
    parts+=['<p class="muted">分区累计值是原生消息层估算，用于发现重复内容；不是供应商实际输入，不用于计算账单收益。摘要等没有经过该观测层的调用仍计入实际 usage，不能从分区估算反推全部成本。</p></section>']
    parts+=['<section><h2>摘要与工作集诊断</h2><div class="scroll"><table><tr><th>策略</th><th>摘要实际输入 / 输出</th><th>自动压缩轮 P95</th><th>手动整理 P95</th><th>工作集均值（估算）</th><th>商品重复率均值</th></tr>']
    for name,s in report['strategies'].items():
        summary=s['usage_by_kind'].get('summary',{})
        parts.append('<tr><td>'+e(name)+'</td><td>'+cell(summary.get('input_tokens',{}).get('total'))+' / '+cell(summary.get('output_tokens',{}).get('total'))+'</td><td>'+cell(s['compacted_round_p95_ms'],' ms')+'</td><td>'+cell(s['manual_compaction_p95_ms'],' ms')+'</td><td>'+cell(s['mean_final_context_estimated_tokens'])+'</td><td>'+cell(None if s['mean_final_product_duplicate_ratio'] is None else s['mean_final_product_duplicate_ratio']*100,'%')+'</td></tr>')
    parts+=['</table></div><p class="muted">没有发生或未计量的项目显示未知。手动整理在买家轮次之间执行，单列耗时；整段场景耗时和累计 usage 均包含整理。估算工作集与实际输入不能混用。</p></section>',
      '<section><h2>逐场景证据与失败样本</h2><div class="filters"><label>范围 <select id="status"><option value="all">全部</option><option value="failed">仅失败</option></select></label><label>层级 <select id="layer"><option value="all">全部</option><option value="context">上下文续答</option><option value="agent">生产工厂工作流</option><option value="cache_replay">固定请求轨迹</option><option value="skill_replay">Skill目录轨迹</option></select></label><input id="query" placeholder="搜索场景、策略" aria-label="搜索场景"></div>']
    for row in rows:
        failed=[k for k,v in row.get('checks',{}).items() if not v]
        outcome='通过' if row['passed'] else '失败'
        desc=f'{row["case_id"]} · {row["strategy"]} · 重复 {row["repetition"]+1}'
        parts.append(f'<details class="scenario" data-failed="{str(not row["passed"]).lower()}" data-layer="{e(row["layer"],quote=True)}" data-query="{e(desc.lower(),quote=True)}"><summary><span class="{ "ok" if row["passed"] else "bad"}">{outcome}</span> · {e(desc)}</summary>')
        parts.append('<p>失败检查：'+e('、'.join(failed) or '无')+'；异常：'+e(row.get('error') or '无')+'</p>')
        parts.append('<p>输入：'+cell(total_input(row))+'；耗时：'+cell(row.get('elapsed_ms'),' ms')+'</p>')
        trace = row.get('evidence_trace', '')
        if re.fullmatch(r'traces/[a-z0-9_-]+\.jsonl', trace):
            parts.append('<p><a href="'+e(trace,quote=True)+'">合成评测原始消息与工具结果</a>（仅本次隔离夹具）</p>')
        if row.get('answer'):parts.append('<pre>'+e(row['answer'])+'</pre>')
        if row.get('context_diagnostics'):
            parts.append('<details><summary>工具与整理诊断</summary><pre>'+e(json.dumps(row['context_diagnostics'],ensure_ascii=False,indent=2))+'</pre></details>')
        for turn in row.get('transcript',[]):
            parts.append('<pre>买家：'+e(turn['user'])+'\n\nAgent：'+e(turn['assistant'])+'</pre>')
        parts.append('</details>')
    if report['missing']:parts.append('<p class="note">还有 '+str(len(report['missing']))+' 次预定运行没有结果，不能作为完整评测。</p>')
    parts+=['</section><section><h2>冻结配置与复现</h2>',
       '<p>源码指纹：<code>'+e(manifest['source']['sha256'])+'</code></p>',
       '<p>评测集：<code>'+e(manifest['dataset_sha256'])+'</code></p>',
       '<p>边界：'+e('；'.join(manifest.get('limits',[])))+'</p>',
       '<p><a href="manifest.json">完整配置</a> · <a href="results.json">原始场景结果</a> · <a href="summary.json">指标与门禁 JSON</a> · <a href="attempts.jsonl">逐次调用账本</a> · <a href="contracts.log">契约测试日志</a></p>',
       '</section><footer>自动生成 · synthetic fixtures only · 不连接真实买家数据库。原始失败保留，复核必须使用新 run_id。</footer></main>',
       '<script>const controls=["status","layer","query"].map(id=>document.getElementById(id));function filter(){const[s,l,q]=controls.map(x=>x.value.toLowerCase());document.querySelectorAll(".scenario").forEach(el=>{el.hidden=(s==="failed"&&el.dataset.failed!=="true")||(l!=="all"&&el.dataset.layer!==l)||!el.dataset.query.includes(q)})}controls.forEach(el=>el.addEventListener("input",filter));</script></body></html>']
    temp=output/'report.html.tmp';temp.write_text('\n'.join(parts),encoding='utf-8');temp.replace(output/'report.html')
    return report


def total_input(row):
    from scripts.eval.harness.metrics import total
    return total([row],'input_tokens')['total']
