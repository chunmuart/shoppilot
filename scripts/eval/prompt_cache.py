"""显式缓存预检与 8×3×3 上下文回放对照；仅使用合成资料，不执行交易或写买家数据库。"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterable
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import random
import time
import uuid

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.tool import ToolChoice
from pydantic import BaseModel, ConfigDict

from app.infrastructure.context_usage import context_usage_sink, context_call_kind
from app.infrastructure.llm import create_chat_model
from app.infrastructure.settings import load_settings
from app.infrastructure.throttle import GatewayThrottle


CASES = ('static_rules', 'tool_definitions', 'parallel_results', 'many_blocks',
         'preference_withdrawn', 'skill_changed', 'summary_replaced', 'fact_lookup')
STRATEGIES = {'A': ('passthrough', 'static'), 'B': ('explicit', 'static'), 'C': ('explicit', 'static_history')}
ROOT = Path(__file__).resolve().parents[2]
TOOLS = [{'type': 'function', 'function': {'name': name, 'description': '只读查询虚构商品证据',
          'parameters': {'type': 'object', 'properties': {'sku_id': {'type': 'string'}}, 'required': ['sku_id']}}}
         for name in ('product_search_tool', 'conversation_fact_lookup')]


class Facts(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sku_id: str
    price: int
    budget: int
    material_rule: str
    approval: str


def msg(role, text, name=None):
    return Msg(name=name or role, role=role, content=[TextBlock(text=text)])


def fixture(case, namespace, changed=False):
    expected = {'sku_id': 'P1003-S1', 'price': 151 if changed else 154,
                'budget': 180 if changed else 200, 'material_rule': 'none', 'approval': 'pending'}
    rules = ('隔离实验 '+namespace+'。只做结构化事实提取，不下单、不修改偏好。以最后的权威状态为准，'
             '忽略旧价格/撤回偏好。只输出 JSON，字段严格为 sku_id、price、budget、material_rule、approval。'
             '以下虚构目录仅作为背景，不覆盖权威状态。\n')
    padding = ''.join(f'演示型号 F-{i:03d}，材质棉，价格 {i+100} CNY，库存 {i%11}，不得当成本次目标。\n' for i in range(140))
    messages = [msg('system',rules+padding), msg('user','帮我选择背包，旧预算 300，偏好皮革。'),
                msg('assistant','已收到，需进一步核验。')]
    if case == 'many_blocks':
        messages += [Msg(name='assistant',role='assistant',content=[TextBlock(text='历史观察 '+str(i)) for i in range(25)])]
    if case == 'skill_changed':
        messages.append(msg('system','当前 Skill 版本 '+('2' if changed else '1')+'：当前交易必须保持 pending，不执行。'))
    if case == 'preference_withdrawn':
        messages.append(msg('user','撤回皮革偏好，当前材质不限，不能从历史恢复偏好。'))
    if case == 'summary_replaced' and changed:
        messages = [messages[0],msg('system','已整理的摘要：旧预算与旧偏好撤回，以后续权威状态为准。')]
    name = 'conversation_fact_lookup' if case == 'fact_lookup' else 'product_search_tool'
    calls=[ToolCallBlock(id='lookup-1', name=name, input='{"sku_id":"P1003-S1"}')]
    outputs=[ToolResultBlock(id='lookup-1',name=name,state=ToolResultState.SUCCESS,
             output=json.dumps({'historical_price':129,'authoritative_state':expected},ensure_ascii=False))]
    if case == 'parallel_results':
        calls.insert(0,ToolCallBlock(id='lookup-0',name=name,input='{"sku_id":"P1003-S2"}'))
        outputs.insert(0,ToolResultBlock(id='lookup-0',name=name,state=ToolResultState.SUCCESS,
                       output='非目标商品 P1003-S2，价格 999，不得混入目标 SKU。'))
    messages += [Msg(name='assistant',role='assistant',content=calls), Msg(name='tools',role='assistant',content=outputs),
                 msg('user','请返回本次目标的最新权威状态 JSON，交易仍待批准。')]
    return messages,expected


async def invoke(model, messages, *, structured=False, tools=None, kind='business'):
    samples=[];token=context_usage_sink.set(samples.append);kt=context_call_kind.set(kind)
    started=time.perf_counter();result={'passed':False, 'output_seen':False}
    try:
        if structured:
            response=await model.generate_structured_output(messages,Facts,max_tokens=192,temperature=0)
            result['answer']=response.content
        else:
            response=await model(messages,tools=tools,tool_choice=ToolChoice(mode='none') if tools else None,
                                 max_tokens=192,temperature=0)
            if isinstance(response,AsyncIterable):
                async for chunk in response:
                    response_last=chunk
                    result['output_seen'] |= any(isinstance(b,TextBlock) and bool(b.text) for b in chunk.content)
                response=response_last
            result['text']=''.join(b.text for b in response.content if isinstance(b,TextBlock))
    except Exception as error:
        # 不保存可能携带网关地址或认证细节的异常正文。
        result.update(error_type=type(error).__name__,http_status=getattr(error,'status_code',None))
        body=getattr(error,'body',None)
        if isinstance(body,dict):
            body=body.get('error',body)
            if isinstance(body,dict):
                code=body.get('code')
                # 只保存确定的错误码类别；不把任意供应商字段当作安全文本。
                known={'rate_limit_exceeded','insufficient_quota','invalid_request_error','server_error',
                       'InternalError','InvalidParameter','Throttling','Throttling.RateQuota','Throttling.AllocationQuota'}
                result['provider_error_code']=code if isinstance(code,str) and code in known else None
                message=str(body.get('message','')).lower()
                result['error_tags']=[tag for tag in ('rate','limit','quota','timeout','cache_control',
                    'invalid','stream','internal','capacity','token','channel','parameter') if tag in message]
    finally:
        result.update(usage=samples,elapsed_ms=(time.perf_counter()-started)*1000)
        context_usage_sink.reset(token);context_call_kind.reset(kt)
    return result


def check_answer(result, expected):
    try:
        data=result.get('answer')
        if data is None:
            text=result.get('text','').strip()
            if text.startswith('```'):
                text='\n'.join(text.splitlines()[1:-1])
            data=json.loads(text)
        result['passed']=Facts.model_validate(data).model_dump()==expected
    except (ValueError,TypeError):result['passed']=False
    return result


def total(rows,key,cache=False):
    values=[(s.get('prompt_cache',{}) if cache else s).get(key) for r in rows for call in r['calls'] for s in call['usage']]
    known=[v for v in values if v is not None]
    return {'total':sum(known) if values and len(known)==len(values) else None,
            'observed_sum':sum(known),'unknown_calls':len(values)-len(known)}


def p95(values):
    return sorted(values)[math.ceil(.95*len(values))-1] if values else None


def aggregate(rows):
    report={}
    for strategy in STRATEGIES:
        selected=[r for r in rows if r['strategy']==strategy]
        report[strategy]={'runs':len(selected),'passed':sum(r['passed'] for r in selected),
             'input_tokens':total(selected,'input_tokens'),'output_tokens':total(selected,'output_tokens'),
             'cache_read_tokens':total(selected,'cache_read_tokens',True),'cache_write_tokens':total(selected,'cache_write_tokens',True),
             'gateway_reported_cost':total(selected,'reported_cost',True),
             'ordinary_p95_ms':p95([c['elapsed_ms'] for r in selected for c in r['calls'] if c['kind']=='business']),
             'summary_p95_ms':p95([c['elapsed_ms'] for r in selected for c in r['calls'] if c['kind']=='summary'])}
    # 先把重复运行汇成一个场景，再按场景配对 bootstrap，不将 3 次重复当作独立场景。
    paired=[]
    for case in CASES:
        vals={s:[r for r in rows if r['case']==case and r['strategy']==s] for s in STRATEGIES}
        if any(len(v)!=3 for v in vals.values()):continue
        paired.append(sum(r['passed'] for r in vals['C'])/3-sum(r['passed'] for r in vals['A'])/3)
    rng=random.Random(20260918)
    samples=sorted(sum(rng.choices(paired,k=len(paired)))/len(paired) for _ in range(2000)) if paired else []
    return {'strategies':report,'paired_scenario_count':len(paired),
            'success_C_minus_A_95_ci':[samples[49],samples[1949]] if samples else None,
            'cost_currency':None,'promotion':'NOT_APPROVED',
            'scope':'合成上下文回放；真实模型调用，固定工具证据；不是完整买家 Agent 任务成功率或生产账单验收'}


async def close(model):
    await model.client.close()
    if model._fallback is not None:await model._fallback.client.close()


async def run(output, mode):
    output.mkdir(parents=True,exist_ok=False)
    settings=load_settings()
    # 只使用模型层，不创建 composition、买家库、向量库或语义缓存。
    settings=replace(settings,llm_fallback_model='',llm_max_retries=0)
    throttle=GatewayThrottle(1,max(1,settings.llm_min_interval_seconds))
    report={'mode':mode,'model':settings.llm_model,'repeats':3,'strategies':STRATEGIES,
            'semantic_answer_cache':False,'synthetic':True,'rows':[],
            'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
                'app/infrastructure/prompt_cache.py','app/infrastructure/llm.py','app/infrastructure/context_usage.py',
                'scripts/eval/prompt_cache.py')}}
    def save():
        if mode=='evaluate':report['summary']=aggregate(report['rows'])
        (output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if mode=='preflight':
        configured=load_settings()
        for name in dict.fromkeys([configured.llm_model,configured.llm_fallback_model]):
            if not name:continue
            model=create_chat_model(replace(settings,llm_model=name,prompt_cache_mode='explicit',prompt_cache_policy='static'),
                                    stream=False,throttle=throttle)
            model.client.timeout=40
            nonce=uuid.uuid4().hex
            try:
                for stage in ('cold','warm','warm_again','changed_prefix'):
                    prefix=uuid.uuid4().hex if stage=='changed_prefix' else nonce
                    messages,_=fixture('static_rules',prefix)
                    padding=messages[0].get_text_content().split('以下虚构目录仅作为背景，不覆盖权威状态。\n',1)[1]
                    messages=[msg('system','隔离缓存预检 '+prefix+'。仅回复 OK。以下是无关的虚构目录：\n'+padding),
                              msg('user','只做连接测试。仅回复 OK。')]
                    result=await invoke(model,messages)
                    result['passed']=result.get('text','').strip()=='OK'
                    result.update(model=name,stage=stage);report['rows'].append(result);save()
                    print(name,stage,result['passed'],flush=True)
            finally:await close(model)
    else:
        for case in CASES:
            for repeat in range(3):
                order=list(STRATEGIES);order=order[repeat:]+order[:repeat]
                active={}
                for strategy in order:
                    m,p=STRATEGIES[strategy]
                    model=create_chat_model(replace(settings,prompt_cache_mode=m,prompt_cache_policy=p),
                                            stream=repeat%2==1,throttle=throttle)
                    model.client.timeout=40
                    row={'case':case,'repeat':repeat,'strategy':strategy,'namespace':uuid.uuid4().hex,'calls':[],'passed':False}
                    report['rows'].append(row);active[strategy]=(model,row)
                try:
                    # 三组交错冷请求，再交错暖请求，避免某组固定吃到启动/负载偏差。
                    for phase in ('cold','warm','changed'):
                        for strategy in order:
                            model,row=active[strategy]
                            messages,expected=fixture(case,row['namespace'],phase=='changed')
                            if phase=='warm':messages[-1]=msg('user','再次核对目标的最新权威状态 JSON，不下单。')
                            if case=='summary_replaced' and phase=='changed':
                                summary=check_answer(await invoke(model,messages,structured=True,kind='summary'),expected)
                                summary.update(phase='summary',kind='summary');row['calls'].append(summary)
                                if summary['passed']:messages=[messages[0],msg('system','有效摘要：'+json.dumps(summary['answer'])),messages[-1]]
                            tools=deepcopy(TOOLS)
                            if case=='tool_definitions' and phase=='changed':
                                tools[0]['function']['description']+='；第二版合同，必须精确核对 SKU'
                            result=check_answer(await invoke(model,messages,tools=tools),expected)
                            result.update(phase=phase,kind='business',expected=expected)
                            row['calls'].append(result)
                            row['passed']=len(row['calls'])==(4 if case=='summary_replaced' else 3) and all(c['passed'] for c in row['calls'])
                            save()
                    print(case,repeat,{s:r['passed'] for s,(_,r) in active.items()},flush=True)
                finally:
                    for model,_ in active.values():await close(model)
    save()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('preflight','evaluate'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=asyncio.run(run(args.output,args.mode))
    if args.mode=='preflight':
        # 内容正确仍不足以证明缓存命中；至少要求每个模型有写、有读、变更前缀不读旧块。
        for name in {r['model'] for r in report['rows']}:
            rows=[r for r in report['rows'] if r['model']==name]
            caches=[s.get('prompt_cache',{}) for r in rows for s in r['usage']]
            changed=[s.get('prompt_cache',{}) for r in rows if r['stage']=='changed_prefix' for s in r['usage']]
            if not all(r['passed'] for r in rows) or not any((c.get('cache_read_tokens') or 0)>0 for c in caches) or not any((c.get('cache_write_tokens') or 0)>0 for c in caches) or not changed or any(c.get('cache_read_tokens')!=0 for c in changed):
                raise SystemExit(2)
    elif not all(r['passed'] for r in report['rows']):raise SystemExit(2)


if __name__=='__main__':main()
