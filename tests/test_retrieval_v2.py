"""千件目录评测：无模型依赖时不得误报在线通过，金标不把排序位置当成偏好。"""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from scripts.eval.retrieval_v2 import score, validate_cases, evaluate
from app.infrastructure.persistence.seed_products import build_seed_products


def test_binary_ndcg_and_duplicate_products():
    assert (
        score(
            [
                {"product_id": "A", "canonical_product_id": "C"},
                {"product_id": "B", "canonical_product_id": "C"},
            ],
            ["C"],
            5,
        )["ndcg"]
        == 1
    )
    assert (
        score(
            [
                {"product_id": "B", "canonical_product_id": "B"},
                {"product_id": "A", "canonical_product_id": "A"},
            ],
            ["A", "B"],
            5,
        )["ndcg"]
        == 1
    )
    assert score([], [], 5) == {
        "recall": None,
        "mrr": None,
        "ndcg": None,
        "empty_ok": True,
    }


def test_frozen_splits_and_catalog_gold():
    cases = [
        json.loads(l)
        for l in Path("eval/v2/product_retrieval.jsonl").read_text().splitlines()
    ]
    assert len(cases) == 72
    validate_cases(cases, build_seed_products())


async def test_blocked_services_never_count_as_live_success(tmp_path, monkeypatch):
    from scripts.eval import retrieval_v2

    monkeypatch.setattr(
        retrieval_v2,
        "check",
        AsyncMock(
            return_value={
                "embedding": {"status": "BLOCKED"},
                "reranker": {"status": "BLOCKED"},
            }
        ),
    )
    r = await evaluate(tmp_path / "report", "dev", live=True)
    assert r["llm_calls"] == 0
    assert r["status"] == "BLOCKED_DEPENDENCIES"
    assert not r["all_live_paths_executed"]
    assert r["summary"]["bm25"]["hard_constraint_failures"] == 0


def test_explicit_intent_requirements_exclude_contradicting_tiers():
    cases = {c["id"]:c for c in (json.loads(l) for l in Path("eval/v2/product_retrieval.jsonl").read_text().splitlines())}
    for family, minimum in ((3,2),(5,2),(6,2),(17,2),(26,1),(27,1)):
        gold = cases[f"v2-{family:02d}-intent"]["relevant_canonical_ids"]
        assert gold
        assert all(int(cid.rsplit("-",1)[1]) >= minimum+1 for cid in gold)


def test_gold_rescore_keeps_original_and_rejects_query_changes():
    import copy
    import pytest
    from scripts.eval.rescore_retrieval import rescore
    row={'id':'x','family':1,'kind':'intent','ids':['A'],'recall':1.,'mrr':1.,'ndcg':1.,'empty_ok':None,
         'elapsed_ms':1,'violations':{},'rerank_applied':True,'vector_available':True,'gold':['C']}
    report={'k':5,'samples':{'a':[row]},'paired':{}}
    old=[{'id':'x','query':'A4','split':'holdout','relevant':['A'],'relevant_canonical_ids':['C'],'expected_empty':False}]
    new=copy.deepcopy(old);new[0]['relevant']=['B'];new[0]['relevant_canonical_ids']=['D']
    r=rescore(report,old,new,[{'product_id':'A','canonical_product_id':'C'}])
    assert report['samples']['a'][0]['recall']==1
    assert r['samples']['a'][0]['recall']==0
    assert r['gold_audit']['new_model_calls']==0
    new[0]['query']='改写后的查询'
    with pytest.raises(ValueError,match='不允许改查询'):
        rescore(report,old,new,[])
