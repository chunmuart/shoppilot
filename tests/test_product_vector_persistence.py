"""真实本地 Qdrant 验证重启复用、增量更新与故障续建，不调用远端模型。"""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from qdrant_client.models import PointStruct

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.vector.embedding_identity import embedding_identity
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex, _point_id
from tests.test_retrieval import _settings


class CountingEmbedding(EmbeddingClient):
    def __init__(self, fail_batch=None, vector=None):
        self.batches = []
        self.fail_batch = fail_batch
        self.vector = vector if vector is not None else [1.0, 0.25, 0.5]

    async def embed(self, text):
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts):
        self.batches.append(list(texts))
        if len(self.batches) == self.fail_batch:
            raise RuntimeError('模拟第二批服务失败')
        return [list(self.vector) for _ in texts]


def products():
    path = Path(__file__).resolve().parents[1] / 'data/catalog-v1.jsonl'
    return [_product_from_record(json.loads(line)) for line in path.read_text().splitlines()[:5]]


async def test_restart_reuses_all_vectors_without_embedding_service(tmp_path):
    settings = _settings(tmp_path)
    repo = InMemoryProductRepository(products())
    first = QdrantProductIndex(settings)
    embedder = CountingEmbedding()
    assert await bootstrap_product_index(repo, embedder, first, batch_size=2)
    assert sum(map(len, embedder.batches)) == 5
    await first.close()
    second = QdrantProductIndex(settings)
    offline = CountingEmbedding(fail_batch=1)
    report = {}
    try:
        assert await bootstrap_product_index(repo, offline, second, report=report)
        assert offline.batches == []
        assert report['reused_products'] == 5 and report['embedded_products'] == 0
        assert len(await second.search([1, 0.25, 0.5], top_n=5)) == 5
    finally:
        await second.close()


async def test_text_change_rebuilds_only_that_product_price_stock_do_not(tmp_path):
    items = products()
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository(items), CountingEmbedding(), index)
        items[0].description += ' 新增防水说明'
        items[1].skus[0] = replace(items[1].skus[0], stock=items[1].skus[0].stock + 3)
        items[2].skus[0] = replace(items[2].skus[0], price=replace(items[2].skus[0].price, amount_in_minor_units=9999))
        embedder = CountingEmbedding(); report = {}
        assert await bootstrap_product_index(InMemoryProductRepository(items), embedder, index, report=report)
        assert embedder.batches == [[items[0].searchable_text()]]
        assert report['reused_products'] == 4
    finally:
        await index.close()


async def test_failed_batch_keeps_prior_vectors_and_restart_only_fills_gaps(tmp_path):
    settings = _settings(tmp_path); items = products()
    first = QdrantProductIndex(settings); report = {}
    try:
        assert not await bootstrap_product_index(InMemoryProductRepository(items), CountingEmbedding(fail_batch=2), first, batch_size=2, report=report)
        assert report['written_products'] == 2 and report['pending_products'] == 3
    finally:
        await first.close()
    second = QdrantProductIndex(settings); embedder = CountingEmbedding(); report = {}
    try:
        assert await bootstrap_product_index(InMemoryProductRepository(items), embedder, second, batch_size=2, report=report)
        assert sum(map(len, embedder.batches)) == 3
        assert report['reused_products'] == 2
    finally:
        await second.close()


async def test_new_product_and_missing_point_are_both_filled(tmp_path):
    from qdrant_client.models import PointIdsList
    items = products(); index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert await bootstrap_product_index(InMemoryProductRepository(items[:4]), CountingEmbedding(), index)
        await index._client.delete(index._collection, PointIdsList(points=[_point_id(items[1].product_id)]), wait=True)
        embedder = CountingEmbedding()
        assert await bootstrap_product_index(InMemoryProductRepository(items), embedder, index)
        assert set(embedder.batches[0]) == {items[1].searchable_text(),items[4].searchable_text()}
    finally:
        await index.close()


async def test_model_version_change_excludes_old_vectors_until_rebuilt(tmp_path):
    settings = _settings(tmp_path); items = products()
    first = QdrantProductIndex(settings)
    assert await bootstrap_product_index(InMemoryProductRepository(items), CountingEmbedding(), first)
    await first.close()
    second = QdrantProductIndex(replace(settings, embedding_version='weights-v2'))
    try:
        assert await second.search([1, 0.25, 0.5], 5) == []
        assert len(await second.products_needing_embeddings(items)) == 5
        embedder = CountingEmbedding()
        assert await bootstrap_product_index(InMemoryProductRepository(items), embedder, second)
        assert sum(map(len, embedder.batches)) == 5
    finally:
        await second.close()


async def test_dimension_change_never_destroys_existing_collection(tmp_path):
    settings = _settings(tmp_path); items = products()
    first = QdrantProductIndex(settings)
    assert await bootstrap_product_index(InMemoryProductRepository(items), CountingEmbedding(), first)
    await first.close()
    second = QdrantProductIndex(replace(settings, embedding_model='other-model'))
    try:
        assert not await bootstrap_product_index(InMemoryProductRepository(items), CountingEmbedding(vector=[1,2]), second)
        info = await second._client.get_collection(second._collection)
        assert info.points_count == 5 and info.config.params.vectors.size == 3
    finally:
        await second.close()


async def test_unversioned_legacy_vectors_are_not_mistaken_for_verified_vectors(tmp_path):
    item = products()[0]; index = QdrantProductIndex(_settings(tmp_path))
    try:
        await index.ensure_ready(3)
        await index._client.upsert(index._collection, [PointStruct(id=_point_id(item.product_id), vector=[1.,0.,0.], payload={'product_id':item.product_id})])
        assert await index.products_needing_embeddings([item]) == [item]
        assert await bootstrap_product_index(InMemoryProductRepository([item]), CountingEmbedding(), index)
        assert await index.products_needing_embeddings([item]) == []
    finally:
        await index.close()


@pytest.mark.parametrize('vector', [[float('nan'),1], [float('inf'),1], []])
async def test_invalid_vector_is_never_saved(tmp_path, vector):
    index = QdrantProductIndex(_settings(tmp_path))
    try:
        assert not await bootstrap_product_index(InMemoryProductRepository(products()), CountingEmbedding(vector=vector), index)
        assert not await index._client.collection_exists(index._collection)
    finally:
        await index.close()


def test_embedding_identity_separates_services_models_versions_and_ignores_secrets(tmp_path):
    base = replace(_settings(tmp_path), embedding_base_url='https://models.example/v1', embedding_model='m')
    key = embedding_identity(base)
    assert key != embedding_identity(replace(base, embedding_version='v2'))
    assert key != embedding_identity(replace(base, embedding_model='m2'))
    assert key != embedding_identity(replace(base, embedding_base_url='https://other.example/v1'))
    assert key == embedding_identity(replace(base, embedding_api_key='different-secret'))
