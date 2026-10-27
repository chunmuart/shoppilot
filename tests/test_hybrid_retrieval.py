from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.retrieval.bm25 import bm25_rank, reciprocal_rank_fusion
from app.infrastructure.rag.category_knowledge import bootstrap_category_knowledge
from tests.test_phase3 import knowledge_base  # noqa: F401


def test_bm25_retains_exact_model_tokens_and_does_not_reward_spam():
    def product(pid, text):
        return SimpleNamespace(product_id=pid, searchable_text=lambda: text)
    products = [product("P1", "WH-1000XM5 Sony 耳机"), product("P2", "Sony " + "耳机 "*500)]
    assert bm25_rank("WH-1000XM5", products)[0][1].product_id == "P1"
    assert len(bm25_rank("不存在型号abc987", products)) == 0


def test_rrf_ignores_incomparable_raw_scores():
    a, b = SimpleNamespace(product_id="A"), SimpleNamespace(product_id="B")
    ranked = reciprocal_rank_fusion([(1000000, a), (1, b)], [(1, b)])
    assert ranked[0][1].product_id == "B"


def test_rrf_supports_frozen_weights_and_rejects_invalid_configuration():
    a, b = SimpleNamespace(product_id="A"), SimpleNamespace(product_id="B")
    ranked = reciprocal_rank_fusion([(1, a), (.5, b)], [(1, b), (.5, a)], weights=(2, 1))
    assert ranked[0][1].product_id == "A"
    for weights in [(1,), (0, 0), (-1, 1), (float("nan"), 1)]:
        with pytest.raises(ValueError):
            reciprocal_rank_fusion([(1, a)], [(1, b)], weights=weights)


async def test_default_vector_path_refills_after_hard_filters():
    repo = InMemoryProductRepository()
    products = await repo.list_all()
    selected = products[40]
    index = SimpleNamespace(search=AsyncMock(side_effect=lambda emb, top_n: [
        VectorHit(p.product_id, 1/(i+1)) for i, p in enumerate(products[:top_n])
    ]))
    usecase = CatalogSearchUseCase(repo, embedder=SimpleNamespace(embed=AsyncMock(return_value=[1])),
                                   vector_index=index)
    usecase._reject_reason = lambda p, s: None if p.product_id == selected.product_id else "category_mismatch"
    result = await usecase.execute(ProductSearchSpec(normalized_query="没有词项命中的查询xyz", top_k=1))
    assert [h["product_id"] for h in result["hits"]] == [selected.product_id]


async def test_hybrid_prefilters_dense_candidates_using_authoritative_product_ids():
    repo = InMemoryProductRepository()
    products = await repo.list_all()
    selected = products[-1]
    index = SimpleNamespace(
        search=AsyncMock(side_effect=AssertionError("支持过滤的索引不应先取全库前几项")),
        search_filtered=AsyncMock(return_value=[VectorHit(selected.product_id, 1)]),
    )
    usecase = CatalogSearchUseCase(repo, embedder=SimpleNamespace(embed=AsyncMock(return_value=[1])),
                                   vector_index=index, hybrid_enabled=True)
    usecase._reject_reason = lambda p, s: None if p.product_id == selected.product_id else "category_mismatch"
    result = await usecase.execute(ProductSearchSpec(normalized_query="没有词项命中的查询xyz", top_k=1))
    assert [h["product_id"] for h in result["hits"]] == [selected.product_id]
    assert index.search_filtered.await_args.kwargs["product_ids"] == [selected.product_id]
    assert result["retrieval_diagnostics"]["filter_mode"] == "authoritative_ids"


async def test_qdrant_filtered_dense_search_uses_existing_ids_without_sparse_vectors(tmp_path):
    from dataclasses import replace
    from app.infrastructure.settings import load_settings
    from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
    products = (await InMemoryProductRepository().list_all())[:3]
    index = QdrantProductIndex(replace(load_settings(), data_dir=tmp_path, qdrant_url=""))
    try:
        await index.ensure_ready(2)
        await index.upsert_products(products, [[1, 0], [.9, .1], [0, 1]])
        method = getattr(index, "search_filtered", None)
        assert method is not None, "需要支持按权威目录 ID 过滤召回"
        hits = await method([1, 0], top_n=2, product_ids=[products[2].product_id])
        assert [hit.product_id for hit in hits] == [products[2].product_id]
        assert await method([1, 0], top_n=2, product_ids=[]) == []
    finally:
        await index.close()


async def test_hybrid_filters_before_lexical_cut_and_deduplicates():
    repo = InMemoryProductRepository()
    usecase = CatalogSearchUseCase(repo, hybrid_enabled=True)
    result = await usecase.execute(ProductSearchSpec(normalized_query="旅行", ship_to="US", price_max_major=100, top_k=8, excluded_material_tags=["合成聚合物"]))
    assert result["recall_strategy"] == "bm25" and not result["rerank_applied"]
    for hit in result["hits"]:
        assert hit["price_major"] <= 100 and "US" in hit["ships_to"]
        assert "合成聚合物" not in hit["material_tags"]
    ids = [h["canonical_product_id"] or h["product_id"] for h in result["hits"]]
    assert len(ids) == len(set(ids))


async def test_adaptive_vector_recall_finds_valid_candidate_below_initial_window():
    repo = InMemoryProductRepository()
    products = await repo.list_all()
    excluded = [p for p in products if p.primary_available_sku().price.to_major_units() > 1][:64]
    selected = next(p for p in products if p.product_id not in {p.product_id for p in excluded})
    # 只允许末尾商品，证明补召回确实经过统一硬约束判断。
    index = SimpleNamespace(search=AsyncMock(side_effect=lambda emb, top_n: [VectorHit(p.product_id, 1/(i+1)) for i,p in enumerate([*excluded, selected][:top_n])]))
    usecase = CatalogSearchUseCase(repo, embedder=SimpleNamespace(embed=AsyncMock(return_value=[1])), vector_index=index, hybrid_enabled=True)
    usecase._reject_reason = lambda p,s: None if p.product_id == selected.product_id else "over_price_cap"
    result = await usecase.execute(ProductSearchSpec(normalized_query="unmatchable-model-abc", top_k=1))
    assert [h["product_id"] for h in result["hits"]] == [selected.product_id]
    assert index.search.await_count == 3
    assert result["recall_strategy"] == "hybrid_only"


async def test_knowledge_same_id_update_and_deleted_file_remove_old_chunks(knowledge_base, tmp_path):
    directory = tmp_path / "knowledge"
    (directory / "outdoor.md").write_text("# 户外新版\n登山杖新版本：只引用这一段。", encoding="utf-8")
    (directory / "guide.md").unlink()
    assert await bootstrap_category_knowledge(knowledge_base, directory) == 1
    docs = await knowledge_base.list_documents()
    assert [doc.document_id for doc in docs] == ["outdoor"]
    assert docs[0].metadata["content_sha256"]
    results = await knowledge_base.search(queries=["登山杖"], top_k=4)
    # 只校验召回正文，时间戳和内容哈希中的数字不代表旧知识仍被检索到。
    content = "\n".join(result.chunk.content.text for result in results)
    assert "新版本" in content and "防水等级" not in content and "800" not in content


async def test_exact_identifier_uses_authoritative_catalog_and_preserves_order():
    repo = InMemoryProductRepository()
    embed = AsyncMock(side_effect=AssertionError('精确实体不应交给向量判断存在性'))
    usecase = CatalogSearchUseCase(repo, embedder=SimpleNamespace(embed=embed), vector_index=SimpleNamespace())
    result = await usecase.execute(ProductSearchSpec(normalized_query='对比 P2222 和 P2206-S1', top_k=5))
    assert result['recall_strategy'] == 'exact_id_lookup'
    assert [h['product_id'] for h in result['hits']] == ['P2222', 'P2206']
    assert result['missing_identifiers'] == []
    assert result['existence_checked']
    embed.assert_not_awaited()


async def test_exact_identifier_never_substitutes_missing_sku_and_keeps_constraints():
    usecase = CatalogSearchUseCase(InMemoryProductRepository(), hybrid_enabled=True)
    result = await usecase.execute(ProductSearchSpec(normalized_query='P2206-S999 P999999 P2222', price_max_major=0))
    assert not result['hits']
    assert result['missing_identifiers'] == ['P2206-S999', 'P999999']
    assert result['filtered_out'][0]['reason'] == 'over_price_cap'
    assert result['filtered_out'][0]['product_id'] == 'P2222'


def test_knowledge_topics_use_longest_title_at_each_query_position():
    from app.infrastructure.rag.knowledge_retrieval import targeted_documents
    documents = [SimpleNamespace(document_id=str(i), metadata={'document_title':title}) for i,title in enumerate([
        '园艺品类洞察', '园艺价格与预算评测知识快照', '烘焙品类洞察', '烘焙参数判断评测知识快照'])]
    assert [d.document_id for d in targeted_documents(documents, '对比园艺和烘焙 参数判断', 3)] == ['0','3']
    assert [d.document_id for d in targeted_documents(documents, '园艺价格与预算有哪些依据', 3)] == ['1']


async def test_knowledge_deduplicates_chunks_and_declines_missing_required_facts():
    from app.infrastructure.rag.knowledge_retrieval import search_knowledge
    kb = SimpleNamespace(search=AsyncMock(return_value=[SimpleNamespace(document_id=i) for i in ['a','a','b','b','c']]))
    assert [r.document_id for r in await search_knowledge(kb, '如何挑选轻便睡袋', 3)] == ['a','b','c']
    kb.search.reset_mock()
    assert await search_knowledge(kb, '没有重量和目的国，请直接告诉我精确关税金额', 3) == []
    kb.search.assert_not_awaited()


async def test_knowledge_scoped_lookup_covers_named_document_outside_vector_window(knowledge_base):
    from app.infrastructure.rag.knowledge_retrieval import search_knowledge
    original = knowledge_base.search
    # 模拟大量相近文档挤掉指定标题：补召回必须经真实Qdrant元数据过滤。
    knowledge_base.search = AsyncMock(return_value=[])
    docs = await knowledge_base.list_documents()
    title = docs[0].metadata['document_title']
    results = await search_knowledge(knowledge_base, title+'有哪些注意事项', 1)
    assert len(results)==1 and results[0].document_id == docs[0].document_id
    knowledge_base.search = original


def test_knowledge_evidence_gate_allows_explanation_of_verification_method():
    from app.infrastructure.rag.knowledge_retrieval import unsupported_fact_reason
    assert unsupported_fact_reason('如何核对精确税率和清关时效，需要哪些证据？') is None
    assert unsupported_fact_reason('请直接给出精确税率和清关时效') is not None


async def test_exact_sku_price_and_card_default_never_fall_back_to_cheaper_sibling():
    usecase = CatalogSearchUseCase(InMemoryProductRepository())
    blocked = await usecase.execute(ProductSearchSpec(normalized_query='P1001-S2', price_max_major=190))
    assert not blocked['hits'] and blocked['filtered_out'][0]['price_major']==199
    assert blocked['filtered_out'][0]['sku_id']=='P1001-S2'
    accepted = await usecase.execute(ProductSearchSpec(normalized_query='P1001 P1001-S2', price_max_major=200, ship_to='CN'))
    hit=accepted['hits'][0]
    assert hit['default_sku_id']=='P1001-S2' and hit['price_major']==199
    assert [sku['sku_id'] for sku in hit['skus']]==['P1001-S2']
    assert hit['landed_price']['subtotal_major']==199
    missing = await usecase.execute(ProductSearchSpec(normalized_query='P1001 P1001-S99'))
    assert not missing['hits'] and missing['missing_identifiers']==['P1001-S99']


@pytest.mark.parametrize('query', [
    '请直接告诉我未登记平台的售后承诺。',
    '请直接告诉我未给地址时的末端派送时间。',
    '请直接告诉我没有来源的网传免税额度。',
    '没给收货地址，直接给出准确到货日期。',
    '请给出未知商家的保修承诺。',
])
def test_static_knowledge_does_not_substitute_neighbours_for_missing_evidence(query):
    from app.infrastructure.rag.knowledge_retrieval import unsupported_fact_reason
    assert unsupported_fact_reason(query) is not None
