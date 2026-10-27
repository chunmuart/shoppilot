# -*- coding: utf-8 -*-
"""订单应用层测试共用真实 SQLite 交易底座，不绕过用户确认。"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.usecases.confirmation_service import ConfirmationService
from app.domain.order.address import Address
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.sql.repositories import bootstrap_schema, create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore


def test_address() -> Address:
    return Address("张三", "CN", "浙江", "杭州", "西湖区某路 1 号", "310000", "13800000000")


@pytest.fixture()
async def confirmation_env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'confirmations.db'}")
    now = [datetime.now(timezone.utc)]
    store = SqlTradeStore(engine, clock=lambda: now[0])
    products = InMemoryProductRepository()
    bus = TradeEventBus()
    evidence = ContextEvidenceStore(tmp_path / "context-evidence.db")
    service = ConfirmationService(products, store, bus, clock=lambda: now[0])
    try:
        await bootstrap_schema(engine)
        await store.initialize_inventory(await products.list_all())
        yield SimpleNamespace(engine=engine, products=products, store=store, bus=bus,
                              service=service, evidence=evidence, now=now)
    finally:
        await engine.dispose()
