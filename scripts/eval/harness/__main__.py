"""python -m scripts.eval.harness：每次 Harness 修改的统一评测入口。"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess

from scripts.eval.harness.contracts import ROOT, SUITE, affected_paths, load_suite, source_manifest, write_json
from scripts.eval.harness.report import render
from scripts.eval.harness.runner import run_profile


def verify_run(path, minimum='smoke'):
    manifest=json.loads((path/'manifest.json').read_text())
    rows=json.loads((path/'results.json').read_text())
    report=render(path,manifest,rows)
    reasons=[]
    if manifest['source']['sha256']!=source_manifest()['sha256']:reasons.append('评测后 Harness/评测源码或测试已改变，必须重跑')
    allowed={'smoke':{'smoke','dev','release'},'release':{'release'}}[minimum]
    if manifest['profile'] not in allowed:reasons.append('评测档位不足；诊断不能替代完整评测')
    if manifest['status']!='completed' or manifest['source_stable'] is not True:reasons.append('运行未完整完成或源码不稳定')
    if not report['matrix_complete'] or report['failure_count']:reasons.append('场景缺失或失败')
    if not manifest.get('contracts',{}).get('passed'):reasons.append('安全契约未通过')
    if minimum=='release' and (not report['gates'] or any(g['status']!='BENEFIT_VERIFIED' for g in report['gates'].values())):reasons.append('未证明真实收益')
    return {'passed':not reasons,'reasons':reasons,'report':str(path/'report.html')}


def compare_runs(baseline, candidate, output, baseline_strategy='current', candidate_strategy='current'):
    a=json.loads((baseline/'manifest.json').read_text());b=json.loads((candidate/'manifest.json').read_text())
    # 不同源码版本是比较目标；其它变量不同则不能冒充 Harness 改动收益。
    fields=['model','gateway_sha256','dataset_sha256','runtime','execution_environment','temperature','cases','repetitions','pricing']
    differences=[f for f in fields if a.get(f)!=b.get(f)]
    if differences:raise ValueError('跨版本对照的非代码变量不一致：'+', '.join(differences))
    if a['status']!='completed' or b['status']!='completed':raise ValueError('跨版本对照要求两次运行完成')
    if baseline_strategy not in a['strategies'] or candidate_strategy not in b['strategies']:
        raise ValueError('跨版本对照的策略不存在')
    from scripts.eval.harness.contracts import assert_output_path
    output=assert_output_path(output);output.mkdir(parents=True)
    rows=[]
    for path,label,strategy in ((baseline,'current',baseline_strategy),(candidate,'candidate',candidate_strategy)):
        original=json.loads((path/'results.json').read_text())
        selected=[r for r in original if r['strategy']==strategy]
        for row in selected:
            mapped = {**row, 'strategy': label}
            trace = row.get('evidence_trace')
            if trace:
                # 跨版本报告必须自带证据；组名前缀避免两个源目录的同名文件互相覆盖。
                if not re.fullmatch(r'traces/[a-z0-9_-]+\.jsonl', trace):
                    raise ValueError('跨版本证据路径不安全')
                source = (path / trace).resolve()
                if not source.is_relative_to(path.resolve()) or not source.is_file():
                    raise ValueError('跨版本证据缺失或超出原运行目录')
                target = output / 'traces' / (label + '-' + Path(trace).name)
                target.parent.mkdir(exist_ok=True)
                target.touch(mode=0o600, exist_ok=False)
                target.write_bytes(source.read_bytes())
                mapped['evidence_trace'] = str(target.relative_to(output))
            rows.append(mapped)
    manifest={**b,'run_id':'compare-'+a['run_id']+'-'+b['run_id'],'profile':'cross_version',
              'strategies':['current','candidate'],'source_stable':a['source_stable'] and b['source_stable'],
              'policy_overrides':{'current':a['policy_overrides'][baseline_strategy],'candidate':b['policy_overrides'][candidate_strategy]},
              'comparison_strategies':[baseline_strategy,candidate_strategy],
              'contracts':{**b.get('contracts',{}),'passed':bool(a.get('contracts',{}).get('passed') and b.get('contracts',{}).get('passed'))},
              'comparison_sources':[a['source']['sha256'],b['source']['sha256']],
              'limits':b['limits']+['跨版本独立时段，不是交错实验，延迟可能受网关负载干扰；此报告不自动批准发布']}
    write_json(output/'manifest.json',manifest)
    (output/'contracts.log').write_text('原始安全回归结果分别见两个源运行目录。\n')
    with (output/'attempts.jsonl').open('w') as ledger:
        for row in rows:
            for sample in row.get('usage',[]):
                ledger.write(json.dumps({'case_id':row['case_id'],'strategy':row['strategy'],
                    'repetition':row['repetition'],'sample':sample},ensure_ascii=False)+'\n')
    render(output,manifest,rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run')
    run.add_argument('--profile',choices=['contracts','smoke','dev','release','cache_probe','efficiency','long_efficiency'],default='smoke')
    run.add_argument('--suite',type=Path,default=SUITE)
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--strategies',default='current,candidate')
    run.add_argument('--case',action='append',help='诊断选择；使用后报告不再属于完整 smoke/release')
    run.add_argument('--skip-contracts',action='store_true',help='仅诊断，不能通过提交验证')
    run.add_argument('--require-benefit',action='store_true',help='收益门禁未通过时返回非零')
    report=commands.add_parser('report');report.add_argument('directory',type=Path)
    verify=commands.add_parser('verify');verify.add_argument('directory',type=Path);verify.add_argument('--minimum',choices=['smoke','release'],default='smoke')
    impact=commands.add_parser('impact');impact.add_argument('--base');impact.add_argument('--files',nargs='*')
    comparison=commands.add_parser('compare');comparison.add_argument('--baseline',type=Path,required=True);comparison.add_argument('--candidate',type=Path,required=True);comparison.add_argument('--output',type=Path,required=True)
    comparison.add_argument('--baseline-strategy',default='current');comparison.add_argument('--candidate-strategy',default='current')
    args=parser.parse_args()
    try:
        if args.command=='run':
            result=asyncio.run(run_profile(args.profile,args.output,suite_path=args.suite,
                strategies=args.strategies.split(','),case_ids=args.case,skip_contracts=args.skip_contracts))
            manifest=json.loads((args.output/'manifest.json').read_text())
            failed=manifest['status']!='completed' or manifest['source_stable'] is not True or not result['matrix_complete'] or result['failure_count']>0
            if args.require_benefit:failed|=not result['gates'] or any(g['status']!='BENEFIT_VERIFIED' for g in result['gates'].values())
            print('HTML: '+str((args.output/'report.html').resolve()))
            return 2 if failed else 0
        if args.command=='report':
            path=args.directory
            manifest=json.loads((path/'manifest.json').read_text())
            # SIGKILL 等无法执行 finally 的情况：允许从逐场景文件重建不完整报告。
            rows=[json.loads(p.read_text()) for p in sorted((path/'rows').glob('*.json'))]
            if not (path/'rows').exists():rows=json.loads((path/'results.json').read_text())
            render(path,manifest,rows);print(path/'report.html');return 0
        if args.command=='verify':
            result=verify_run(args.directory,args.minimum);print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 2
        if args.command=='compare':compare_runs(args.baseline,args.candidate,args.output,args.baseline_strategy,args.candidate_strategy);return 0
        paths=args.files
        if paths is None:
            if not args.base:raise ValueError('impact 需要 --base 或 --files')
            paths=subprocess.check_output(['git','diff','--name-only',args.base],cwd=ROOT,text=True).splitlines()
            paths+=subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines()
        hits=affected_paths(paths)
        print(json.dumps({'harness_changed':bool(hits),'files':hits,'required':['contracts','smoke'] if hits else [],
             'release':'参数、策略或模型调整上线前需 release --require-benefit'},ensure_ascii=False,indent=2));return 0
    except KeyboardInterrupt:
        print('评测已中断；已结算调用和不完整 HTML 保留。');return 130
    except (ValueError,FileNotFoundError) as error:
        print(str(error));return 2


if __name__=='__main__':raise SystemExit(main())
