"""候选覆盖与前排效果必须独立计分，采集阶段信息不能改变正常返回。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from scripts.eval.retrieval_stages import candidate_metrics, aggregate_candidates, verify_merge_union


def result_fixture():
    products = {pid: SimpleNamespace(product_id=pid, canonical_product_id=canonical)
                for pid, canonical in [('a1', 'A'), ('a2', 'A'), ('b', 'B'), ('x', 'X')]}
    result = {'hits': [{'product_id': 'x'}, {'product_id': 'a2'}], 'retrieval_stages': {
        'merged_candidates': ['a1', 'a2', 'b', 'x'],
        'fused_candidates': ['x', 'a2', 'b', 'a1'],
        'ranked_candidates': ['x', 'a2', 'b'],
    }}
    return result, products


def test_full_candidate_coverage_can_have_top_k_loss_and_deduplicates_listings():
    result, products = result_fixture()
    metric = candidate_metrics(result, products, ['A', 'B'])
    assert metric['candidate_recall'] == 1
    assert metric['candidate_listing_count'] == 4
    assert metric['candidate_canonical_count'] == 3
    assert metric['ranking_loss_recall'] == .5
    assert metric['ranking_lost_ids'] == ['B']
    assert metric['relevant_ranks'] == [{'canonical_id':'A','rank':2}, {'canonical_id':'B','rank':3}]


def test_negative_cases_do_not_inflate_candidate_recall():
    result, products = result_fixture()
    positive = candidate_metrics(result, products, ['A', 'B'])
    negative = candidate_metrics(result, products, [])
    summary = aggregate_candidates([positive, negative])
    assert negative['candidate_recall'] is None
    assert summary['positive_cases'] == 1 and summary['recall'] == 1
    assert summary['empty_failures'] == 1


@pytest.mark.parametrize('damage', ['fusion_loses', 'dedup_loses', 'wrong_top_k'])
def test_stage_scoring_rejects_lost_candidates_or_unrelated_final_results(damage):
    result, products = result_fixture()
    if damage == 'fusion_loses':
        result['retrieval_stages']['fused_candidates'].remove('b')
    elif damage == 'dedup_loses':
        result['retrieval_stages']['ranked_candidates'].remove('b')
    else:
        result['hits'] = [{'product_id':'b'}]
    with pytest.raises(ValueError):
        candidate_metrics(result, products, ['A', 'B'])


def test_merge_checks_full_union_and_detects_a_truncated_pool():
    rows = [{'case_id':'one','variant':variant,'retrieval_stages':{'merged_candidates':ids}}
            for variant, ids in [('bm25',['x','a']), ('dense',['a','b']), ('hybrid',['x','a','b'])]]
    assert verify_merge_union(rows)['union_verified']
    rows[-1]['retrieval_stages']['merged_candidates'].remove('b')
    with pytest.raises(ValueError):
        verify_merge_union(rows)


@pytest.mark.parametrize('lexical_weight', [0, 1])
@pytest.mark.parametrize('vector_fails', [False, True])
@pytest.mark.parametrize('rerank_enabled', [False, True])
async def test_real_pipeline_stage_capture_is_opt_in_and_does_not_change_results(lexical_weight, vector_fails, rerank_enabled):
    products = (await InMemoryProductRepository().list_all())[:12]
    repo = InMemoryProductRepository(products)
    index = SimpleNamespace(search_filtered=AsyncMock(return_value=[
        VectorHit(p.product_id, 1 / (i + 1)) for i,p in enumerate(products[:8])]))
    kwargs = dict(embedder=SimpleNamespace(embed=AsyncMock(return_value=[1.])),
                  vector_index=index, hybrid_enabled=True, hybrid_lexical_weight=lexical_weight)
    if vector_fails:
        index.search_filtered.side_effect = RuntimeError('测试向量服务不可用')
    if rerank_enabled:
        # 专用精排将融合顺序反转，确认采集的是各阶段的真实顺序。
        kwargs['reranker'] = SimpleNamespace(rerank=AsyncMock(
            side_effect=lambda query, documents: list(range(len(documents)))))
    plain = CatalogSearchUseCase(repo, **kwargs)
    observed = CatalogSearchUseCase(repo, **kwargs, capture_retrieval_stages=True)
    spec = ProductSearchSpec(normalized_query='旅行', top_k=3)
    expected = await plain.execute(spec)
    actual = await observed.execute(spec)
    trace = actual.pop('retrieval_stages')
    assert 'retrieval_stages' not in expected
    assert actual == expected
    assert set(trace['merged_candidates']) == set(trace['fused_candidates'])
    assert [h['product_id'] for h in actual['hits']] == trace['ranked_candidates'][:3]
    if vector_fails:
        assert trace['vector_candidates'] == []
        assert trace['merged_candidates'] == trace['lexical_candidates']
    elif lexical_weight == 0:
        assert trace['merged_candidates'] == trace['vector_candidates']
    if rerank_enabled:
        canonical_seen = set()
        by_id = {p.product_id: p for p in products}
        expected_ranked = []
        for pid in reversed(trace['fused_candidates']):
            canonical = by_id[pid].canonical_product_id or pid
            if canonical not in canonical_seen:
                canonical_seen.add(canonical)
                expected_ranked.append(pid)
        assert trace['ranked_candidates'] == expected_ranked
