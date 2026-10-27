"""全量商品向量持久化及三路检索对照；真实本地模型、固定标注，不调用聊天模型。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
from scripts.eval.hard_constraints import find_hit_constraint_violations
from scripts.eval.local_multilingual_embedding import LocalMultilingualEmbedding, MODEL, model_files, model_version
from scripts.eval.retrieval_v2 import score, aggregate
from scripts.eval.retrieval_stages import candidate_metrics, aggregate_candidates, verify_merge_union

ROOT = Path(__file__).resolve().parents[2]


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build_semantic_cases(records):
    """同一需求的直接、同义、场景问法共用金标，不按被测算法的得分选题。"""
    cases = read_rows(ROOT / 'eval/v3/semantic_paraphrases.jsonl')
    references = {c['id']: c for c in read_rows(ROOT / 'eval/v2/product_retrieval.jsonl')}
    available = {r['canonical_product_id'] for r in records
                 if 'CN' in r['ships_to'] and any(s['stock'] > 0 for s in r['skus'])}
    pairs = defaultdict(list)
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('语义改写用例 ID 重复')
    for case in cases:
        reference = references[case['reference_case_id']]
        if (case['relevant_canonical_ids'] != reference['relevant_canonical_ids']
                or case['family'] != reference['family']
                or case['ship_to'] != reference['ship_to']
                or case['target_currency'] != reference['target_currency']
                or not set(case['relevant_canonical_ids']) <= available):
            raise ValueError('语义改写改变了金标或可售条件')
        if case['scope'] != 'semantic_' + case['query_kind']:
            raise ValueError('语义改写分组错误')
        pairs[case['pair_id']].append(case)
    for rows in pairs.values():
        if (Counter(c['query_kind'] for c in rows) != {'direct': 1, 'synonym': 1, 'scenario': 1}
                or len({c['reference_case_id'] for c in rows}) != 1
                or len({c['query'] for c in rows}) != 3):
            raise ValueError('同一需求必须有三种不同问法，且标准答案一致')
    return cases


def build_cases(records):
    """先冻结可审查金标；全库按同款计分，不能沿用某一语种切片的漏标。"""
    sliced = read_rows(ROOT / 'eval/v3/multilingual_retrieval.jsonl')
    cases = []; seen = set()
    for original in sliced:
        key = (original['query_language'], original['query'])
        if key in seen:
            continue
        seen.add(key)
        # 明确要求 Lingua 系列，使原有不同品牌的相似商品不成为未标出的相关项。
        relevant = sorted({r['canonical_product_id'] for r in records
                           if r['brand'] == 'Lingua' and r.get('evaluation_family') == original['family']
                           and 'CN' in r['ships_to'] and any(s['stock'] > 0 for s in r['skus'])})
        assert relevant
        cases.append({'id': f"full-{original['query_language']}-{original['family']:02d}",
                      'scope': 'full_multilingual', 'query': 'Lingua ' + original['query'],
                      'query_language': original['query_language'], 'family': original['family'],
                      'ship_to': 'CN', 'relevant_canonical_ids': relevant})
    for original in read_rows(ROOT / 'eval/v2/product_retrieval.jsonl'):
        if original['split'] == 'holdout':
            cases.append({**original, 'scope': 'full_business', 'query_language': 'zh'})
    for original in sliced:
        cases.append({**original, 'scope': 'language_slice'})
    cases.extend(build_semantic_cases(records))
    return cases


class NoInference(EmbeddingClient):
    async def embed(self, text):
        raise AssertionError('重启复用检查禁止调用 embedding')
    async def embed_batch(self, texts):
        raise AssertionError('重启复用检查禁止调用 embedding')


class FrozenQueryEmbedding(EmbeddingClient):
    def __init__(self, values):
        self.values = values
    async def embed(self, text):
        return self.values[text]
    async def embed_batch(self, texts):
        return [self.values[text] for text in texts]


def frozen_inputs():
    paths = [ROOT / 'data/catalog-v3.jsonl', ROOT / 'eval/v2/product_retrieval.jsonl',
             ROOT / 'eval/v3/multilingual_retrieval.jsonl',
             ROOT / 'eval/v3/semantic_paraphrases.jsonl', Path(__file__),
             ROOT / 'scripts/eval/local_multilingual_embedding.py',
             ROOT / 'scripts/eval/retrieval_v2.py', ROOT / 'scripts/eval/hard_constraints.py',
             ROOT / 'scripts/eval/retrieval_stages.py',
             ROOT / 'app/infrastructure/vector/embedding_identity.py',
             ROOT / 'app/infrastructure/vector/index_bootstrap.py',
             ROOT / 'app/infrastructure/vector/qdrant_product_index.py',
             ROOT / 'app/domain/catalog/product.py', ROOT / 'app/infrastructure/retrieval/bm25.py',
             ROOT / 'app/infrastructure/retrieval/fusion.py',
             ROOT / 'app/application/usecases/catalog_search.py']
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}


async def evaluate(args):
    args.output.mkdir(parents=True, exist_ok=False)
    records = read_rows(ROOT / 'data/catalog-v3.jsonl')
    products = [_product_from_record(r) for r in records]
    repo = InMemoryProductRepository(products)
    client = NoInference() if args.reuse_only else LocalMultilingualEmbedding(args.model_cache)
    settings = replace(load_settings(), data_dir=args.index_dir, qdrant_url='',
                       qdrant_collection='full_products_multilingual_minilm', embedding_base_url='local://fastembed',
                       embedding_model=MODEL, embedding_dim=384, embedding_version=model_version(args.model_cache))
    index = QdrantProductIndex(settings)
    report = {'status':'BUILDING', 'schema_version':'retrieval-stages-v1',
              'catalog_size':len(products), 'sku_count':sum(len(p.skus) for p in products),
              'models':{'embedding':MODEL, 'version':settings.embedding_version, 'reranker':None},
              'model_files':model_files(args.model_cache), 'model_key':index.embedding_key,
              'index_directory':str(args.index_dir.resolve()), 'collection':settings.qdrant_collection,
              'frozen':frozen_inputs(), 'index_build':{}, 'samples':[],
              'parameters':{'k':5, 'candidates':32, 'rrf_weights':[1,1]},
              'metric_definitions':{
                  'candidate_summary':'每路最多32条listing，混合取并集最多64条；按canonical去重计算候选池Recall，不截前5',
                  'final_top5_summary':'完成融合排序与同款去重后，最终前5条的Recall、MRR、NDCG',
                  'summary':'兼容旧报告字段，等同final_top5_summary，不是候选池召回率',
                  'ranking_loss_recall':'候选中已找到、但最终前5条未保留的正确同款数，除以全部正确同款数',
              }, 'merge_checks':[],
              'scope':'合成目录固定参数诊断；全库对照与100商品语种切片分别统计；不是新留出或生产收益验收',
              'latency_scope':'检索执行耗时不含预先编码的查询向量；单独记录查询编码总耗时', 'llm_calls':0}
    try:
        start = time.perf_counter()
        ok = await bootstrap_product_index(repo, client, index, batch_size=16, report=report['index_build'])
        report['index_build']['elapsed_seconds'] = time.perf_counter() - start
        report['index_build']['remaining_after_verify'] = len(await index.products_needing_embeddings(products))
        report['index_build']['collection_points'] = (await index._client.get_collection(index._collection)).points_count
        if not ok or report['index_build']['remaining_after_verify']:
            raise RuntimeError('未完成全量商品向量同步，停止评测')
        print(json.dumps({'index':report['index_build']},ensure_ascii=False),flush=True)
        if args.reuse_only or args.index_only:
            report['status'] = 'REUSE_VERIFIED' if args.reuse_only else 'INDEX_READY'
            return report
        cases = build_cases(records)
        (args.output/'cases.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in cases))
        report['cases_sha256'] = hashlib.sha256((args.output/'cases.jsonl').read_bytes()).hexdigest()
        report['case_counts'] = dict(Counter(c['scope'] for c in cases))
        queries = list(dict.fromkeys(c['query'] for c in cases))
        start = time.perf_counter(); query_vectors = {}
        for pos in range(0,len(queries),16):
            chunk = queries[pos:pos+16]
            query_vectors.update(zip(chunk,await client.embed_batch(chunk)))
        report['query_encoding'] = {'unique_queries':len(queries),'seconds':time.perf_counter()-start}
        (args.output/'query-vectors.json').write_text(json.dumps(query_vectors,ensure_ascii=False))
        embedder = FrozenQueryEmbedding(query_vectors)
        by_id = {p.product_id:p for p in products}
        for i,case in enumerate(cases):
            pool = products if case['scope'] != 'language_slice' else [p for p in products if p.source_platform == case['platform'] and p.source_language == case['corpus_language']]
            selected_repo = InMemoryProductRepository(pool)
            variants = {'bm25': CatalogSearchUseCase(selected_repo,hybrid_enabled=True,capture_retrieval_stages=True),
                        'dense': CatalogSearchUseCase(selected_repo,embedder,index,hybrid_enabled=True,hybrid_lexical_weight=0,capture_retrieval_stages=True),
                        'hybrid': CatalogSearchUseCase(selected_repo,embedder,index,hybrid_enabled=True,capture_retrieval_stages=True)}
            spec = ProductSearchSpec(normalized_query=case['query'],ship_to=case.get('ship_to'),
                                     price_max_major=case.get('price_max_major'),target_currency=case.get('target_currency','CNY'),
                                     category=case.get('category'),required_material_tags=case.get('required_material_tags',[]),
                                     excluded_material_tags=case.get('excluded_material_tags',[]),top_k=5)
            names=list(variants);names=names[i%3:]+names[:i%3]
            for name in names:
                start=time.perf_counter();result=await variants[name].execute(spec)
                elapsed=(time.perf_counter()-start)*1000
                if name != 'bm25' and not result.get('vector_available'):
                    raise RuntimeError('向量路径退化，禁止将结果写成纯向量或混合检索')
                violations=find_hit_constraint_violations(result['hits'],by_id,ship_to=spec.ship_to,
                    price_max_major=spec.price_max_major,target_currency=spec.target_currency,
                    category=spec.category,required_material_tags=spec.required_material_tags,excluded_material_tags=spec.excluded_material_tags)
                report['samples'].append({'case_id':case['id'],'scope':case['scope'],'variant':name,
                    'pair_id':case.get('pair_id'),'query_kind':case.get('query_kind'),
                    'query':case['query'],'query_language':case['query_language'],'corpus_language':case.get('corpus_language'),
                    'platform':case.get('platform'),'family':case['family'],'pool_size':len(pool),
                    **score(result['hits'],case['relevant_canonical_ids'],5),
                    **candidate_metrics(result,by_id,case['relevant_canonical_ids']),
                    'retrieval_stages':result['retrieval_stages'],
                    'elapsed_ms':elapsed,'violations':{k:sorted(v) for k,v in violations.items()},
                    'rerank_applied':result['rerank_applied'],'vector_available':result.get('vector_available',False),
                    'actual_strategy':result['recall_strategy'],'gold':case['relevant_canonical_ids'],
                    'hits':[{'product_id':h['product_id'],'canonical_product_id':h['canonical_product_id'],'title':h['title'],
                             'source_platform':h.get('source_platform'),'source_language':h.get('source_language')} for h in result['hits']]})
            report['merge_checks'].append(verify_merge_union(report['samples'][-3:]))
            if (i+1)%25==0 or i+1==len(cases):
                print(f"retrieval: {i+1}/{len(cases)}",flush=True)
                (args.output/'partial.json').write_text(json.dumps(report,ensure_ascii=False))
        groups=defaultdict(list)
        for row in report['samples']:
            groups[(row['scope'],row['variant'])].append(row)
        report['summary']={f'{scope}/{variant}':{'cases':len(rows),**aggregate(rows)} for (scope,variant),rows in groups.items()}
        report['final_top5_summary'] = report['summary']
        report['candidate_summary'] = {f'{scope}/{variant}':aggregate_candidates(rows)
                                       for (scope,variant),rows in groups.items()}
        report['by_language']={f'{scope}/{variant}/{lang}':{'cases':len(rows),**aggregate(rows)}
                              for (scope,variant) in groups for lang in sorted({r['query_language'] for r in groups[(scope,variant)]})
                              if (rows:=[r for r in groups[(scope,variant)] if r['query_language']==lang])}
        report['status']='COMPLETED'
        return report
    except Exception as error:
        report['status']='FAILED';report['error_type']=type(error).__name__
        raise
    finally:
        report['inputs_unchanged']=report['frozen']==frozen_inputs()
        if not report['inputs_unchanged']:
            report['status']='INPUTS_CHANGED'
        await index.close()
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        (args.output/'failures.json').write_text(json.dumps([r for r in report['samples'] if r['recall'] not in (None,1) or r['ndcg'] not in (None,1) or r['empty_ok'] is False or r['violations']],ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--index-dir',type=Path,default=ROOT/'data/product-vectors-multilingual')
    parser.add_argument('--model-cache',type=Path,default=ROOT/'.cache/multilingual-models')
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--index-only',action='store_true');mode.add_argument('--reuse-only',action='store_true')
    args=parser.parse_args()
    report=asyncio.run(evaluate(args))
    print(json.dumps({'status':report['status'],'summary':report.get('summary',{})},ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['status'] in ('COMPLETED','INDEX_READY','REUSE_VERIFIED') else 1)


if __name__=='__main__':
    main()
