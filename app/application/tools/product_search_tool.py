# -*- coding: utf-8 -*-
"""product_search_tool

商品检索工具：结构化检索入参 → CatalogSearchUseCase → 商品卡 JSON。
MainAgent 单干与 SearchAgent 派发两条路径共用同一工具实例。
工厂模式注入 UseCase 与 EventBus，模型看到的只是工具入参与返回值结构。

注意：本模块不能用 `from __future__ import annotations`——
AgentScope 用 pydantic 从函数签名动态生成 JSON schema，字符串化注解会解析失败。
"""
import json
from typing import Optional

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.shipping.tariff_schedule import TariffSchedule
from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.budget import remember_verified_result
from app.infrastructure.persistence.context_evidence import product_decision_view


_KNOWN_CATEGORIES = (
    "旅行装备",
    "户外运动",
    "数码配件",
    "家居生活",
    "美妆个护",
    "厨房餐饮",
    "办公学习",
    "母婴宠物",
)

_CATEGORY_ALIASES = {
    "旅行装备": ("行李箱", "旅行收纳", "颈枕", "眼罩", "旅行包"),
    "户外运动": ("露营灯", "登山杖", "露营凳", "户外"),
    "数码配件": ("耳机", "耳塞", "充电器", "扩展坞", "三脚架", "数码"),
    "家居生活": ("家居", "收纳盒", "收纳架", "茶具", "香器"),
    "美妆个护": ("美妆", "个护", "护肤", "化妆"),
    "厨房餐饮": ("厨房", "餐具", "餐盒", "厨具"),
    "办公学习": ("办公", "学习", "文具", "台灯"),
    "母婴宠物": ("母婴", "宠物", "婴儿"),
}


def _normalize_category(category: Optional[str], normalized_query: str) -> Optional[str]:
    """把模型给出的叶子类目收敛到目录一级类目；无法识别时不施加错误硬过滤。"""
    for known in _KNOWN_CATEGORIES:
        if category == known or (category and known in category):
            return known
    haystacks = [value for value in (category, normalized_query) if value]
    for known, aliases in _CATEGORY_ALIASES.items():
        if any(alias in value for value in haystacks for alias in aliases):
            return known
    return next((known for known in _KNOWN_CATEGORIES if known in normalized_query), None)


def build_product_search_tool(usecase: CatalogSearchUseCase, bus: TradeEventBus, evidence_store=None, context_strategy="legacy"):
    async def product_search_tool(
        normalized_query: str = "",
        category: Optional[str] = None,
        ship_to: Optional[str] = None,
        top_k: int | str = 5,
        price_max_major: float | str | None = None,
        target_currency: str = "CNY",
        excluded_material_tags: list[str] | None = None,
        required_material_tags: list[str] | None = None,
        product_id: str | None = None,
        sku_id: str | None = None,
    ) -> ToolChunk:
        """检索跨境商品库（embedding+rerank 二阶段召回），返回 Top-K 商品卡 JSON。
        传入 ship_to 时商品卡自动内联 landed_price 到手价明细（小计+运费+关税，统一折算 target_currency），
        无需另行计算单件价格。已知商品或规格时直接传 product_id / sku_id，优先精确查询；
        两者同时传入时必须属于同一商品，未找到不能用相似商品替换。报价按一件计算。

        Args:
            normalized_query (`str`):
                标准化检索词，保留品类词与关键属性词；精确 ID 查询时可以省略。
            category (`str | None`):
                品类槽位，可选，如"旅行装备"、"数码配件"。
            ship_to (`str | None`):
                收货国家二位码，可选，如 "CN"、"US"；传入后过滤不可送达商品并内联到手价。
            top_k (`int`):
                返回候选数量，默认 5。
            price_max_major (`float | None`):
                价格上限（target_currency 主单位），买家有预算硬约束时必传，由检索链路结构化过滤。
            target_currency (`str`):
                价格口径币种，默认 "CNY"。
            excluded_material_tags (`list[str] | None`):
                材质黑名单，如买家明确不要塑料时传 ["合成聚合物"]。
            required_material_tags (`list[str] | None`):
                材质白名单，如必须是金属时传 ["金属"]。
            product_id (`str | None`):
                精确商品 ID，如 "P1003"；与 sku_id 同时提供时必须匹配。
            sku_id (`str | None`):
                精确规格 ID，如 "P1003-S1"；优先于 product_id 和查询文字，保持当前规格。
        """
        # 模型有时会把数字参数当字符串传（实测 qwen3-max 传 "300"），
        # schema 层放宽为接受数字字符串，这里统一强转后再进检索链路。
        if isinstance(top_k, str):
            try:
                top_k = int(top_k)
            except ValueError:
                return ToolChunk(
                    content=[TextBlock(type="text", text=f"[error] top_k 非法：{top_k}")],
                    state=ToolResultState.ERROR,
                )
        if isinstance(price_max_major, str):
            try:
                price_max_major = float(price_max_major)
            except ValueError:
                return ToolChunk(
                    content=[TextBlock(type="text", text=f"[error] price_max_major 非法：{price_max_major}")],
                    state=ToolResultState.ERROR,
                )
        snapshot_ctx = ShoppingContext.current()
        context_exclusions = list(snapshot_ctx.excluded_material_tags) if snapshot_ctx else []
        excluded_material_tags = list(
            dict.fromkeys([*context_exclusions, *(excluded_material_tags or [])]),
        )
        # ID 核验不能被查询文字中推测出的品类误过滤；显式品类约束仍保留。
        category = _normalize_category(category, "" if product_id or sku_id else normalized_query)
        session_id = ShoppingContext.current_session_id()
        args = {
            "normalized_query": normalized_query,
            "category": category,
            "ship_to": ship_to,
            "top_k": top_k,
            "price_max_major": price_max_major,
            "target_currency": target_currency,
            "excluded_material_tags": excluded_material_tags or [],
            "required_material_tags": required_material_tags or [],
            "product_id": product_id,
            "sku_id": sku_id,
        }
        bus.publish(session_id, "tool.invoke", {"tool": "product_search_tool", "args": args})
        if ship_to and ship_to not in TariffSchedule(ExchangeRateTable()).supported_destinations():
            error = f"暂不支持的目的国：{ship_to}"
            bus.publish(session_id, "tool.result", {"tool": "product_search_tool", "error": error})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] {error}")],
                state=ToolResultState.ERROR,
            )
        try:
            spec = ProductSearchSpec(
                normalized_query=normalized_query,
                category=category,
                ship_to=ship_to,
                top_k=top_k,
                price_max_major=price_max_major,
                target_currency=target_currency,
                excluded_material_tags=excluded_material_tags or [],
                required_material_tags=required_material_tags or [],
                product_id=product_id,
                sku_id=sku_id,
            )
            result = await usecase.execute(spec)
        except ValueError as err:
            bus.publish(session_id, "tool.result", {"tool": "product_search_tool", "error": str(err)})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] {err}")],
                state=ToolResultState.ERROR,
            )
        result["query_conditions"] = args
        from datetime import datetime, timezone
        result["observed_at"] = datetime.now(timezone.utc).isoformat()
        remember_verified_result("products", result)
        if evidence_store is not None and snapshot_ctx is not None:
            result["result_ref"] = await evidence_store.save(snapshot_ctx.buyer_id, session_id, "products", result)
        bus.publish(
            session_id,
            "tool.result",
            {
                "tool": "product_search_tool",
                "hit_count": len(result["hits"]),
                "recall_strategy": result["recall_strategy"],
                "total_candidates": result["total_candidates"],
                "rerank_applied": result["rerank_applied"],
                # 商品卡随事件下发，前端无需再调接口即可渲染（含 landed_price 到手价）
                "hits": result["hits"],
                **({"result_ref": result["result_ref"]} if "result_ref" in result else {}),
                **({"filtered_out": result["filtered_out"]} if "filtered_out" in result else {}),
            },
        )
        from app.infrastructure.context_products import business_view, product_page, token_estimate
        view = product_decision_view(result) if context_strategy == "legacy" else business_view(result)
        if context_strategy != "legacy" and token_estimate(view) > 12000:
            if not result.get("result_ref"):
                raise RuntimeError("大型商品结果未归档，不能安全分页")
            view = {**product_page(result, token_limit=12000), "query_conditions": args, "observed_at": result["observed_at"]}
        return ToolChunk(
            content=[TextBlock(type="text", text=json.dumps(view, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    return product_search_tool
