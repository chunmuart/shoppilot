"""审核错标后对冻结输出重算；保留原报告，不重新调用模型，也不伪装成新留出实验。"""
import argparse
from collections import defaultdict
import copy
import hashlib
import json
from pathlib import Path
from scripts.eval.retrieval_v2 import score, aggregate
from scripts.eval.retrieval_upgrade import paired_interval


def rescore(original,old_cases,new_cases,catalog):
    old={c['id']:c for c in old_cases};new={c['id']:c for c in new_cases}
    if old.keys()!=new.keys():raise ValueError('不能新增或移除实验场景')
    allowed={'relevant','relevant_canonical_ids','gold_rule','expected_empty'}
    corrections=[]
    for cid,c in old.items():
        if {k:v for k,v in c.items() if k not in allowed}!={k:v for k,v in new[cid].items() if k not in allowed}:
            raise ValueError('只允许纠正金标，不允许改查询、切分或约束')
        if c['relevant_canonical_ids']!=new[cid]['relevant_canonical_ids']:
            corrections.append({'id':cid,'query':c['query'],'before':c['relevant_canonical_ids'],'after':new[cid]['relevant_canonical_ids']})
    result=copy.deepcopy(original);by_id={p['product_id']:p for p in catalog}
    for rows in result['samples'].values():
        for row in rows:
            case=new[row['id']]
            row.update(score([by_id[pid] for pid in row['ids']],case['relevant_canonical_ids'],original['k']))
            row['gold']=case['relevant_canonical_ids']
    result['summary']={n:aggregate(rows) for n,rows in result['samples'].items()}
    result['by_kind']={n:{kind:aggregate([r for r in rows if r['kind']==kind]) for kind in {r['kind'] for r in rows}}
                       for n,rows in result['samples'].items()}
    for comparison in result['paired']:
        candidate,baseline=comparison.split('_minus_')
        for metric in ('recall','mrr','ndcg'):
            families=defaultdict(list)
            for a,b in zip(result['samples'][baseline],result['samples'][candidate]):
                if a[metric] is not None:families[a['family']].append(b[metric]-a[metric])
            deltas=[sum(v)/len(v) for v in families.values()]
            result['paired'][comparison][metric]={'mean_delta':sum(deltas)/len(deltas),'family_bootstrap_95pct':paired_interval(deltas)}
    result['failures']={n:[s for s in rows if s['recall'] is not None and s['recall']<1 or s['empty_ok'] is False or s['violations']]
                        for n,rows in result['samples'].items()}
    result['gold_audit']={'corrections':corrections,'new_model_calls':0,'scope':'同一批冻结输出的金标纠错敏感性分析，不是新的独立留出验证'}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('report','old-dataset','dataset','catalog','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();read=lambda p:[json.loads(l) for l in p.read_text().splitlines()]
    r=rescore(json.loads(a.report.read_text()),read(a.old_dataset),read(a.dataset),read(a.catalog))
    r['gold_audit'].update(original_report_sha256=hashlib.sha256(a.report.read_bytes()).hexdigest(),
                          corrected_dataset_sha256=hashlib.sha256(a.dataset.read_bytes()).hexdigest())
    with a.output.open('x') as f:json.dump(r,f,ensure_ascii=False,indent=2)
    print(json.dumps(r['summary'],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
