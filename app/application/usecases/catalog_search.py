# -*- coding: utf-8 -*-
"""CatalogSearchUseCase

商品检索核心 UseCase，对齐参考实现五步流程：
    1. EmbeddingClient 把 normalized_query 向量化
    2. ProductVectorIndex.search(top_n) 拿候选 product_id（Qdrant，COSINE）
    3. ProductRepository.find_by_ids 还原 Product 聚合
    4. Reranker 精排取 top_k；失败/未配置降级按向量分排序（rerank_applied=false）
    5. 组装商品卡 JSON；命中 ship_to 时内联到手价（小计+运费+关税，统一目标币种）

降级链（recall_strategy 如实标注）：
    embedding_rerank → embedding_only → keyword_2gram（embedding 服务异常时兜底）

计价收敛设计：到手价在检索链路内联计算（TariffSchedule 规则内核），
不给 Agent 单独暴露比价/运费工具，减少不必要的工具调用轮次。

过滤可观测：被 ship_to / price_max_major 硬约束挡掉的候选以 filtered_out 摘要回传，
让模型能区分"库里没有这个商品"与"有但不满足约束"，不致于给出误导性结论。
"""
from __future__ import annotations

import logging
import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional

from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.ports.retrieval_ports import (
    EmbeddingClient,
    ProductVectorIndex,
    Reranker,
)
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.shipping.tariff_schedule import TariffSchedule
from app.application.usecases.product_media import product_media
from app.infrastructure.retrieval.bm25 import bm25_rank, reciprocal_rank_fusion

logger = logging.getLogger(__name__)

# 一阶段召回候选数（> top_k，给精排留空间）
_RECALL_TOP_N = 8

# 被硬约束挡掉的候选回传条数上限（只回摘要，避免上下文膨胀）
_FILTERED_OUT_LIMIT = 3


@dataclass(frozen=True)
class ProductCard:
    product_id: str
    title: str
    brand: str
    category: str
    origin_country: str
    price_major: float
    currency: str
    source_price_major: float
    source_currency: str
    highlights: list[str]
    skus: list[dict]
    score: float
    landed_price: Optional[dict]  # ship_to 命中时的到手价明细，未命中为 None
    source_platform: str
    canonical_product_id: str
    material_tags: list[str]
    weight_kg: float
    # 仅扩展展示投影，已有检索与报价字段保持兼容。
    description: str = ""
    rating_summary: dict[str, float | int] | None = None
    rating_is_live: bool = False  # 当前目录是评测快照，评分不来自实时平台查询。
    ships_to: list[str] = field(default_factory=list)
    dimensions_cm: dict[str, float] = field(default_factory=dict)
    updated_at: str = ""
    default_sku_id: str = ""
    image_url: str | None = None
    image_kind: str = "placeholder"
    image_alt: str = "暂无商品图片"
    source_language: str = ""
    source_locale: str = ""
    data_provenance: str = ""

    def to_dict(self) -> dict:
        card = {
            "product_id": self.product_id,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "origin_country": self.origin_country,
            "price_major": self.price_major,
            "currency": self.currency,
            "source_price_major": self.source_price_major,
            "source_currency": self.source_currency,
            "highlights": self.highlights,
            "skus": self.skus,
            "score": round(self.score, 4),
            "source_platform": self.source_platform,
            "canonical_product_id": self.canonical_product_id,
            "material_tags": self.material_tags,
            "weight_kg": self.weight_kg,
            "description": self.description,
            "rating_summary": self.rating_summary,
            "rating_is_live": self.rating_is_live,
            "ships_to": self.ships_to,
            "dimensions_cm": self.dimensions_cm,
            "updated_at": self.updated_at,
            "default_sku_id": self.default_sku_id,
            "image_url": self.image_url,
            "image_kind": self.image_kind,
            "image_alt": self.image_alt,
        }
        if self.landed_price is not None:
            card["landed_price"] = self.landed_price
        if self.source_language:
            card.update(source_language=self.source_language, source_locale=self.source_locale)
        if self.data_provenance:
            card["data_provenance"] = self.data_provenance
        return card


def tokenize(text: str) -> set[str]:
    """极简分词：空格切词 + 中文连续段落的 2-gram（关键词降级召回用）。"""
    terms: set[str] = set()
    for chunk in text.lower().split():
        terms.add(chunk)
        # 对含 CJK 的 chunk 补 2-gram，缓解中文无空格问题
        if any("\u4e00" <= ch <= "\u9fff" for ch in chunk) and len(chunk) >= 2:
            terms.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return terms


class CatalogSearchUseCase:
    def __init__(
        self,
        product_repo: ProductRepository,
        embedder: Optional[EmbeddingClient] = None,
        vector_index: Optional[ProductVectorIndex] = None,
        reranker: Optional[Reranker] = None,
        tariff_schedule: Optional[TariffSchedule] = None,
        hybrid_enabled: bool = False,
        hybrid_lexical_weight: float = 1.0,
        hybrid_vector_weight: float = 1.0,
        recall_candidates: int = 32,
        capture_retrieval_stages: bool = False,
    ) -> None:
        # 仅评测主动开启；记录混合检索的实际阶段，不改变排序或向模型增加字段。
        self._capture_retrieval_stages = capture_retrieval_stages
        self._hybrid_enabled = hybrid_enabled
        self._fusion_weights = (hybrid_lexical_weight, hybrid_vector_weight)
        reciprocal_rank_fusion([], [], weights=self._fusion_weights)
        if type(recall_candidates) is not int or not 8 <= recall_candidates <= 256:
            raise ValueError("recall_candidates 须为 8 到 256 的整数")
        self._recall_candidates = recall_candidates
        self._product_repo = product_repo
        self._embedder = embedder
        self._vector_index = vector_index
        self._reranker = reranker
        self._tariff = tariff_schedule or TariffSchedule(rates=ExchangeRateTable())

    async def execute(self, spec: ProductSearchSpec) -> dict:
        # 稳定实体 ID 直接核对权威目录，不能用向量 top-N 判断商品是否存在。
        # 结构化标识优先；旧查询文字中的 ID 仍兼容，显式 ID 未命中不能退回模糊检索。
        exact_id = spec.sku_id or spec.product_id
        identifiers = [exact_id] if exact_id else list(dict.fromkeys(re.findall(r"(?<![A-Za-z0-9])P\d{4,}(?:-S\d+)?(?![A-Za-z0-9-])", spec.normalized_query.upper())))
        if identifiers:
            return await self._execute_exact_ids(spec, identifiers)
        if self._hybrid_enabled:
            return await self._execute_hybrid(spec)
        scored: list[tuple[float, Product]] = []
        recall_strategy = "keyword_2gram"
        rerank_applied = False

        if self._embedder is not None and self._vector_index is not None:
            try:
                scored = await self._vector_recall(spec, adaptive=True)
                recall_strategy = "embedding_only"
            except Exception as err:  # noqa: BLE001 —— 召回基建异常必须降级而非失败
                logger.warning("向量召回不可用，降级关键词召回：%s", err)
                scored = []

        if recall_strategy == "embedding_only" and scored:
            # 二阶段精排；失败降级按向量分排序
            try:
                scored = await self._rerank(spec, scored)
                recall_strategy = "embedding_rerank"
                rerank_applied = True
            except Exception as err:  # noqa: BLE001
                logger.warning("rerank 不可用，按向量分排序：%s", err)
        elif not scored:
            scored = await self._keyword_recall(spec)
            recall_strategy = "keyword_2gram"

        # ship_to / 价格硬约束过滤 + top_k 截断（硬约束走结构化过滤，不交给模型）
        filtered: list[tuple[float, Product]] = []
        filtered_out: list[dict] = []
        for score, product in scored:
            reason = self._reject_reason(product, spec)
            if reason is None:
                filtered.append((score, product))
            elif len(filtered_out) < _FILTERED_OUT_LIMIT:
                filtered_out.append(self._to_rejected(product, spec, reason))

        hits = [self._to_card(score, product, spec) for score, product in filtered[: spec.top_k]]
        result = {
            "hits": [card.to_dict() for card in hits],
            "total_candidates": len(filtered),
            "recall_strategy": recall_strategy,
            "rerank_applied": rerank_applied,
        }
        if filtered_out:
            # 如实告知"召回到了但被硬约束挡掉"，否则模型分不清"库里没有"与"被过滤"，
            # 会把超预算商品答成"没有这个商品"
            result["filtered_out"] = filtered_out
        return result

    async def _execute_exact_ids(self, spec: ProductSearchSpec, identifiers: list[str]) -> dict:
        product_ids = list(dict.fromkeys(identifier.split("-S", 1)[0] for identifier in identifiers))
        products = {p.product_id: p for p in await self._product_repo.find_by_ids(product_ids)}
        hits, rejected, missing = [], [], []
        for product_id in product_ids:
            product = products.get(product_id)
            requested = [identifier for identifier in identifiers if identifier.split("-S", 1)[0] == product_id]
            if product is None:
                missing.extend(requested)
                continue
            specific = [identifier for identifier in requested if "-S" in identifier]
            # 同一商品出现明确规格后，不能再因泛商品 ID 而回退默认规格。
            selected = specific or [product.primary_available_sku().sku_id]
            eligible_skus = []
            for sku_id in selected:
                sku = product.find_sku(sku_id)
                if sku is None:
                    missing.append(sku_id)
                    continue
                reason = self._reject_reason(product, spec, primary=sku)
                if reason:
                    rejected.append({**self._to_rejected(product, spec, reason, primary=sku), "sku_id": sku_id})
                else:
                    eligible_skus.append(sku)
            if eligible_skus:
                card = self._to_card(1.0, product, spec, primary=eligible_skus[0]).to_dict()
                if specific:
                    accepted = {sku.sku_id for sku in eligible_skus}
                    card["skus"] = [sku for sku in card["skus"] if sku["sku_id"] in accepted]
                hits.append(card)
        return {"hits": hits[:spec.top_k], "total_candidates": len(hits),
                "recall_strategy": "exact_id_lookup", "rerank_applied": False,
                "requested_identifiers": identifiers, "missing_identifiers": missing,
                "filtered_out": rejected[:_FILTERED_OUT_LIMIT], "existence_checked": True}

    async def _execute_hybrid(self, spec: ProductSearchSpec) -> dict:
        # 两路共享同一份权威目录快照；过滤资格不依赖向量库的陈旧价格或库存。
        products = await self._product_repo.list_all()
        permitted = [p.product_id for p in products if self._reject_reason(p, spec) is None]
        diagnostics = {"filter_mode": "adaptive_post_filter", "eligible_products": len(permitted),
                       "lexical_weight": self._fusion_weights[0], "vector_weight": self._fusion_weights[1],
                       "candidate_limit": max(self._recall_candidates, spec.top_k * 4)}
        async def lexical():
            return bm25_rank(spec.normalized_query, products)
        async def vector():
            if self._embedder is None or self._vector_index is None:
                return None
            try:
                embedding = await self._embedder.embed(spec.normalized_query)
                filtered_search = getattr(self._vector_index, "search_filtered", None)
                if filtered_search is not None:
                    hits = await filtered_search(embedding, top_n=diagnostics["candidate_limit"], product_ids=permitted)
                    if hits is not None:
                        diagnostics["filter_mode"] = "authoritative_ids"
                        by_id = {p.product_id: p for p in products}
                        return [(h.score, by_id[h.product_id]) for h in hits if h.product_id in by_id]
                return await self._vector_recall(spec, adaptive=True, embedding=embedding)
            except Exception as err:
                logger.warning("Hybrid 向量侧不可用：%s", type(err).__name__)
                return None
        lexical_hits, vector_hits = await asyncio.gather(lexical(), vector())
        # 硬约束先于候选截断，BM25会扫描当前小目录的全部词项命中。
        rejected = []
        def eligible(ranking):
            result = []
            for score, product in ranking:
                reason = self._reject_reason(product, spec)
                if reason is None:
                    result.append((score, product))
                elif len(rejected) < _FILTERED_OUT_LIMIT:
                    rejected.append(self._to_rejected(product, spec, reason))
            return result
        lexical_hits = eligible(lexical_hits)[:diagnostics["candidate_limit"]]
        vector_eligible = eligible(vector_hits or [])
        # 单路故障时保留健康侧，不让零权重把降级结果清空。
        weights = self._fusion_weights if vector_hits is not None else (1.0, 0.0)
        scored = reciprocal_rank_fusion(lexical_hits, vector_eligible, weights=weights)
        stages = None
        if self._capture_retrieval_stages:
            stages = {
                "lexical_candidates": [p.product_id for _, p in lexical_hits],
                "vector_candidates": [p.product_id for _, p in vector_eligible],
                # 零权重的路不参与融合；向量故障时以实际降级权重为准。
                "merged_candidates": list(dict.fromkeys(
                    p.product_id for ranking, weight in zip((lexical_hits, vector_eligible), weights)
                    if weight > 0 for _, p in ranking)),
                "fused_candidates": [p.product_id for _, p in scored],
            }
        strategy = "hybrid_only" if vector_hits is not None else "bm25"
        rerank_applied = False
        if scored and self._reranker is not None:
            try:
                scored = await self._rerank(spec, scored)
                strategy, rerank_applied = ("hybrid_rerank" if vector_hits is not None else "bm25_rerank"), True
            except Exception as err:
                diagnostics["reranker_error"] = getattr(err, "code", type(err).__name__)
                logger.warning("Hybrid 重排不可用：%s", diagnostics["reranker_error"])
        deduped, seen = [], set()
        for score, product in scored:
            key = product.canonical_product_id or product.product_id
            if key not in seen:
                seen.add(key)
                deduped.append((score, product))
        result = {"hits": [self._to_card(score, p, spec).to_dict() for score, p in deduped[:spec.top_k]],
                "total_candidates": len(deduped), "recall_strategy": strategy, "rerank_applied": rerank_applied,
                "retrieval_variant": "bm25_vector_rrf_v1", "vector_available": vector_hits is not None,
                "filtered_out": rejected, "retrieval_diagnostics": diagnostics}
        if stages is not None:
            stages["ranked_candidates"] = [p.product_id for _, p in deduped]
            result["retrieval_stages"] = stages
        return result

    def _reject_reason(self, product: Product, spec: ProductSearchSpec, primary=None) -> Optional[str]:
        """返回硬约束拒绝原因，None 表示通过。"""
        if primary is not None and primary.stock <= 0:
            return "requested_sku_out_of_stock"
        if not product.has_available_sku():
            return "out_of_stock"
        if spec.category and product.category != spec.category:
            return "category_mismatch"
        if set(product.material_tags) & set(spec.excluded_material_tags):
            return "material_excluded"
        if set(spec.required_material_tags) - set(product.material_tags):
            return "material_required_missing"
        if spec.ship_to and spec.ship_to not in product.ships_to:
            return "ship_to_unavailable"
        if not self._within_price_cap(product, spec, primary=primary):
            return "over_price_cap"
        return None

    def _to_rejected(self, product: Product, spec: ProductSearchSpec, reason: str, primary=None) -> dict:
        primary_in_target = self._tariff.rates.convert((primary or product.primary_available_sku()).price, spec.target_currency)
        return {
            "product_id": product.product_id,
            "title": product.title,
            "category": product.category,
            "price_major": round(primary_in_target.to_major_units(), 2),
            "currency": spec.target_currency,
            "reason": reason,
        }

    def _within_price_cap(self, product: Product, spec: ProductSearchSpec, primary=None) -> bool:
        if spec.price_max_major is None:
            return True
        primary_in_target = self._tariff.rates.convert((primary or product.primary_available_sku()).price, spec.target_currency)
        return primary_in_target.to_major_units() <= spec.price_max_major

    # ---- 一阶段：向量召回 ----

    async def _vector_recall(self, spec: ProductSearchSpec, adaptive: bool = False, embedding=None) -> list[tuple[float, Product]]:
        if embedding is None:
            embedding = await self._embedder.embed(spec.normalized_query)
        top_n = max(self._recall_candidates, spec.top_k*4) if adaptive else _RECALL_TOP_N
        while True:
            vector_hits = await self._vector_index.search(embedding, top_n=top_n)
            products = await self._product_repo.find_by_ids([hit.product_id for hit in vector_hits])
            by_id = {product.product_id: product for product in products}
            scored = [(hit.score, by_id[hit.product_id]) for hit in vector_hits if hit.product_id in by_id]
            if not adaptive or len(vector_hits) < top_n or top_n >= 256 or sum(self._reject_reason(p, spec) is None for _, p in scored) >= spec.top_k:
                return scored
            top_n = min(256, top_n*2)

    # ---- 二阶段：精排 ----

    async def _rerank(
        self,
        spec: ProductSearchSpec,
        scored: list[tuple[float, Product]],
    ) -> list[tuple[float, Product]]:
        if self._reranker is None:
            raise RuntimeError("Reranker 未配置")
        documents = [product.searchable_text() for _, product in scored]
        rerank_scores = await self._reranker.rerank(spec.normalized_query, documents)
        import math
        if len(rerank_scores) != len(scored) or not all(math.isfinite(float(score)) for score in rerank_scores):
            raise ValueError("重排分数必须等长且有限")
        reranked = [
            (rerank_scores[i], product)
            for i, (_, product) in enumerate(scored)
        ]
        reranked.sort(key=lambda pair: pair[0], reverse=True)
        return reranked

    # ---- 兜底：关键词召回 ----

    async def _keyword_recall(self, spec: ProductSearchSpec) -> list[tuple[float, Product]]:
        query_terms = tokenize(spec.normalized_query)
        candidates: list[tuple[float, Product]] = []
        for product in await self._product_repo.list_all():
            score = self._keyword_score(query_terms, product, spec)
            if score > 0:
                candidates.append((score, product))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates

    @staticmethod
    def _keyword_score(query_terms: set[str], product: Product, spec: ProductSearchSpec) -> float:
        doc_terms = tokenize(product.searchable_text())
        matched = query_terms & doc_terms
        if not matched:
            return 0.0
        score = float(len(matched))
        # 品类槽位命中加权，让"槽位过滤"优于全文命中
        if spec.category and spec.category in product.category:
            score += 3.0
        return score

    # ---- 商品卡组装（含到手价内联）----

    def _to_card(self, score: float, product: Product, spec: ProductSearchSpec, primary=None) -> ProductCard:
        primary = primary or product.primary_available_sku()
        media = product_media(product.product_id, product.title)
        primary_in_target = self._tariff.rates.convert(primary.price, spec.target_currency)
        landed_price: Optional[dict] = None
        if spec.ship_to:
            try:
                quote = self._tariff.quote(
                    subtotal=primary.price,
                    category=product.category,
                    ship_to=spec.ship_to,
                    quantity=1,
                    target_currency=spec.target_currency,
                )
                landed_price = quote.to_dict()
            except ValueError as err:
                # 目的国不在规则表内：如实标注，不编造数字
                landed_price = {"unavailable_reason": str(err)}
        return ProductCard(
            product_id=product.product_id,
            title=product.title,
            brand=product.brand,
            category=product.category,
            origin_country=product.origin_country,
            # 商品卡价格必须与预算过滤使用同一目标币种；原始报价单独保留，
            # SKU 详情继续维持平台原币种，方便后续下单时选择具体规格。
            price_major=primary_in_target.to_major_units(),
            currency=spec.target_currency,
            source_price_major=primary.price.to_major_units(),
            source_currency=primary.price.currency,
            highlights=[f"{h.label}：{h.detail}" if h.detail else h.label for h in product.highlights],
            skus=[
                {
                    "sku_id": sku.sku_id,
                    "spec": sku.spec,
                    "price_major": sku.price.to_major_units(),
                    "currency": sku.price.currency,
                    "stock": sku.stock,
                }
                for sku in product.skus
            ],
            score=score,
            landed_price=landed_price,
            source_platform=product.source_platform,
            canonical_product_id=product.canonical_product_id,
            material_tags=product.material_tags,
            weight_kg=product.weight_kg,
            description=product.description,
            rating_summary=dict(product.rating_summary) if product.rating_summary is not None else None,
            ships_to=list(product.ships_to),
            dimensions_cm=dict(product.dimensions_cm),
            updated_at=product.updated_at,
            source_language=product.source_language,
            source_locale=product.source_locale,
            data_provenance=product.data_provenance,
            default_sku_id=primary.sku_id,
            image_url=media.image_url,
            image_kind=media.image_kind,
            image_alt=media.image_alt,
        )
