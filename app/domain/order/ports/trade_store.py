"""交易确认与库存、订单的原子持久化端口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from app.domain.catalog.product import Product


class TradeStoreError(ValueError):
    """可向调用方展示的交易拒绝；code 用于稳定的接口错误处理。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TradeStore(ABC):
    @abstractmethod
    async def initialize_inventory(self, products: list[Product]) -> None:
        """新增库存种子，刷新权威价格；已有库存不得被目录种子重置。"""

    @abstractmethod
    async def get_inventory(self, sku_ids: list[str] | None = None) -> dict[str, int]: ...

    @abstractmethod
    async def prepare_confirmation(
        self, *, operation_id: str, buyer_id: str, session_id: str,
        action: Literal["create", "cancel"], payload: dict, expires_at: datetime,
    ) -> dict: ...

    @abstractmethod
    async def get_confirmation(
        self, confirmation_id: str, *, buyer_id: str, session_id: str,
    ) -> dict: ...

    @abstractmethod
    async def list_confirmations(
        self, *, buyer_id: str, session_id: str, limit: int = 20,
    ) -> list[dict]: ...

    @abstractmethod
    async def resolve_confirmation(
        self, confirmation_id: str, *, buyer_id: str, session_id: str,
        snapshot_hash: str, approved: bool,
    ) -> dict: ...

    @abstractmethod
    async def get_order(self, order_id: str, *, buyer_id: str) -> dict: ...

    async def list_orders(self, *, buyer_id: str, status: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        """仅列出当前买家订单，分页与状态过滤由存储执行。"""
        raise NotImplementedError
