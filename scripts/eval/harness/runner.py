"""统一运行、逐请求落盘和完成/失败/取消后的 HTML 汇总。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET

from scripts.eval.harness.contracts import ROOT, SCHEMA_VERSION, assert_output_path, digest, execution_environment, load_suite, source_manifest, write_json
from scripts.eval.harness.report import render


async def contract_checks(suite, output):
    start=time.monotonic()
    with (output/'contracts.log').open('w') as log:
        test_environment={k:v for k,v in os.environ.items() if k!='PYTEST_ADDOPTS'}
        process=await asyncio.create_subprocess_exec(sys.executable,'-m','pytest','-q',
            *suite['contract_tests'],'--junitxml='+str(output/'contracts.xml'),cwd=ROOT,env=test_environment,stdout=log,stderr=log)
        try:code=await process.wait()
        except BaseException:
            process.terminate()
            await process.wait()
            raise
    counts={'tests':0,'failures':0,'errors':0,'skipped':0}
    covered=set()
    if (output/'contracts.xml').exists():
        for group in ET.parse(output/'contracts.xml').getroot().iter('testsuite'):
            for key in counts:counts[key]+=int(group.get(key,'0'))
            for test in group.iter('testcase'):covered.update(test.get('classname','').split('.'))
    missing={Path(p).stem for p in suite['contract_tests']}-covered
    return {**counts,'exit_code':code,'missing_test_modules':sorted(missing),
            'passed':code==0 and counts['tests']>0 and counts['skipped']==0 and not missing,
            'passed_count':counts['tests']-counts['failures']-counts['errors']-counts['skipped'],
            'elapsed_ms':(time.monotonic()-start)*1000}


def append_attempt(path, identity, sample):
    # 逐请求结算即落盘；取消时在途未结算仍为未知，不借续跑把未知费用抹掉。
    with path.open('a') as stream:
        stream.write(json.dumps({**identity,'sample':sample},ensure_ascii=False,allow_nan=False)+'\n')
        stream.flush()


def evidence_writer(path, identity):
    """只供下方隔离夹具安装，落盘真实请求白名单和工具结果；不接生产开关。"""
    path.touch(mode=0o600, exist_ok=False)
    sequence = 0
    def append(event):
        nonlocal sequence
        sequence += 1
        with path.open('a') as stream:
            stream.write(json.dumps({**identity, 'sequence': sequence, 'monotonic_seconds': time.monotonic(),
                                    **event}, ensure_ascii=False, allow_nan=False)+'\n')
    return append


async def execute_case(case, strategy, repetition, settings, suite, work, throttle, collect):
    from scripts.eval.run_context import Benchmark
    from scripts.eval.report_context import score
    from scripts.eval.harness.workflows import run_workflow
    configured=replace(settings,**suite['strategies'][strategy]['overrides'],data_dir=work)
    if case['layer']=='skill_replay':
        from scripts.eval.harness.cache_replay import run_skill_replay
        row=await run_skill_replay(case,configured,throttle,work,repetition,collect,suite['runtime'])
    elif case['layer']=='cache_replay':
        from scripts.eval.harness.cache_replay import run_cache_replay
        row=await run_cache_replay(case,configured,throttle,work,repetition,collect,suite['runtime'])
    elif case['layer']=='agent':
        row=await run_workflow(case,configured,throttle,work,repetition,collect,suite['runtime'])
    else:
        runner=Benchmark(configured,throttle,work,configured.context_pruning_timing,stream=False,
                         output_limit=suite['runtime']['output_limit'],usage_hook=collect,
                         request_timeout=suite['runtime']['request_timeout_seconds'])
        row=await runner.run_case(case,'layered' if configured.context_strategy=='layered' else 'legacy',repetition)
        row=score([row],{case['id']:case})[0]
        if case.get('expected_black_skus'):
            expected=case['expected_black_skus']
            found=set(re.findall(r'P\d+-S\d+',row['answer']))
            row['checks']['complete_black_sku_set']=found=={s['sku_id'] for s in expected}
            if case['template_id']=='dev-sku':
                from scripts.eval.harness.workflows import check_sku_quotes
                row['checks']['sku_price_currency_associations']=check_sku_quotes(row['answer'],expected)
            row['passed']=all(row['checks'].values()) and row['error'] is None
        row['layer']='context'
    row['strategy']=strategy
    row['critical']=case['critical']
    row['title']=case['title']
    # 原 Benchmark 的错误可能含网关 URL；新报告只存异常类型。
    if row.get('error'):row['error']=str(row['error']).split(':',1)[0]
    return row


async def run_profile(profile, output, *, suite_path=None, strategies=None, case_ids=None, skip_contracts=False):
    suite=load_suite(suite_path) if suite_path else load_suite()
    if profile not in suite['profiles']:raise ValueError('未知评测档位')
    selected=suite['profiles'][profile]
    names=strategies or ['current','candidate']
    if len(names)<2 or len(names)!=len(set(names)) or 'current' not in names or set(names)-set(suite['strategies']):
        raise ValueError('策略需包含 current 且不得重复/未知')
    ids=case_ids or selected['cases']
    if len(ids)!=len(set(ids)) or set(ids)-{c['id'] for c in suite['_cases']}:raise ValueError('场景未知或重复')
    # 人工选场景属于诊断，不能伪装为完整 smoke/release。
    actual_profile='diagnostic' if case_ids or skip_contracts else profile
    output=assert_output_path(output);output.mkdir(parents=True)
    (output/'rows').mkdir();(output/'attempts.jsonl').touch();(output/'contracts.log').touch()
    (output/'traces').mkdir()
    from app.infrastructure.settings import load_settings
    from app.infrastructure.throttle import GatewayThrottle
    # 离线契约不需要模型密钥；真实评测先保存报告，再明确拦截缺失配置。
    config=load_settings(require_llm_key=False);runtime=suite['runtime']
    settings=replace(config,llm_fallback_model='',llm_max_retries=runtime['model_retries'],
        semantic_cache_enabled=False,redis_url='',queue_enabled=False,tavily_api_key='',
        context_size=runtime['context_size'],reply_token_budget=0,token_budget_total=0,otlp_endpoint='',
        otlp_traces_endpoint='',otlp_headers='',otlp_traces_headers='',harness_enabled=True,
        llm_min_interval_seconds=runtime['min_interval_seconds'],llm_max_concurrency=1)
    frozen=source_manifest()
    # 工作区可能尚未提交，仅有 Git SHA 不足以复现；归档白名单源码，绝不打包 .env/数据库。
    with tarfile.open(output/'source.tar.gz','w:gz') as archive:
        for name in frozen['files']:archive.add(ROOT/name,arcname=name,recursive=False)
    manifest={'schema_version':SCHEMA_VERSION,'run_id':datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8],
      'status':'running','profile':actual_profile,'requested_profile':profile,'cases':ids,'strategies':names,
      'repetitions':selected['repetitions'],'model':settings.llm_model,'gateway_sha256':digest(settings.llm_base_url.encode()),
      'runtime':runtime,'execution_environment':execution_environment(settings),
      'temperature':0,'dataset_sha256':suite['_dataset_sha256'],'suite_sha256':suite['_suite_sha256'],
      'source':frozen,'source_stable':None,'policy_overrides':{n:suite['strategies'][n]['overrides'] for n in names},
      'source_archive_sha256':digest((output/'source.tar.gz').read_bytes()),
      'gates':suite['gates'],'pricing':suite['pricing'],'limits':suite['limits'],'semantic_answer_cache':False,
      'synthetic':True,'contracts':None,'dataset_provenance':suite['dataset_provenance'],
      'scorer_version':suite.get('scorer_version','sku-quotes-v1.1'),
      'synthetic_evidence': {'enabled': True, 'directory': 'traces', 'format': 'jsonl',
                             'request_fields': 'model/messages/tools/tool_choice/response_format/temperature/max_tokens/max_completion_tokens/stream',
                             'scope': '仅临时数据库和合成买家；不包含鉴权、headers、客户端配置'}}
    rows=[]
    def checkpoint():
        write_json(output/'manifest.json',manifest)
        return render(output,manifest,rows)
    # None 与未执行都不是通过。
    manifest['contracts']={}
    write_json(output/'cases.json',[c for c in suite['_cases'] if c['id'] in ids])
    write_json(output/'suite.json',{k:v for k,v in suite.items() if not k.startswith('_')})
    checkpoint()
    try:
        if not skip_contracts:
            manifest['contracts']=await contract_checks(suite,output)
            checkpoint()
            if not manifest['contracts']['passed']:
                manifest['status']='failed';manifest['error']='contract_checks_failed'
                return checkpoint()
        if ids and (not settings.llm_api_key or not settings.llm_base_url):
            manifest['status']='failed';manifest['error']='model_credentials_missing'
            manifest['missing_configuration'] = [name for name, value in (
                ('LLM_API_KEY', settings.llm_api_key), ('LLM_BASE_URL', settings.llm_base_url)) if not value]
            print('未执行真实模型评测，缺少配置：'+', '.join(manifest['missing_configuration'])+
                  '。GitHub Actions 请配置对应的 HARNESS_LLM_API_KEY / HARNESS_LLM_BASE_URL secrets。', flush=True)
            return checkpoint()
        throttle=GatewayThrottle(1,runtime['min_interval_seconds'])
        cases={c['id']:c for c in suite['_cases']}
        # 场景次序固定种子打散；同场景的策略交错，重复时轮换顺序。
        order=list(ids);random.Random(runtime['seed']).shuffle(order)
        for repetition in range(selected['repetitions']):
            for case_id in order:
                rotated=names[repetition%len(names):]+names[:repetition%len(names)]
                for strategy in rotated:
                    identity={'case_id':case_id,'strategy':strategy,'repetition':repetition}
                    write_json(output/'active.json',identity)
                    samples=[]
                    def collect(sample):
                        samples.append(sample)
                        append_attempt(output/'attempts.jsonl',identity,sample)
                    start=time.monotonic()
                    from app.infrastructure.context_usage import evaluation_evidence_sink
                    trace_name = f'{case_id}-{strategy}-{repetition}.jsonl'
                    evidence_token = evaluation_evidence_sink.set(evidence_writer(output/'traces'/trace_name, identity))
                    with tempfile.TemporaryDirectory(prefix='shoppilot-harness-') as temporary:
                        try:
                            row=await asyncio.wait_for(execute_case(cases[case_id],strategy,repetition,settings,suite,
                                Path(temporary),throttle,collect),runtime['scenario_timeout_seconds'])
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            row={**identity,'layer':cases[case_id]['layer'],'mode':cases[case_id]['mode'],
                                 'critical':cases[case_id]['critical'],'checks':{'execution_completed':False},
                                 'passed':False,'error':type(error).__name__,'usage':samples,
                                 'round_metrics':[],'elapsed_ms':(time.monotonic()-start)*1000}
                        finally:
                            evaluation_evidence_sink.reset(evidence_token)
                    row['evidence_trace'] = 'traces/'+trace_name
                    rows.append(row)
                    write_json(output/'rows'/f'{case_id}-{strategy}-{repetition}.json',row)
                    checkpoint()
                    print(json.dumps({**identity,'passed':row['passed'],'error':row.get('error')},ensure_ascii=False),flush=True)
        manifest['status']='completed'
    except (asyncio.CancelledError,KeyboardInterrupt):
        manifest['status']='cancelled'
        raise
    except Exception as error:
        manifest['status']='failed';manifest['error']=type(error).__name__
        raise
    finally:
        manifest['source_stable']=source_manifest()['sha256']==frozen['sha256']
        manifest['finished_at']=datetime.now(timezone.utc).isoformat()
        checkpoint()
    return checkpoint()
