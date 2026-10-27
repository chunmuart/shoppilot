# -*- coding: utf-8 -*-
"""订单用例：写工具仅准备确认，真实写入统一通过 ConfirmationService.resolve。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.order.address import Address

if TYPE_CHECKING:
    from app.application.usecases.confirmation_service import ConfirmationService
    from app.domain.order.ports.trade_store import TradeStore


@dataclass(frozen=True)
class OrderItemInput:
    product_id: str
    sku_id: str
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.product_id, str) or not self.product_id.strip():
            raise ValueError("OrderItem.product_id 不能为空")
        if not isinstance(self.sku_id, str) or not self.sku_id.strip():
            raise ValueError("OrderItem.sku_id 不能为空")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("OrderItem.quantity 必须为正整数，不能使用布尔值、小数或字符串")


class PlaceOrderUseCase:
    def __init__(self, confirmations: ConfirmationService) -> None:
        self._confirmations = confirmations

    async def execute(
        self, buyer_id: str, items: list[OrderItemInput], shipping_address: Address, *, session_id: str,
    ) -> dict:
        """只生成权威确认快照，不扣库存、不创建订单。"""
        return await self._confirmations.prepare_order(buyer_id, session_id, items, shipping_address)


class QueryOrderUseCase:
    def __init__(self, trade_store: TradeStore) -> None:
        self._trade_store = trade_store

    async def execute(self, order_id: str, *, buyer_id: str) -> dict:
        if not isinstance(buyer_id, str) or not buyer_id.strip():
            raise ValueError("查询订单必须提供买家身份")
        if not isinstance(order_id, str) or not order_id.strip():
            raise ValueError("订单号不能为空")
        return await self._trade_store.get_order(order_id, buyer_id=buyer_id)


class CancelOrderUseCase:
    def __init__(self, confirmations: ConfirmationService) -> None:
        self._confirmations = confirmations

    async def execute(self, order_id: str, reason: str, *, buyer_id: str, session_id: str) -> dict:
        """只准备取消确认；库存回补由用户确认后的单个事务执行。"""
        return await self._confirmations.prepare_cancel(buyer_id, session_id, order_id, reason)
