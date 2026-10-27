"""全库评测不重复计算平台翻译问题，相关同款不能因目标站点缺货被漏标。"""
from collections import Counter

import pytest

from scripts.eval import full_catalog_retrieval
from scripts.eval.full_catalog_retrieval import ROOT, build_cases, build_semantic_cases, read_rows


def test_full_catalog_cases_are_unique_and_keep_slices_separate():
    records = read_rows(ROOT / 'data/catalog-v3.jsonl')
    cases = build_cases(records)
    assert Counter(c['scope'] for c in cases) == {
        'full_multilingual':88,'full_business':54,'language_slice':616,
        'semantic_direct':24,'semantic_synonym':24,'semantic_scenario':24,
    }
    full = [c for c in cases if c['scope']=='full_multilingual']
    assert len({c['query'] for c in full})==88
    assert Counter(c['query_language'] for c in full)=={language:8 for language in ('zh','en','es','de','fr','ja','nl','pl','sv','it','pt')}
    assert all(c['query'].startswith('Lingua ') for c in full)
    assert len({(c['scope'],c['id']) for c in cases})==len(cases)


def test_full_catalog_gold_includes_alternatives_available_on_other_platforms():
    cases=build_cases(read_rows(ROOT/'data/catalog-v3.jsonl'))
    full=next(c for c in cases if c['id']=='full-en-00')
    sliced=next(c for c in cases if c['id']=='ml-amazon-en-en-00')
    # 同款 20L 在 Amazon 英文切片不可配送，但全库其他 listing 可配送，必须计入全库金标。
    assert 'CAN-ML-01-1' not in sliced['relevant_canonical_ids']
    assert 'CAN-ML-01-1' in full['relevant_canonical_ids']
    assert set(sliced['relevant_canonical_ids']) < set(full['relevant_canonical_ids'])


def test_business_regression_preserves_original_holdout_queries_and_gold():
    original=[c for c in read_rows(ROOT/'eval/v2/product_retrieval.jsonl') if c['split']=='holdout']
    current=[c for c in build_cases(read_rows(ROOT/'data/catalog-v3.jsonl')) if c['scope']=='full_business']
    assert len(original)==len(current)==54
    for before,after in zip(original,current):
        assert all(after[key]==value for key,value in before.items())


def test_semantic_cases_cover_every_original_positive_family_with_identical_gold():
    records = read_rows(ROOT / 'data/catalog-v3.jsonl')
    cases = build_semantic_cases(records)
    original = {c['family']: c for c in read_rows(ROOT / 'eval/v2/product_retrieval.jsonl')
                if c['split'] == 'holdout' and c['kind'] == 'intent'}
    assert set(c['family'] for c in cases) == set(original)
    assert len(cases) == 3 * len(original) == 72
    for case in cases:
        assert case['relevant_canonical_ids'] == original[case['family']]['relevant_canonical_ids']
        assert case['query'].startswith('Roamix ')
        assert '独立人工复核' in case['provenance']
    powerbank = {c['query_kind']: c for c in cases if c['family'] == 7}
    assert '移动电源' in powerbank['direct']['query']
    assert '充电宝' in powerbank['synonym']['query']
    assert '移动电源' not in powerbank['synonym']['query']
    assert '充电宝' not in powerbank['scenario']['query']
    assert '移动电源' not in powerbank['scenario']['query']


@pytest.mark.parametrize('damage', ['gold', 'duplicate', 'missing_query_kind'])
def test_semantic_cases_reject_changed_gold_or_incomplete_pairs(monkeypatch, damage):
    records = read_rows(ROOT / 'data/catalog-v3.jsonl')
    cases = read_rows(ROOT / 'eval/v3/semantic_paraphrases.jsonl')
    if damage == 'gold':
        cases[0]['relevant_canonical_ids'] = ['CAN-ML-01-1']
    elif damage == 'duplicate':
        cases.append(cases[0])
    else:
        cases.pop(0)
    def read_fixture(path):
        return cases if path.name == 'semantic_paraphrases.jsonl' else read_rows(path)
    monkeypatch.setattr(full_catalog_retrieval, 'read_rows', read_fixture)
    with pytest.raises(ValueError):
        build_semantic_cases(records)
