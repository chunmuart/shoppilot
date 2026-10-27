"""多语言目录、Unicode 词项与权威商品事实的回归验证。"""
from collections import Counter
import json
from pathlib import Path
import unicodedata

import pytest
from app.infrastructure.persistence.seed_products import build_seed_products, _product_from_record
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.retrieval.bm25 import terms, bm25_rank
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from scripts.catalog_multilingual_data import PLATFORM_LANGUAGES, NAMES, PURPOSES
from scripts.generate_catalog_multilingual import build_records
from scripts.eval.data_quality import validate_catalog_raw_records, validate_catalog_products, validate_catalog_distribution

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_has_exact_language_coverage_and_preserves_old_records():
    records = [json.loads(line) for line in (ROOT/'data/catalog-v3.jsonl').read_text().splitlines()]
    old = [json.loads(line) for line in (ROOT/'data/catalog-v2.jsonl').read_text().splitlines()]
    assert records == build_records()
    assert records[:1000] == old
    new = records[1000:]
    counts = Counter((r['source_platform'], r['source_language']) for r in new)
    assert counts == {(platform, lang): 100 for platform, langs in PLATFORM_LANGUAGES.items() for lang in langs}
    assert len(new) == 2700
    assert len({r['product_id'] for r in records}) == len(records)
    assert all(r['data_provenance'] == 'synthetic' for r in new)
    for platform, languages in PLATFORM_LANGUAGES.items():
        for lang, locale in languages.items():
            group = [r for r in new if r['source_platform'] == platform and r['source_language'] == lang]
            assert len({r['title'] for r in group}) == 100
            assert len({r['description'] for r in group}) == 100
            assert len({r['evaluation_family'] for r in group}) == 25
            assert all(r['source_locale'] == locale for r in group)
    for lang in NAMES:
        assert len(NAMES[lang].split('|')) == len(PURPOSES[lang].split('|')) == 25


def test_catalog_quality_and_canonical_business_facts():
    records = build_records()
    products = [_product_from_record(r) for r in records]
    assert validate_catalog_raw_records(records) == []
    assert validate_catalog_products(products) == []
    assert validate_catalog_distribution(products) == []
    groups = {}
    for r in records[1000:]:
        # 本地化字段名不同，但同款的精确数值、材料和重量不能随翻译漂移。
        signature = (r['model_spec']['value'], r['model_spec']['unit'], tuple(r['material_tags']), r['weight_kg'], r['category'])
        assert groups.setdefault(r['canonical_product_id'], signature) == signature


@pytest.mark.parametrize('word', ['Ładowarka', 'Słuchawki', 'électronique', 'Größe', 'Mörkläggningsgardin', 'capacidade', 'العربية', 'наушники'])
def test_unicode_words_are_not_fragmented(word):
    assert terms(word) == [unicodedata.normalize('NFKC', word).casefold()]
    assert terms(unicodedata.normalize('NFD', word)) == terms(word)


def test_cjk_kana_and_model_tokens():
    assert terms('旅行背包') == ['旅行', '行背', '背包']
    assert 'ケッ' in terms('細口ケトル ケットル')
    assert 'ドホ' in terms('ヘッドホン')
    assert terms('ＵＳＢ－Ｃ WH-1000XM5') == ['usb-c', 'wh-1000xm5']


def test_metadata_does_not_leak_translation_or_evaluation_labels_to_index():
    p = next(p for p in build_seed_products() if p.source_language == 'pl')
    text = p.searchable_text()
    assert 'Plecak' in text and 'Podróże' in text
    for word in ('旅行装备', '合成聚合物', 'hard_negative', 'evaluation_family', 'long_tail', 'pl-PL'):
        assert word not in text


async def test_multilingual_card_and_authoritative_constraints():
    products = build_seed_products()
    usecase = CatalogSearchUseCase(InMemoryProductRepository(products), hybrid_enabled=True)
    result = await usecase.execute(ProductSearchSpec(normalized_query='Ładowarka USB-C 65 W', ship_to='CN', top_k=5))
    assert result['recall_strategy'] == 'bm25'
    assert result['hits']
    assert result['rerank_applied'] is False
    by_id = {p.product_id:p for p in products}
    for hit in result['hits']:
        product = by_id[hit['product_id']]
        assert 'CN' in hit['ships_to']
        assert product.has_available_sku()
        if product.source_language:
            assert hit['source_language'] == product.source_language
            assert hit['source_locale'] == product.source_locale
            assert hit['data_provenance'] == 'synthetic'
    # 精确引用仍以旧 SKU 权威字段为准，不受扩容或语种影响。
    exact = await usecase.execute(ProductSearchSpec(normalized_query='P1003-S1'))
    assert exact['hits'][0]['skus'][0]['sku_id'] == 'P1003-S1'


@pytest.mark.parametrize('language,query', [('pl','Ładowarka'), ('fr','céramique'), ('ja','ヘッドホン'), ('sv','Mörkläggningsgardin')])
def test_native_word_retrieval(language, query):
    products = [p for p in build_seed_products() if p.source_language == language]
    results = bm25_rank(query, products)
    assert results
    assert query.casefold() in results[0][1].searchable_text().casefold()


def test_evaluation_gold_belongs_to_target_language_and_excludes_unavailable():
    from scripts.eval.retrieval_multilingual import validate_cases
    cases = [json.loads(line) for line in (ROOT/'eval/v3/multilingual_retrieval.jsonl').read_text().splitlines()]
    records = build_records()
    assert len(cases) == 616
    assert Counter(c['mode'] for c in cases) == {'monolingual':216,'cross_language':400}
    assert sum(c['query_language'] == 'zh' for c in cases) == 216
    assert validate_cases(cases,records)
    by_id = {r['product_id']:r for r in records}
    for case in cases:
        for pid in case['relevant_product_ids']:
            r = by_id[pid]
            assert r['source_language'] == case['corpus_language']
            assert r['source_platform'] == case['platform']
            assert 'CN' in r['ships_to'] and any(s['stock'] > 0 for s in r['skus'])
    cases[0]['relevant_product_ids'].append('P1003')
    with pytest.raises(ValueError,match='金标'):
        validate_cases(cases,records)


async def test_evaluation_rejects_substitute_reranker_model(tmp_path, monkeypatch):
    from scripts.eval.retrieval_multilingual import evaluate
    monkeypatch.setenv('RERANKER_MODEL','not-qwen-text-rerank')
    with pytest.raises(ValueError,match='qwen-text-rerank'):
        await evaluate(tmp_path/'substitute',live=True)


def test_compound_word_recall_and_model_identity():
    from types import SimpleNamespace
    def item(pid, value):
        return SimpleNamespace(product_id=pid,searchable_text=lambda:value)
    products = [item('A','Wanderrucksack'), item('B','Keramiktasse'), item('C','WH-1000XM4')]
    assert bm25_rank('Rucksack', products)[0][1].product_id == 'A'
    assert bm25_rank('WH-1000XM5', products) == []
    assert bm25_rank('Rucksack', products, subword_weight=0) == []


def test_cached_terms_follow_text_changes_and_do_not_cache_inventory():
    from types import SimpleNamespace
    holder = {'text':'Wanderrucksack'}
    product = SimpleNamespace(product_id='A',searchable_text=lambda:holder['text'])
    assert bm25_rank('Rucksack',[product])
    holder['text'] = 'Keramiktasse'
    assert not bm25_rank('Rucksack',[product])
