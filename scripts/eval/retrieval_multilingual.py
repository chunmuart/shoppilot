"""多语言模块评测：旧/新词项消融及真实 qwen-text-rerank；禁用聊天模型。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
from functools import partial
import hashlib
import json
from pathlib import Path
import re
import statistics
import time
from unittest.mock import patch

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.rerank.http_reranker import HttpReranker
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
from app.infrastructure.context_usage import context_usage_sink
from scripts.retrieval_preflight import check
from scripts.eval.retrieval_v2 import score
from scripts.eval.retrieval_upgrade import paired_interval
from scripts.eval.hard_constraints import find_hit_constraint_violations
from scripts.generate_retrieval_multilingual import build_cases
from app.infrastructure.retrieval.bm25 import bm25_rank

ROOT = Path(__file__).resolve().parents[2]


def legacy_terms(text):
    # 冻结修改前的词项实现；其它过滤、候选预算及 RRF 完全相同。
    value = text.casefold()
    tokens = re.findall(r'[a-z0-9]+(?:[-_.][a-z0-9]+)*', value)
    for segment in re.findall(r'[\u4e00-\u9fff]+', value):
        tokens.extend(segment[i:i+2] for i in range(len(segment)-1))
        if len(segment) == 1:
            tokens.append(segment)
    return tokens


def summarize(rows):
    latencies = sorted(r['elapsed_ms'] for r in rows)
    return {
        'cases': len(rows),
        **{key: statistics.mean(r[key] for r in rows) if rows else None for key in ('recall', 'mrr', 'ndcg')},
        'p95_ms': latencies[max(0, __import__('math').ceil(len(latencies)*.95)-1)] if rows else None,
        'hard_constraint_failures': sum(bool(r['violations']) for r in rows),
        'rerank_successes': sum(r['rerank_applied'] for r in rows),
        'vector_successes': sum(r['vector_available'] for r in rows),
    }


def validate_cases(cases, records):
    # 固定查询、语种切片和金标归属同时校验，避免跨语种评测混入英语译文。
    if cases != build_cases(records):
        raise ValueError('评测查询或金标与冻结构建规则不一致')
    return True


async def evaluate(output: Path, *, live=False):
    output.mkdir(parents=True, exist_ok=False)
    settings = load_settings()
    if live and settings.reranker_model != 'qwen-text-rerank':
        raise ValueError('本评测只接受 qwen-text-rerank，不能用其它模型冒充')
    catalog_path = ROOT/'data/catalog-v3.jsonl'
    dataset_path = ROOT/'eval/v3/multilingual_retrieval.jsonl'
    records = [json.loads(line) for line in catalog_path.read_text().splitlines()]
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines()]
    validate_cases(cases, records)
    products = [_product_from_record(r) for r in records]
    by_id = {p.product_id:p for p in products}
    paths = [catalog_path, dataset_path, Path(__file__), ROOT/'scripts/generate_retrieval_multilingual.py',
             ROOT/'scripts/catalog_multilingual_data.py', ROOT/'scripts/generate_catalog_multilingual.py',
             ROOT/'app/infrastructure/retrieval/bm25.py', ROOT/'app/domain/catalog/product.py',
             ROOT/'app/application/usecases/catalog_search.py', ROOT/'app/infrastructure/rerank/http_reranker.py']
    fingerprint = lambda: {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {'catalog_size':len(products), 'cases':len(cases), 'frozen':fingerprint(),
              'models':{'embedding':settings.embedding_model, 'reranker':settings.reranker_model},
              'scope':'合成多语言类别意图试验；语种切片每组100件；不是全量目录混合检索收益验收',
              'baseline':'同一当前业务管线，旧 ASCII/Han 单词项 vs Unicode + 低权重子词；不是旧版整个系统',
              'parameters':{'top_k':5,'candidates':32,'rrf_weights':[1,1],'subword_weight':.25},
              'dependencies':{}, 'status':'OFFLINE_ONLY', 'samples':[], 'llm_calls':0}
    embedder = index = ranker = None
    if live:
        report['dependencies'] = await check(settings)
        if report['dependencies']['reranker']['status'] == 'PASS':
            ranker = HttpReranker(settings)
        if report['dependencies']['embedding']['status'] == 'PASS':
            embedder = OpenAIEmbeddingClient(settings)
            index = QdrantProductIndex(replace(settings,data_dir=output/'index',qdrant_url='',qdrant_collection='multilingual_v3'))
            try:
                vectors = await embedder.embed_batch([p.searchable_text() for p in products])
                await index.ensure_ready(len(vectors[0]))
                await index.upsert_products(products, vectors)
            except Exception as error:
                report['dependencies']['embedding'].update(status='BLOCKED_INDEX_BUILD',error_type=type(error).__name__)
                await index.close()
                index = embedder = None
        report['status'] = 'LIVE_COMPLETED' if index and ranker else 'BLOCKED_DEPENDENCIES'
    usages = []
    sink = context_usage_sink.set(usages.append)
    try:
        for i, case in enumerate(cases):
            # 显式用目标语种原文做候选，不让同一目录的英语译文替跨语种召回答题。
            pool = [p for p in products if p.source_platform == case['platform'] and p.source_language == case['corpus_language']]
            repo = InMemoryProductRepository(pool)
            variants = {'bm25_legacy_terms':CatalogSearchUseCase(repo,hybrid_enabled=True),
                        'bm25_unicode':CatalogSearchUseCase(repo,hybrid_enabled=True)}
            if ranker:
                variants['bm25_qwen_rerank'] = CatalogSearchUseCase(repo,reranker=ranker,hybrid_enabled=True)
            if index:
                variants['dense'] = CatalogSearchUseCase(repo,embedder,index,hybrid_enabled=True,hybrid_lexical_weight=0)
                variants['hybrid'] = CatalogSearchUseCase(repo,embedder,index,hybrid_enabled=True)
                if ranker:
                    variants['hybrid_qwen_rerank'] = CatalogSearchUseCase(repo,embedder,index,ranker,hybrid_enabled=True)
            names = list(variants)
            names = names[i % len(names):] + names[:i % len(names)]
            for name in names:
                spec = ProductSearchSpec(normalized_query=case['query'],ship_to=case['ship_to'],top_k=5)
                started = time.monotonic()
                if name == 'bm25_legacy_terms':
                    with patch('app.infrastructure.retrieval.bm25.terms',legacy_terms), patch(
                        'app.application.usecases.catalog_search.bm25_rank', partial(bm25_rank, subword_weight=0)
                    ):
                        result = await variants[name].execute(spec)
                else:
                    result = await variants[name].execute(spec)
                violations = find_hit_constraint_violations(result['hits'],by_id,ship_to=case['ship_to'])
                report['samples'].append({
                    'id':case['id'], 'variant':name, 'mode':case['mode'], 'family':case['family'],
                    'corpus_language':case['corpus_language'], 'query_language':case['query_language'], 'platform':case['platform'],
                    **score(result['hits'],case['relevant_canonical_ids'],5),
                    'elapsed_ms':(time.monotonic()-started)*1000,
                    'ids':[h['product_id'] for h in result['hits']], 'gold':case['relevant_product_ids'],
                    'violations':{k:sorted(v) for k,v in violations.items()},
                    'strategy':result['recall_strategy'], 'vector_available':result.get('vector_available',False),
                    'rerank_applied':result['rerank_applied'], 'diagnostics':result.get('retrieval_diagnostics',{}),
                })
            if (i+1)%50 == 0:
                print(json.dumps({'completed':i+1,'total':len(cases)}),flush=True)
    finally:
        context_usage_sink.reset(sink)
        if index:
            await index.close()
    grouped = defaultdict(list)
    for row in report['samples']:
        grouped[(row['variant'],row['mode'])].append(row)
    report['summary'] = {f'{v}/{m}':summarize(rows) for (v,m),rows in grouped.items()}
    report['by_language'] = {f'{variant}/{mode}/{language}':summarize([r for r in rows if r['corpus_language']==language])
                             for (variant,mode),rows in grouped.items() for language in sorted({r['corpus_language'] for r in rows})}
    report['by_query_language'] = {f'{variant}/{mode}/{language}':summarize([r for r in rows if r['query_language']==language])
                                  for (variant,mode),rows in grouped.items() for language in sorted({r['query_language'] for r in rows})}
    report['paired_tokenizer'] = {}
    for mode in ('monolingual','cross_language'):
        old = {r['id']:r for r in grouped[('bm25_legacy_terms',mode)]}
        new = grouped[('bm25_unicode',mode)]
        for metric in ('recall','ndcg'):
            families = defaultdict(list)
            for r in new:
                families[r['family']].append(r[metric]-old[r['id']][metric])
            differences = [statistics.mean(values) for values in families.values()]
            report['paired_tokenizer'][f'{mode}/{metric}'] = {'mean_difference':statistics.mean(differences),
                'cluster_count':len(differences),'ci95':paired_interval(differences),'unit':'商品意图族（平台与翻译共享的意图不当独立样本）'}
    report['usage'] = usages
    report['frozen_unchanged'] = report['frozen'] == fingerprint()
    report['actual_billed_cost'] = None
    expected_live = [r for r in report['samples'] if r['variant']=='hybrid_qwen_rerank']
    report['all_requested_live_paths_executed'] = len(expected_live)==len(cases) and all(r['vector_available'] and r['rerank_applied'] for r in expected_live)
    if live and not report['all_requested_live_paths_executed']:
        report['status'] = 'BLOCKED_DEPENDENCIES'
    failures = [r for r in report['samples'] if r['recall'] < 1 or r['ndcg'] < 1 or r['violations']]
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    (output/'failures.json').write_text(json.dumps(failures,ensure_ascii=False,indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--live',action='store_true')
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.output,live=args.live))
    print(json.dumps({'status':result['status'],'summary':result['summary']},ensure_ascii=False,indent=2))
    raise SystemExit(1 if args.live and not result['all_requested_live_paths_executed'] else 0)


if __name__ == '__main__':
    main()
