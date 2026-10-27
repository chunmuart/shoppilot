"""评测器自身也要验收：缺失成本、伪通过、场景配对与 HTML 注入。"""
import copy
import json
from pathlib import Path

import pytest

from scripts.eval.harness.contracts import load_suite, affected_paths, assert_output_path
from scripts.eval.harness.metrics import total, paired, summarize
from scripts.eval.harness.report import render
from scripts.eval.harness.workflows import check_facts


def manifest(profile='release'):
    return {'schema_version':'harness-eval-v1','run_id':'test-only','profile':profile,'status':'completed',
        'cases':['a','b'],'strategies':['current','candidate'],'repetitions':3,'model':'fake-for-unit-test',
        'source':{'sha256':'test'},'source_stable':True,'dataset_sha256':'test','pricing':{'currency':None},
        'contracts':{'passed':True,'passed_count':2},'limits':['合成单测，不是模型评测证据'],
        'policy_overrides':{'current':{},'candidate':{}},
        'gates':{'actual_input_reduction':.25,'ordinary_p95_max_ratio':1.15,'success_rate_min_difference':0}}


def rows():
    return [{'case_id':case,'strategy':strategy,'repetition':repeat,'layer':'context','mode':'snapshot',
             'passed':True,'critical':True,'error':None,'checks':{'facts':True},'elapsed_ms':100,
             'usage':[{'kind':'business','input_tokens':100 if strategy=='current' else 50,'output_tokens':10,'elapsed_ms':50,'ttft_ms':None,
                       'prompt_cache':{'model':'fake-for-unit-test','response_model':'fake-for-unit-test','response_model_matches':True,'protocol_status':'valid'}}],
             'round_metrics':[{'elapsed_ms':100,'compacted':False}],'answer':'测试正文'}
            for case in ('a','b') for strategy in ('current','candidate') for repeat in range(3)]


def test_frozen_dataset_has_separate_dev_holdout_and_workflows():
    suite=load_suite();cases=suite['_cases'];by_id={c['id']:c for c in cases}
    assert len(cases)==48 and sum(c['layer']=='agent' for c in cases)==8
    assert all(by_id[i]['split']=='dev' for i in suite['profiles']['smoke']['cases'])
    assert not set(suite['profiles']['dev']['cases']) & set(suite['profiles']['release']['cases'])
    assert suite['strategies']['current']['overrides']['prompt_cache_mode']=='passthrough'


def test_synthetic_trace_is_durable_private_and_report_link_is_local(tmp_path):
    from scripts.eval.harness.runner import evidence_writer
    from app.infrastructure.context_usage import evaluation_evidence_sink, record_evaluation_evidence
    file = tmp_path/'events.jsonl'
    writer = evidence_writer(file, {'case_id': 'a', 'strategy': 'current', 'repetition': 0})
    record_evaluation_evidence('tool_result', {'result': '默认不写'})
    assert file.read_text() == ''
    token = evaluation_evidence_sink.set(writer)
    try:
        record_evaluation_evidence('tool_result', {'arguments': {'offset': 5}, 'state': 'error', 'result': '合成错误'})
        record_evaluation_evidence('model_request', {'messages': []})
    finally:
        evaluation_evidence_sink.reset(token)
    record_evaluation_evidence('tool_result', {'result': '退出后不写'})
    events = [json.loads(line) for line in file.read_text().splitlines()]
    assert [event['sequence'] for event in events] == [1, 2]
    assert events[0]['payload']['state'] == 'error'
    assert file.stat().st_mode & 0o777 == 0o600
    samples = rows();samples[0]['evidence_trace'] = 'traces/a-current-0.jsonl'
    samples[1]['evidence_trace'] = 'https://invalid.example/unsafe'
    render(tmp_path, manifest(), samples)
    html = (tmp_path/'report.html').read_text()
    assert 'href="traces/a-current-0.jsonl"' in html
    assert 'https://invalid.example/unsafe' not in html


def test_impact_recognizes_parent_repository_and_ignores_tutorials():
    assert affected_paths(['项目/项目工程/shoppilot/app/infrastructure/llm.py','项目教程/05.md'])==['app/infrastructure/llm.py']
    assert affected_paths(['app/application/harness/loop_detector.py','README.md'])==['app/application/harness/loop_detector.py']


def test_unknown_and_empty_usage_never_becomes_zero():
    assert total([{'usage':[]}],'input_tokens')=={'total':None,'observed_sum':None,'known':0,'unknown':1}
    r=rows();r[0]['usage'][0]['input_tokens']=None
    report=summarize(manifest(),r)
    assert report['strategies']['current']['input_tokens']['total'] is None
    assert report['gates']['candidate']['status']=='BLOCKED'
    assert report['paired_intervals']['candidate']['input_reduction']['scenarios']==1


def test_interrupted_grid_cannot_show_completed_subset_as_full_total():
    report=summarize(manifest(),rows()[:-1])
    candidate=report['strategies']['candidate']
    assert candidate['input_tokens']['total'] is None
    assert candidate['input_tokens']['observed_sum']==250
    assert candidate['coverage_complete'] is False


def test_repeat_is_not_an_independent_statistical_unit():
    result=paired(rows(),'current','candidate',3,'input_tokens')
    assert result=={'unit':'scenario','scenarios':2,'estimate':.5,'ci95':[.5,.5]}
    assert paired(rows()[:-1],'current','candidate',3)['scenarios']==1


def test_one_scenario_cannot_claim_a_zero_width_confidence_interval():
    one=[r for r in rows() if r['case_id']=='a']
    result=paired(one,'current','candidate',3,'input_tokens')
    assert result['estimate']==.5 and result['ci95'] is None


def test_manual_compaction_latency_and_usage_are_not_lost_between_rounds():
    r=rows()
    r[0]['compaction_metrics']=[{'elapsed_ms':321,'trigger':'manual'}]
    r[0]['usage'].append({'kind':'summary','input_tokens':30,'output_tokens':20,'elapsed_ms':300})
    m=summarize(manifest(),r)['strategies']['current']
    assert m['manual_compaction_p95_ms']==321 and m['ordinary_round_p95_ms']==100
    assert m['usage_by_kind']['summary']['input_tokens']['total']==30
    assert m['input_tokens']['total']==630


@pytest.mark.parametrize('mutation',['missing','duplicate','critical','source','not_release','contract_skip','cp'])
def test_invalid_experiment_cannot_claim_benefit(mutation):
    r=rows();m=manifest()
    if mutation=='missing':r.pop()
    elif mutation=='duplicate':r[-1]=copy.deepcopy(r[0])
    elif mutation=='critical':r[-1]['passed']=False;r[-1]['checks']['facts']=False
    elif mutation=='source':m['source_stable']=False
    elif mutation=='not_release':m['profile']='smoke'
    elif mutation=='contract_skip':m['contracts']={}
    elif mutation=='cp':m['policy_overrides']['candidate']['prompt_cache_mode']='explicit'
    assert summarize(m,r)['gates']['candidate']['status']=='BLOCKED'


def test_valid_input_efficiency_is_separate_from_billing_and_deployment():
    gate=summarize(manifest(),rows())['gates']['candidate']
    assert gate['status']=='BENEFIT_VERIFIED'
    assert gate['billing_reduction'] is None and gate['production_deploy_authorized'] is False

@pytest.mark.parametrize('mutation',['unknown','mismatch','conflict','violation'])
def test_provider_identity_and_contract_are_release_prerequisites(mutation):
    data=rows();cache=data[0]['usage'][0]['prompt_cache']
    if mutation=='unknown':cache.pop('response_model_matches')
    elif mutation=='mismatch':cache.update(response_model='other',response_model_matches=False)
    elif mutation=='conflict':cache['response_model_conflict']=True
    else:cache['protocol_status']='tools_forbidden'
    assert summarize(manifest(),data)['gates']['candidate']['status']=='BLOCKED'


def test_slow_candidate_cannot_pass_due_to_token_savings():
    r=rows()
    for row in r:
        if row['strategy']=='candidate':row['round_metrics'][0]['elapsed_ms']=150
    assert summarize(manifest(),r)['gates']['candidate']['status']=='BLOCKED'


def test_more_output_cannot_hide_behind_less_input():
    r=rows()
    for row in r:
        if row['strategy']=='candidate':row['usage'][0]['output_tokens']=1000
    assert summarize(manifest(),r)['gates']['candidate']['status']=='BLOCKED'


def test_html_escapes_model_text_and_needs_no_network(tmp_path):
    m=manifest('smoke');r=rows()
    r[0]['answer']='</pre><script>alert("x")</script><img src=x onerror=alert(1)>'
    render(tmp_path,m,r)
    html=(tmp_path/'report.html').read_text()
    assert '<script>alert(' not in html and '<img src=x' not in html
    assert '&lt;script&gt;' in html
    assert 'src="http' not in html and '未知' in html and '未满足上线门禁' in html
    assert (tmp_path/'results.json').exists() and (tmp_path/'summary.json').exists()


def test_field_association_cannot_match_budget_digits_as_stock():
    assert check_facts('{"sku_id":"P1003-S1","unit_price_major":129,"stock":80,"budget_major":180,"currency":"CNY"}')
    assert not check_facts('{"sku_id":"P1003-S1","unit_price_major":129,"stock":8,"budget_major":180,"currency":"CNY"}')
    assert not check_facts('单价1299，库存8，预算180')


def test_all_black_skus_include_non_default_black_aliases_and_quotes_are_paired():
    from scripts.eval.harness.workflows import check_sku_quotes
    case=next(c for c in load_suite()['_cases'] if c['id']=='h1-dev-sku')
    assert len(case['expected_black_skus'])==3
    assert any(s['currency']=='USD' for s in case['expected_black_skus'])
    expected=[{'sku_id':'P1001-S1','price_major':129,'currency':'CNY'},
              {'sku_id':'P1002-S1','price_major':219,'currency':'USD'}]
    assert check_sku_quotes('P1001-S1 129 CNY; P1002-S1 219 USD',expected)
    assert not check_sku_quotes('P1001-S1 219 USD; P1002-S1 129 CNY',expected)


def test_existing_output_and_live_data_directory_are_protected(tmp_path):
    from scripts.eval.harness.contracts import ROOT
    with pytest.raises(ValueError):assert_output_path(tmp_path)
    with pytest.raises(ValueError):assert_output_path(ROOT/'data'/'harness-evaluation')


@pytest.mark.asyncio
async def test_runner_keeps_failed_case_and_generates_html(tmp_path,monkeypatch):
    from scripts.eval.harness import runner
    async def failure(*args):raise TimeoutError('must not be copied: credential-secret')
    monkeypatch.setattr(runner,'execute_case',failure)
    from app.infrastructure.settings import load_settings
    from dataclasses import replace
    monkeypatch.setattr('app.infrastructure.settings.load_settings',lambda **kwargs:replace(load_settings(require_llm_key=False),llm_api_key='test-key',llm_base_url='http://localhost.invalid'))
    out=tmp_path/'evaluation'
    await runner.run_profile('smoke',out,case_ids=['agent-sku'],skip_contracts=True)
    saved=json.loads((out/'results.json').read_text())
    assert len(saved)==2 and all(r['error']=='TimeoutError' and not r['passed'] for r in saved)
    assert 'credential-secret' not in (out/'report.html').read_text()
    assert json.loads((out/'manifest.json').read_text())['profile']=='diagnostic'


@pytest.mark.asyncio
async def test_cancelled_runner_still_has_partial_report_and_attempt_ledger(tmp_path,monkeypatch):
    import asyncio
    from scripts.eval.harness import runner
    async def cancel(*args):
        args[-1]({'kind':'business','input_tokens':None,'output_tokens':None,'elapsed_ms':1})
        raise asyncio.CancelledError()
    monkeypatch.setattr(runner,'execute_case',cancel)
    from app.infrastructure.settings import load_settings
    from dataclasses import replace
    monkeypatch.setattr('app.infrastructure.settings.load_settings',lambda **kwargs:replace(load_settings(require_llm_key=False),llm_api_key='test-key',llm_base_url='http://localhost.invalid'))
    out=tmp_path/'cancelled'
    with pytest.raises(asyncio.CancelledError):
        await runner.run_profile('smoke',out,case_ids=['agent-sku'],skip_contracts=True)
    assert json.loads((out/'manifest.json').read_text())['status']=='cancelled'
    assert (out/'attempts.jsonl').read_text()
    assert '已中断' in (out/'report.html').read_text()


def test_cross_version_comparison_rejects_model_change(tmp_path):
    from scripts.eval.harness.__main__ import compare_runs
    for name,model in (('a','one'),('b','two')):
        path=tmp_path/name;path.mkdir();(path/'manifest.json').write_text(json.dumps({'model':model}))
    with pytest.raises(ValueError,match='model'):compare_runs(tmp_path/'a',tmp_path/'b',tmp_path/'out')


def test_cross_version_comparison_keeps_distinct_private_trace_files(tmp_path):
    from scripts.eval.harness.__main__ import compare_runs
    for name in ('a', 'b'):
        path = tmp_path/name
        (path/'traces').mkdir(parents=True)
        (path/'manifest.json').write_text(json.dumps(manifest()))
        samples = rows()
        samples[0]['evidence_trace'] = 'traces/same-name.jsonl'
        (path/'traces/same-name.jsonl').write_text(json.dumps({'source':name})+'\n')
        (path/'results.json').write_text(json.dumps(samples))
    out = tmp_path/'out'
    compare_runs(tmp_path/'a', tmp_path/'b', out)
    compared = json.loads((out/'results.json').read_text())
    linked = [row for row in compared if row.get('evidence_trace')]
    assert len(linked) == 2
    for row, original in zip(linked, ('a', 'b')):
        file = out/row['evidence_trace']
        assert json.loads(file.read_text()) == {'source':original}
        assert file.stat().st_mode & 0o777 == 0o600
        assert 'href="'+row['evidence_trace']+'"' in (out/'report.html').read_text()


@pytest.mark.parametrize('reference', ['../outside.jsonl', 'traces/missing.jsonl', 'traces/escape.jsonl'])
def test_cross_version_comparison_rejects_missing_or_external_traces(tmp_path, reference):
    from scripts.eval.harness.__main__ import compare_runs
    outside = tmp_path/'outside.jsonl'
    outside.write_text('不应复制的文件')
    for name in ('a', 'b'):
        path = tmp_path/name
        (path/'traces').mkdir(parents=True)
        (path/'traces/escape.jsonl').symlink_to(outside)
        (path/'manifest.json').write_text(json.dumps(manifest()))
        samples = rows()
        samples[0]['evidence_trace'] = reference
        (path/'results.json').write_text(json.dumps(samples))
    with pytest.raises(ValueError, match='证据'):
        compare_runs(tmp_path/'a', tmp_path/'b', tmp_path/'out')


def test_execution_environment_is_reproducible_without_credentials():
    from dataclasses import replace
    from app.infrastructure.settings import load_settings
    from scripts.eval.harness.contracts import execution_environment
    configured=replace(load_settings(require_llm_key=False),llm_api_key='do-not-export',identity_hmac_secret='do-not-export',loop_repeat_threshold=7)
    metadata=execution_environment(configured)
    assert 'do-not-export' not in json.dumps(metadata)
    assert metadata['settings']['loop_repeat_threshold']==7
    assert metadata['packages']['agentscope']


def test_application_still_requires_key_but_offline_configuration_does_not(tmp_path, monkeypatch):
    from app.infrastructure.settings import load_settings
    monkeypatch.setenv('LLM_API_KEY', '')
    monkeypatch.setenv('DATA_DIR', str(tmp_path/'data'))
    with pytest.raises(RuntimeError, match='LLM_API_KEY'):
        load_settings()
    assert load_settings(require_llm_key=False).llm_api_key == ''


@pytest.mark.parametrize('profile', ['contracts', 'smoke'])
async def test_missing_credentials_allow_contracts_but_block_model_with_report(tmp_path, monkeypatch, profile, capsys):
    from unittest.mock import AsyncMock
    from scripts.eval.harness import runner
    monkeypatch.setenv('LLM_API_KEY', '')
    monkeypatch.setenv('LLM_BASE_URL', '')
    monkeypatch.setenv('DATA_DIR', str(tmp_path/'data'))
    contracts = AsyncMock(return_value={'passed':True, 'passed_count':1})
    model = AsyncMock(side_effect=AssertionError('缺少密钥不能请求模型'))
    monkeypatch.setattr(runner, 'contract_checks', contracts)
    monkeypatch.setattr(runner, 'execute_case', model)
    out = tmp_path/profile
    await runner.run_profile(profile, out)
    saved = json.loads((out/'manifest.json').read_text())
    contracts.assert_awaited_once()
    model.assert_not_awaited()
    assert saved['source_stable'] is True
    assert (out/'report.html').is_file()
    assert json.loads((out/'results.json').read_text()) == []
    if profile == 'contracts':
        assert saved['status'] == 'completed'
        assert 'error' not in saved
    else:
        assert saved['status'] == 'failed'
        assert saved['error'] == 'model_credentials_missing'
        assert saved['missing_configuration'] == ['LLM_API_KEY', 'LLM_BASE_URL']
        assert '缺少配置' in (out/'report.html').read_text()
        assert 'HARNESS_LLM_API_KEY' in capsys.readouterr().out


def test_cross_version_comparison_rejects_hidden_runtime_change(tmp_path):
    from scripts.eval.harness.__main__ import compare_runs
    for name,threshold in (('a',3),('b',7)):
        path=tmp_path/name;path.mkdir()
        (path/'manifest.json').write_text(json.dumps({'execution_environment':{'settings':{'loop_repeat_threshold':threshold}}}))
    with pytest.raises(ValueError,match='execution_environment'):
        compare_runs(tmp_path/'a',tmp_path/'b',tmp_path/'out')


@pytest.mark.parametrize('suffix,expected_result',[
    ('；当前选中 P1001-S1。', True),
    ('；比较对象 P1002-S1 和 P1001-S1。', True),
    ('；P1001-S1 最新报价 219 USD。', False),
    ('；P1001-S1 最新报价 219 CNY。', False),
    ('；P1001-S1 原报价币种 CNY。', True),
    ('；P1002-S1 原报价币种为 USD（商品级字段虽标 CNY，SKU 级币种以 USD 为准）。', True),
    ('；P1002-S1 原报价币种为 USD（商品级币种为 CNY，SKU 级币种以 USD 为准）。', True),
    ('；P1002-S1 原报价币种为 CNY（商品级字段虽标 USD）。', False),
    ('；P1002-S1 单价219 USD（商品级价格1500 CNY）。', True),
    ('；P1002-S1 单价1500 CNY（商品级价格219 USD）。', False),
    ('；P1001-S1 原报价币种 USD。', False),
    ('；P1001-S1 报价219 CNY，预算129。', False),
    ('；P1001-S1 单价219。', False),
    ('\n\n说明：商品级展示为 CNY，SKU 原报价以 USD 为准。', True),
])
def test_quote_references_do_not_require_repeating_price_but_conflicts_fail(suffix,expected_result):
    from scripts.eval.harness.workflows import check_sku_quotes
    golden=[{'sku_id':'P1001-S1','price_major':129,'currency':'CNY'},
            {'sku_id':'P1002-S1','price_major':219,'currency':'USD'}]
    answer='P1001-S1 129 CNY; P1002-S1 219 USD'+suffix
    assert check_sku_quotes(answer,golden) is expected_result
    assert not check_sku_quotes('只比较 P1001-S1 和 P1002-S1',golden)
    assert not check_sku_quotes('P1002-S1 219 USD。P1001-S1 已排除，省略报价。',golden)
