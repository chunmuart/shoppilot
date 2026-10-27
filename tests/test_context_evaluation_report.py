"""验收统计不能把未知成本记零，也不能把重复运行当独立场景。"""
from scripts.eval.report_context import score, summarize, paired_interval


def row(strategy='layered',passed=True,case='c',tokens=100):
    return dict(strategy=strategy,passed=passed,case_id=case,input_tokens=tokens,mode='long',
                model_calls=2,lookup_calls=1,elapsed_ms=200,usage=[],round_metrics=[{'elapsed_ms':50,'compacted':False},{'elapsed_ms':150,'compacted':True}])


def test_unknown_usage_is_not_zero_and_summary_rounds_not_ordinary_latency():
    result=summarize([row(),row(tokens=None)])['layered']
    assert result['input_tokens'] is None and result['long_input_tokens'] is None
    assert result['unknown_usage_runs']==1 and result['ordinary_round_p95_ms']==50


def test_interval_pairs_at_scenario_not_repeat_level():
    values=[row('legacy',True,'a'),row('layered',False,'a')]
    values += [row('legacy',False,'b'),row('layered',True,'b')]*10
    result=paired_interval(values,'legacy','layered')
    assert result['difference']==0 and result['unit']=='scenario'
    assert result['ci95']==[-1,1]


def test_currency_alias_scoring_keeps_original_evidence():
    record={'case_id':'c','answer':'预算180元人民币','current_calls':0,'mode':'snapshot','checks':{'facts':False},'error':None}
    cases={'c':{'contains':['180','CNY'],'requires_current':False}}
    result=score([record],cases)[0]
    assert result['passed'] and result['original_checks']=={'facts':False}
    assert result['answer']=='预算180元人民币'


def test_long_cost_interval_uses_paired_scenarios_and_unknown_blocks():
    from scripts.eval.report_context import paired_cost_interval
    rows=[row('legacy',tokens=100),row('layered',tokens=75)]*3
    result=paired_cost_interval(rows,'legacy','layered')
    assert result['scenarios']==1 and result['reduction']==.25 and result['ci95']==[.25,.25]
    rows.append(row('layered',tokens=None))
    assert paired_cost_interval(rows,'legacy','layered') is None


async def test_benchmark_old_summary_runs_despite_layered_automatic_threshold(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from agentscope.state import AgentState
    from app.infrastructure.context_governance import LayeredContextMiddleware
    from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
    from app.application.agents.context_policy import build_context_config
    from scripts.eval.run_context import compact_for_evaluation
    agent=SimpleNamespace(state=AgentState(),context_config=build_context_config(128000,20000))
    async def old_summary(**kwargs):
        assert kwargs['context_config'].trigger_ratio<.001
        agent.state.summary=(agent.state.summary or '')+'一次有效摘要'
    agent._compress_context_impl=AsyncMock(side_effect=old_summary)
    middleware=LayeredContextMiddleware(ContextEvidenceStore(tmp_path/'e.db'),summary=False)
    assert (await compact_for_evaluation(agent,middleware))['summary_changed']
    assert (await compact_for_evaluation(agent,middleware))['summary_changed']
    assert agent._compress_context_impl.await_count==2


def test_absent_preference_synonym_is_not_a_false_failure():
    record={'case_id':'c','answer':'当前无长期排除塑料的偏好，该约束已取消。','current_calls':1,'mode':'snapshot','checks':{'facts':False},'error':None}
    assert score([record],{'c':{'contains':['不'],'requires_current':True}})[0]['passed']


def test_expensive_reason_and_cancelled_status_explicit_aliases():
    for value,answer in [('太贵','原因是价格超出预算。'),('CANCELLED','订单当前状态：已取消。')]:
        r={'case_id':'c','answer':answer,'current_calls':1,'mode':'snapshot','checks':{'facts':False},'error':None}
        assert score([r],{'c':{'contains':[value],'requires_current':True}})[0]['passed']


def test_historical_current_prices_cannot_be_swapped():
    from scripts.eval.report_context import association_check
    assert association_check('hold-old-price','第一批历史单价139.0 CNY，当前单价129.0 CNY')['status']=='failed'
    assert association_check('hold-old-price','第一批历史单价129.0 CNY，当前单价139.0 CNY')['status']=='passed'


def test_sku_stock_and_sku_price_pairs_require_correct_assignment():
    from scripts.eval.report_context import association_check
    assert association_check('hold-sku-stock','石墨黑（P1003-S1）历史库存60，雾霾蓝（P1003-S2）历史库存80')['status']=='failed'
    assert association_check('hold-sku-stock','石墨黑（P1003-S1）历史库存80，雾霾蓝（P1003-S2）历史库存60')['status']=='passed'
    assert association_check('hold-pair','P1001-S2 单价129，P1003-S1 单价199')['status']=='failed'
    assert association_check('hold-pair','P1001-S2 单价199，P1003-S1 单价129')['status']=='passed'


def test_association_parser_keeps_titles_and_multiline_sku_attributes():
    from scripts.eval.report_context import association_check
    answer='P1001-S2（旅行三件套，沙漠黄）历史单价为199.0 CNY\nP1003-S1（背包，石墨黑）历史单价为129.0 CNY'
    assert association_check('hold-pair',answer)['status']=='passed'
    assert association_check('hold-blue','SKU: P1003-S2\n规格：雾霾蓝\n历史库存：60件')['status']=='passed'
    assert association_check('hold-blue','SKU P1003-S2，当前库存为60件')['status']=='failed'


def test_fee_equation_postfix_labels_do_not_create_false_swaps():
    from scripts.eval.report_context import association_check
    answer='运费25元，税费0元。计算：129（商品） + 25（运费） + 0（税费）=154元'
    assert association_check('hold-tax',answer)['status']=='passed'
    assert association_check('hold-tax','运费0元，税费25元')['status']=='failed'


def test_run_grid_rejects_missing_and_duplicate_results():
    import pytest
    from scripts.eval.report_context import validate_run_grid
    manifest={'cases':['c'],'strategies':['a','b'],'repetitions':1,'completed':2}
    one={'case_id':'c','strategy':'a','repetition':0}
    with pytest.raises(ValueError):validate_run_grid([one,one],manifest)
    with pytest.raises(ValueError):validate_run_grid([one],manifest)
    validate_run_grid([one,{**one,'strategy':'b'}],manifest)


def test_removed_requirement_literal_applies_equally_to_all_strategies():
    for strategy in ['legacy','deterministic','layered']:
        for answer,expected in [('当前偏好中已无“长期排除塑料”的要求。',True),('当前偏好中仍有“长期排除塑料”的要求。',False)]:
            record={'case_id':'c','strategy':strategy,'answer':answer,'current_calls':1,'mode':'snapshot','checks':{'facts':False},'error':None}
            result=score([record],{'c':{'contains':['不'],'requires_current':True}})[0]
            assert result['passed'] is expected
            assert result['answer']==answer
