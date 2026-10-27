# -*- coding: utf-8 -*-
"""InMemoryProductRepository / InMemoryOrderRepository

开发态内存仓储实现。ProductRepository 由种子数据初始化；
OrderRepository 提供自增单号（GBX-XXXX 前缀，便于日志排查）。
"""
from __future__ import annotations

import itertools
from typing import Awaitable, Callable, Optional

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.product import Product
from app.domain.order.order import Order
from app.domain.order.ports.order_repository import OrderRepository
from app.infrastructure.persistence.seed_products import build_seed_products


class InMemoryProductRepository(ProductRepository):
    def __init__(self, products: Optional[list[Product]] = None) -> None:
        seed = products if products is not None else build_seed_products()
        self._products: dict[str, Product] = {p.product_id: p for p in seed}
        self._inventory_reader: Callable[[list[str]], Awaitable[dict[str, int]]] | None = None

    def bind_inventory(self, reader: Callable[[list[str]], Awaitable[dict[str, int]]]) -> None:
        self._inventory_reader = reader

    async def _refresh_inventory(self, products: list[Product]) -> list[Product]:
        if self._inventory_reader is not None:
            stocks = await self._inventory_reader([sku.sku_id for p in products for sku in p.skus])
            for product in products:
                for sku in product.skus:
                    sku.stock = stocks.get(sku.sku_id, 0)
        return products

    async def find_by_id(self, product_id: str) -> Optional[Product]:
        products = await self._refresh_inventory([self._products[product_id]] if product_id in self._products else [])
        return products[0] if products else None

    async def find_by_ids(self, product_ids: list[str]) -> list[Product]:
        return await self._refresh_inventory([self._products[pid] for pid in product_ids if pid in self._products])

    async def list_all(self) -> list[Product]:
        return await self._refresh_inventory(list(self._products.values()))


class InMemoryOrderRepository(OrderRepository):
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._counter = itertools.count(1)

    async def save(self, order: Order) -> None:
        self._orders[order.order_id] = order

    async def find_by_id(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    async def next_order_id(self) -> str:
        return f"GBX-{next(self._counter):06d}"
