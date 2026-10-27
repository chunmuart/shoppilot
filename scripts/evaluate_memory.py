"""真实提炼模型小样本回归，不写买家记忆；输出可审阅事实及规则检查。"""
import argparse, asyncio, json
from pathlib import Path
from app.infrastructure.settings import load_settings
from app.infrastructure.llm import create_chat_model
from app.infrastructure.semantic_memory import PreferenceDistiller
from app.domain.buyer.preference import BuyerPreference

async def run(args):
    model=create_chat_model(load_settings(),stream=False)
    distiller=PreferenceDistiller(model);results=[]
    try:
        for case in json.loads(args.cases.read_text()):
            existing=[BuyerPreference('evaluation',p['kind'],p['statement'],memory_id=p['memory_id']) for p in case.get('existing',[])]
            try:
                facts=await distiller.extract(BuyerPreference('evaluation',case['kind'],case['input']),existing)
                statements='；'.join(f['statement'] for f in facts)
                checks={'count':len(facts)==case['count'],
                    'scope':all(x in statements for x in case.get('contains',[])),
                    'noise':all(x not in statements for x in case.get('absent',[])),
                    'polarity':all(f['kind']==case['fact_kind'] for f in facts) if 'fact_kind' in case else True,
                    'conflict':any(f.get('conflicts') for f in facts)==case['conflict'] if 'conflict' in case else True}
                row={'id':case['id'],'facts':facts,'checks':checks,'passed':all(checks.values())}
            except Exception as error:row={'id':case['id'],'passed':False,'error':type(error).__name__}
            results.append(row)
            print(json.dumps(row,ensure_ascii=False),flush=True)
        report={'model':load_settings().llm_model,'cases':len(results),'passed':sum(r['passed'] for r in results),'results':results,'limitation':'小样本规则评测；不代表完整事实蕴含、召回质量或生产准确率'}
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        if report["passed"]!=report["cases"]:raise SystemExit(1)
    finally:await model.client.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cases',type=Path,default=Path('eval/memory/cases.json'));parser.add_argument('--output',type=Path,default=Path('eval/verification/memory-upgrade-20260909/extraction.json'));asyncio.run(run(parser.parse_args()))
