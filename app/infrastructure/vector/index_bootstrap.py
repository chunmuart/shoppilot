"""启动时增量同步商品向量；已写入的批次在重启、故障重试时直接复用。"""
from __future__ import annotations

import logging
import math
from typing import Any

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex

logger = logging.getLogger(__name__)


async def bootstrap_product_index(
    product_repo: ProductRepository,
    embedder: EmbeddingClient,
    vector_index: ProductVectorIndex,
    *,
    batch_size: int = 10,
    report: dict[str, Any] | None = None,
) -> bool:
    """只推理缺失／变化的商品，每批持久写入；失败返回 False，并保留已完成批次。"""
    if batch_size < 1:
        raise ValueError('建库 batch_size 必须为正整数')
    result = report if report is not None else {}
    result.update(total_products=0, reused_products=0, embedded_products=0,
                  written_products=0, pending_products=0, batches=0, complete=False)
    try:
        products = await product_repo.list_all()
        result['total_products'] = len(products)
        if not products:
            logger.warning('商品库为空，跳过向量建库')
            return False
        pending = await vector_index.products_needing_embeddings(products)
        result['reused_products'] = len(products) - len(pending)
        result['pending_products'] = len(pending)
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            embeddings = await embedder.embed_batch([p.searchable_text() for p in batch])
            if len(embeddings) != len(batch) or not embeddings or not embeddings[0]:
                raise ValueError('embedding 返回数量或维度无效，未写入本批商品')
            dimension = len(embeddings[0])
            if not all(len(v) == dimension and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                for x in v
            ) for v in embeddings):
                raise ValueError('embedding 维度不一致或包含非有限数值，未写入本批商品')
            result['embedded_products'] += len(batch)
            await vector_index.ensure_ready(vector_dim=dimension)
            await vector_index.upsert_products(batch, embeddings)
            result['written_products'] += len(batch)
            result['pending_products'] -= len(batch)
            result['batches'] += 1
            result['vector_dimension'] = dimension
            logger.info('商品向量同步：复用 %d，写入 %d，待完成 %d',
                        result['reused_products'], result['written_products'], result['pending_products'])
        result['complete'] = True
        logger.info('商品向量就绪：共 %d，复用 %d，新推理 %d',
                    len(products), result['reused_products'], result['embedded_products'])
        return True
    except Exception as err:  # 建库失败不阻塞应用启动，但脚本必须据此返回失败。
        result['error_type'] = type(err).__name__
        logger.warning('商品向量同步未完成，已写入批次保留，检索可能降级：%s', err)
        return False
