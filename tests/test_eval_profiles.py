# -*- coding: utf-8 -*-
"""正式/降级检索评测 profile 的门禁口径。"""
from scripts.eval.run_product_recall import profile_thresholds


def test_online_main_keeps_quality_thresholds_but_not_unjudged_precision_gate():
    thresholds = profile_thresholds("online-main")

    assert thresholds.recall == 0.90
    assert thresholds.mrr == 0.85
    assert thresholds.ndcg == 0.85
    assert thresholds.precision is None
    assert thresholds.empty_accuracy == 1.0
    assert thresholds.filter_accuracy == 1.0


def test_offline_fallback_only_gates_safety_and_baseline_regression():
    thresholds = profile_thresholds("offline-fallback")

    assert thresholds.recall == 0.0
    assert thresholds.mrr == 0.0
    assert thresholds.ndcg == 0.0
    assert thresholds.precision is None
    assert thresholds.empty_accuracy == 1.0
    assert thresholds.filter_accuracy == 1.0


def test_knowledge_formal_gate_does_not_require_unjudged_precision():
    from scripts.eval.run_category_recall import formal_thresholds

    thresholds = formal_thresholds()

    assert thresholds.recall == 0.85
    assert thresholds.precision is None
    assert thresholds.mrr == 0.85
    assert thresholds.ndcg == 0.85
    assert thresholds.empty_accuracy == 1.0
    assert thresholds.policy_rejection_accuracy == 1.0


def test_hybrid_experiment_requires_observed_vector_and_rerank_without_changing_online_main():
    from scripts.eval.run_product_recall import hybrid_execution_errors

    assert profile_thresholds("online-main").required_recall_strategies == {"embedding_rerank"}
    valid = {"case_id": "hybrid", "actual_strategy": "hybrid_rerank", "vector_available": True, "rerank_applied": True, "raw_retrieved": ["P1"]}
    assert hybrid_execution_errors([valid]) == []
    assert hybrid_execution_errors([{**valid, "rerank_applied": False}])
    assert hybrid_execution_errors([{**valid, "vector_available": False}])
    assert hybrid_execution_errors([{**valid, "actual_strategy": "bm25"}])
    assert hybrid_execution_errors([{**valid, "actual_strategy": "hybrid_only", "raw_retrieved": [], "rerank_applied": False}]) == []


async def test_bm25_experimental_entry_never_initializes_online_dependencies(monkeypatch):
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    from scripts.eval import run_product_recall

    def forbidden():
        raise AssertionError("BM25 must stay offline")
    monkeypatch.setattr(run_product_recall, "load_settings", forbidden)
    usecase, _, strategy = await run_product_recall.build_usecase("bm25")
    result = await usecase.execute(ProductSearchSpec(normalized_query="旅行三件套", top_k=3))
    assert strategy == "bm25"
    assert result["recall_strategy"] == "bm25"
    assert result["retrieval_variant"] == "bm25_vector_rrf_v1"
