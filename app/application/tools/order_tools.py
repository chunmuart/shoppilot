# -*- coding: utf-8 -*-
"""订单工具集：create_order_tool / query_order_tool / cancel_order_tool

MainAgent 单干与 TradeAgent 派发两条路径共用。写工具只准备确认凭证，不执行用户决议。

注意：本模块不能用 `from __future__ import annotations`（AgentScope schema 生成依赖运行时注解）。
"""
import json

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    OrderItemInput,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.domain.order.address import Address
from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.budget import remember_verified_result


def _ok(payload: dict) -> ToolChunk:
    if payload.get("confirmation_required"):
        remember_verified_result("confirmation", {"pending": True})
    elif payload.get("order_id"):
        remember_verified_result("trade", {"orders": [payload]})
    return ToolChunk(
        content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
        state=ToolResultState.SUCCESS,
    )


def _fail(message: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(type="text", text=f"[error] {message}")],
        state=ToolResultState.ERROR,
    )


def _identity():
    snapshot = ShoppingContext.current()
    if snapshot is None or not snapshot.buyer_id or not snapshot.shopping_session_id:
        raise ValueError("订单操作缺少已绑定的买家与会话身份")
    return snapshot.buyer_id, snapshot.shopping_session_id


async def _verified_order_items(evidence_store, buyer_id: str, session_id: str, items: list[dict]) -> list[OrderItemInput]:
    """从会话原始工具证据核对来源，模型文字及当前目录不能替代检索记录。"""
    if evidence_store is None:
        raise ValueError("商品检索证据未接入，不能准备下单确认")
    verified = []
    for item in items:
        product_id, sku_id = item.get("product_id"), item.get("sku_id")
        if not isinstance(product_id, str) or not product_id.strip():
            raise ValueError("订单行必须提供 product_id")
        if sku_id is not None and (not isinstance(sku_id, str) or not sku_id.strip()):
            raise ValueError("sku_id 必须为非空文字")
        evidence = await evidence_store.find_product(buyer_id, session_id, product_id=product_id, sku_id=sku_id or "")
        if evidence is None:
            raise ValueError(f"当前会话未检索返回商品或规格：{sku_id or product_id}；请先用 product_search_tool 精确核验")
        card = next((hit for hit in evidence["data"].get("hits", []) if hit.get("product_id") == product_id), None)
        if card is None:
            raise ValueError("商品检索证据与请求不匹配")
        sku_ids = {sku["sku_id"] for sku in card.get("skus", []) if sku.get("sku_id")}
        if sku_id is None:
            # 单商品入口沿用返回卡片的默认规格，不猜测买家没有见过的 SKU。
            sku_id = card.get("default_sku_id") or (next(iter(sku_ids)) if len(sku_ids) == 1 else None)
        if sku_id not in sku_ids:
            raise ValueError(f"商品 {product_id} 缺少可核对的规格，请先检索并明确 sku_id")
        verified.append(OrderItemInput(product_id, sku_id, item.get("quantity", 1)))
    return verified


def build_create_order_tool(usecase: PlaceOrderUseCase, bus: TradeEventBus, evidence_store=None):
    async def create_order_tool(
        items: list[dict] | None = None,
        shipping_address: dict | None = None,
        product_id: str | None = None,
        sku_id: str | None = None,
        quantity: int = 1,
    ) -> ToolChunk:
        """准备下单意向的权威确认卡，返回 confirmation_required，不创建订单或扣库存。

        即使买家在对话中说“同意”，也必须等待其点击页面确认卡；模型不能代为确认。
        金额仅含所选商品，不含运费和税费，不代表付款。买家身份由系统会话上下文注入。
        商品与规格必须来自当前买家、当前会话的检索结果；否则先精确检索。
        单商品可直接传 product_id；省略 sku_id 时沿用检索卡默认规格，确认卡仍展示具体规格。

        Args:
            items (`list[dict]`):
                多商品订单行列表，每项形如 {"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}；与单商品参数互斥。
            shipping_address (`dict`):
                收货地址，形如 {"recipient_name": "...", "country": "CN", "state": "...",
                "city": "...", "address_line": "...", "postal_code": "...", "phone": "..."}。
            product_id (`str | None`):
                单商品入口，如 "P1001"；必须曾在当前会话检索返回。
            sku_id (`str | None`):
                单商品规格，缺省沿用检索卡中的默认规格，不更换商品。
            quantity (`int`):
                单商品购买数量，默认 1，必须为正整数；使用 items 时数量放在订单行内。
        """
        try:
            buyer_id, session_id = _identity()
        except ValueError as err:
            return _fail(str(err))
        bus.publish(session_id, "tool.invoke", {"tool": "create_order_tool", "args": {"buyer_id": buyer_id, "items": items}})
        try:
            if items is not None and (product_id is not None or sku_id is not None or type(quantity) is not int or quantity != 1):
                raise ValueError("items 与单商品 product_id / sku_id / quantity 参数不能混用")
            if items is None:
                items = [{"product_id": product_id, "sku_id": sku_id, "quantity": quantity}]
            if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
                raise ValueError("items 必须是非空订单行对象列表")
            if not isinstance(shipping_address, dict):
                raise ValueError("shipping_address 必须是地址对象")
            order_items = await _verified_order_items(evidence_store, buyer_id, session_id, items)
            address = Address(
                recipient_name=shipping_address.get("recipient_name", ""),
                country=shipping_address.get("country", ""),
                state=shipping_address.get("state", ""),
                city=shipping_address.get("city", ""),
                address_line=shipping_address.get("address_line", ""),
                postal_code=shipping_address.get("postal_code", ""),
                phone=shipping_address.get("phone", ""),
            )
            result = await usecase.execute(
                buyer_id=buyer_id, session_id=session_id, items=order_items, shipping_address=address,
            )
        except (ValueError, KeyError, TypeError) as err:
            bus.publish(session_id, "tool.result", {"tool": "create_order_tool", "error": str(err)})
            return _fail(str(err))
        bus.publish(session_id, "tool.result", {"tool": "create_order_tool", **result})
        return _ok(result)

    return create_order_tool


def build_query_order_tool(usecase: QueryOrderUseCase, bus: TradeEventBus):
    async def query_order_tool(order_id: str) -> ToolChunk:
        """查询当前买家的订单详情；订单号本身不构成读取权限。

        Args:
            order_id (`str`):
                订单号，如 "GBX-000001"。
        """
        try:
            buyer_id, session_id = _identity()
        except ValueError as err:
            return _fail(str(err))
        bus.publish(session_id, "tool.invoke", {"tool": "query_order_tool", "args": {"order_id": order_id}})
        try:
            snapshot = await usecase.execute(order_id, buyer_id=buyer_id)
        except ValueError as err:
            bus.publish(session_id, "tool.result", {"tool": "query_order_tool", "error": str(err)})
            return _fail(str(err))
        bus.publish(session_id, "tool.result", {"tool": "query_order_tool", "order": snapshot})
        return _ok(snapshot)

    return query_order_tool


def build_cancel_order_tool(usecase: CancelOrderUseCase, bus: TradeEventBus):
    async def cancel_order_tool(order_id: str, reason: str) -> ToolChunk:
        """准备当前买家的订单取消确认卡，不立即取消或回补库存。

        用户必须点击页面确认卡才能执行。自然语言同意不能代替该用户动作。

        Args:
            order_id (`str`):
                订单号，如 "GBX-000001"。
            reason (`str`):
                取消原因，必填。
        """
        try:
            buyer_id, session_id = _identity()
        except ValueError as err:
            return _fail(str(err))
        bus.publish(session_id, "tool.invoke", {"tool": "cancel_order_tool", "args": {"order_id": order_id, "reason": reason}})
        try:
            result = await usecase.execute(order_id, reason, buyer_id=buyer_id, session_id=session_id)
        except ValueError as err:
            bus.publish(session_id, "tool.result", {"tool": "cancel_order_tool", "error": str(err)})
            return _fail(str(err))
        bus.publish(session_id, "tool.result", {"tool": "cancel_order_tool", **result})
        return _ok(result)

    return cancel_order_tool
