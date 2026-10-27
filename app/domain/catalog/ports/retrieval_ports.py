# -*- coding: utf-8 -*-
"""检索基础设施端口：EmbeddingClient / ProductVectorIndex / Reranker

Domain 不关心实现：Infrastructure 提供 OpenAI 兼容 embedding、Qdrant 索引、HTTP reranker。
UseCase 通过这三个端口完成"embed → 向量召回 → rerank"二阶段召回，
任一环节不可用时由 UseCase 负责降级（关键词召回 / 跳过精排）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.catalog.product import Product


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class VectorHit:
    product_id: str
    score: float


class ProductVectorIndex(ABC):
    async def products_needing_embeddings(self, products: list[Product]) -> list[Product]:
        """返回向量缺失或已过期的商品；不支持版本查询的旧适配器全量构建。"""
        return products

    async def search_filtered(self, embedding: list[float], top_n: int, *, product_ids: list[str]) -> list[VectorHit] | None:
        """按权威目录许可的 ID 召回；旧适配器返回 None，由应用层渐进补召回。"""
        return None

    @abstractmethod
    async def ensure_ready(self, vector_dim: int) -> None:
        """确保 collection 存在（幂等）。"""

    @abstractmethod
    async def upsert_products(self, products: list[Product], embeddings: list[list[float]]) -> None:
        ...

    @abstractmethod
    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        ...


class Reranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长的精排分数；失败抛异常，由调用方降级。"""
