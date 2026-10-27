"""交易底座契约测试：使用真实文件 SQLite 和独立引擎，禁止外部服务。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select, update

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.domain.order.address import Address
from app.domain.order.order import Order
from app.domain.order.order_line import OrderLine
from app.domain.order.ports.trade_store import TradeStoreError
from app.infrastructure.persistence.sql.repositories import SqlOrderRepository, create_engine
from app.infrastructure.persistence.sql.tables import OrderLineRow, OrderRow
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.sql.trade_tables import TradeConfirmationRow, TradeOperationRow

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
ADDRESS = {"recipient_name": "测试买家", "country": "CN", "state": "上海", "city": "上海",
           "address_line": "测试路 1 号", "postal_code": "200000", "phone": "13800000000"}


def product(*, stock=5, price=12900, currency="CNY", sku_id="sku-1", product_id="p-1"):
    return Product(product_id=product_id, title="测试商品", brand="test", category="test",
        origin_country="CN", description="测试用", skus=[Sku(sku_id, "标准款", Money.of(price, currency), stock)])


def payload(*, quantity=2, price=12900, currency="CNY", sku_id="sku-1", product_id="p-1"):
    return {"items": [{"product_id": product_id, "sku_id": sku_id, "title": "测试商品",
        "unit_price_minor": price, "currency": currency, "quantity": quantity}],
        "shipping_address": dict(ADDRESS)}


@pytest.fixture
async def stores(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'trade.db'}"
    engine1, engine2 = create_engine(url), create_engine(url)
    first, second = SqlTradeStore(engine1, clock=lambda: NOW), SqlTradeStore(engine2, clock=lambda: NOW)
    await first.initialize_inventory([product()])
    yield first, second, engine1, engine2
    await engine1.dispose()
    await engine2.dispose()


async def prepare(store, *, operation_id="operation-1", buyer_id="buyer-1", session_id="session-1",
                  body=None, action="create", expiry=None):
    return await store.prepare_confirmation(operation_id=operation_id, buyer_id=buyer_id, session_id=session_id,
        action=action, payload=body or payload(), expires_at=expiry or NOW + timedelta(minutes=5))


async def resolve(store, confirmation, *, approved=True, buyer_id="buyer-1", session_id="session-1", hash_value=None):
    return await store.resolve_confirmation(confirmation["confirmation_id"], buyer_id=buyer_id, session_id=session_id,
        snapshot_hash=hash_value or confirmation["snapshot_hash"], approved=approved)


async def counts(engine):
    async with engine.connect() as db:
        return {table.__tablename__: await db.scalar(select(func.count()).select_from(table))
                for table in [OrderRow, OrderLineRow, TradeOperationRow]}


async def assert_error(code, operation):
    with pytest.raises(TradeStoreError) as caught:
        await operation
    assert caught.value.code == code


async def test_create_commits_order_inventory_and_decision_together(stores):
    store, other, engine, _ = stores
    confirmation = await prepare(store)
    assert confirmation["status"] == "pending"
    assert confirmation["payload"]["total_amount_minor"] == 25800
    assert confirmation["payload"]["amount_scope"] == "merchandise_only"
    assert await other.get_inventory() == {"sku-1": 5}
    result = await resolve(store, confirmation)
    assert result["status"] == "approved"
    assert result["result"]["status"] == "CONFIRMED"
    assert len(result["result"]["order_id"]) == 32
    assert result["result"]["order_kind"] == "purchase_intent"
    assert await other.get_inventory() == {"sku-1": 3}
    assert await other.get_order(result["result"]["order_id"], buyer_id="buyer-1") == result["result"]
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 1}


async def test_prepare_replay_returns_existing_even_with_new_expiry(stores):
    first, second, _, _ = stores
    original = await prepare(first)
    replay = await prepare(second, expiry=NOW + timedelta(minutes=10))
    assert replay == original
    await resolve(first, original)
    assert (await prepare(second))["status"] == "approved"


@pytest.mark.parametrize("mutate", [
    lambda body: body["items"][0].update(quantity=3),
    lambda body: body["items"][0].update(unit_price_minor=100),
    lambda body: body["items"][0].update(currency="USD"),
    lambda body: body["shipping_address"].update(address_line="另外一条路"),
])
async def test_operation_id_cannot_change_bound_content(stores, mutate):
    store = stores[0]
    await prepare(store)
    changed = payload()
    mutate(changed)
    await assert_error("OPERATION_CONFLICT", prepare(store, body=changed))


@pytest.mark.parametrize("buyer_id,session_id", [("buyer-2", "session-1"), ("buyer-1", "session-2")])
async def test_confirmation_owner_is_enforced_everywhere(stores, buyer_id, session_id):
    store = stores[0]
    confirmation = await prepare(store)
    await assert_error("OWNER_MISMATCH", prepare(store, buyer_id=buyer_id, session_id=session_id))
    await assert_error("OWNER_MISMATCH", resolve(store, confirmation, buyer_id=buyer_id, session_id=session_id))
    await assert_error("OWNER_MISMATCH", store.get_confirmation(confirmation["confirmation_id"], buyer_id=buyer_id, session_id=session_id))
    assert await store.list_confirmations(buyer_id=buyer_id, session_id=session_id) == []


async def test_hash_and_decision_replay_are_enforced(stores):
    store = stores[0]
    confirmation = await prepare(store)
    await assert_error("SNAPSHOT_MISMATCH", resolve(store, confirmation, hash_value="0" * 64))
    rejected = await resolve(store, confirmation, approved=False)
    assert rejected["status"] == "rejected" and rejected["result"] is None
    assert await resolve(stores[1], confirmation, approved=False) == rejected
    await assert_error("DECISION_CONFLICT", resolve(store, confirmation, approved=True))
    assert await store.get_inventory() == {"sku-1": 5}
    assert await counts(stores[2]) == {"orders": 0, "order_items": 0, "trade_operations": 1}


async def test_pending_expires_but_committed_replay_remains_valid(stores):
    store = stores[0]
    confirmation = await prepare(store)
    pending = await prepare(store, operation_id="pending")
    committed = await resolve(store, confirmation)
    future = SqlTradeStore(stores[3], clock=lambda: NOW + timedelta(days=1))
    await assert_error("CONFIRMATION_EXPIRED", resolve(future, pending))
    await assert_error("CONFIRMATION_EXPIRED", resolve(future, pending, approved=False))
    assert (await future.get_confirmation(pending["confirmation_id"], buyer_id="buyer-1", session_id="session-1"))["expired"]
    assert await resolve(future, confirmation) == committed


@pytest.mark.parametrize("quantity", [True, False, 0, -1, 1.0, "1", None])
async def test_storage_rejects_invalid_quantities(stores, quantity):
    await assert_error("INVALID_ARGUMENT", prepare(stores[0], body=payload(quantity=quantity)))
    assert await counts(stores[2]) == {"orders": 0, "order_items": 0, "trade_operations": 0}


async def test_duplicate_sku_is_aggregated_before_hash_and_stock(stores):
    body = payload(quantity=1)
    body["items"].append(dict(body["items"][0]))
    confirmation = await prepare(stores[0], body=body)
    assert len(confirmation["payload"]["items"]) == 1
    assert confirmation["payload"]["items"][0]["quantity"] == 2
    replay = await prepare(stores[1], body=payload(quantity=2))
    assert replay == confirmation


@pytest.mark.parametrize("price,currency", [(9900, "CNY"), (12900, "USD")])
async def test_authoritative_price_or_currency_change_invalidates_pending_quote(stores, price, currency):
    confirmation = await prepare(stores[0])
    await stores[1].initialize_inventory([product(price=price, currency=currency)])
    await assert_error("PRICE_CHANGED", resolve(stores[0], confirmation))
    assert await stores[0].get_inventory() == {"sku-1": 5}
    assert await counts(stores[2]) == {"orders": 0, "order_items": 0, "trade_operations": 0}


async def test_independent_connections_replay_one_decision_once(stores):
    first, second, engine, _ = stores
    confirmation = await prepare(first)
    results = await asyncio.gather(resolve(first, confirmation), resolve(second, confirmation))
    assert results[0] == results[1]
    assert await first.get_inventory() == {"sku-1": 3}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 1}


async def test_independent_operations_race_for_last_stock(stores):
    first, second, engine, _ = stores
    c1 = await prepare(first, operation_id="a", body=payload(quantity=4))
    c2 = await prepare(second, operation_id="b", body=payload(quantity=4))
    results = await asyncio.gather(resolve(first, c1), resolve(second, c2), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, TradeStoreError)]
    assert len(errors) == 1 and errors[0].code == "INSUFFICIENT_STOCK"
    assert await second.get_inventory() == {"sku-1": 1}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 1}


async def test_restart_does_not_reset_stock_and_can_replay_decision(stores):
    first, _, engine, _ = stores
    confirmation = await prepare(first)
    result = await resolve(first, confirmation)
    url = str(engine.url)
    await engine.dispose()
    restarted_engine = create_engine(url)
    restarted = SqlTradeStore(restarted_engine, clock=lambda: NOW)
    try:
        await restarted.initialize_inventory([product(stock=999)])
        assert await restarted.get_inventory() == {"sku-1": 3}
        assert await resolve(restarted, confirmation) == result
    finally:
        await restarted_engine.dispose()


async def test_first_startup_with_independent_engines_serializes_schema_and_seed(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'brand-new-trade.db'}"
    engines = [create_engine(url), create_engine(url)]
    stores = [SqlTradeStore(engine, clock=lambda: NOW) for engine in engines]
    try:
        await asyncio.gather(*(store.initialize_inventory([product()]) for store in stores))
        assert await stores[0].get_inventory() == {"sku-1": 5}
        confirmation = await prepare(stores[0])
        await resolve(stores[1], confirmation)
        await asyncio.gather(*(store.initialize_inventory([product()]) for store in stores))
        assert await stores[1].get_inventory() == {"sku-1": 3}
        assert await counts(engines[0]) == {"orders": 1, "order_items": 1, "trade_operations": 1}
    finally:
        for engine in engines:
            await engine.dispose()


async def test_cancel_is_atomic_and_repeated_resolution_only_restores_once(stores):
    first, second, engine, _ = stores
    placed = await resolve(first, await prepare(first))
    order_id = placed["result"]["order_id"]
    cancellation = await prepare(first, operation_id="cancel", action="cancel", body={"order_id": order_id, "reason": "不需要了"})
    assert cancellation["payload"]["total_amount_minor"] == 25800
    assert cancellation["payload"]["shipping_address"] == ADDRESS
    results = await asyncio.gather(resolve(first, cancellation), resolve(second, cancellation))
    assert results[0] == results[1]
    assert results[0]["result"]["status"] == "CANCELLED"
    assert await second.get_inventory() == {"sku-1": 5}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 2}
    assert (await second.get_order(order_id, buyer_id="buyer-1"))["status"] == "CANCELLED"


async def test_two_cancel_confirmations_cannot_restore_stock_twice(stores):
    first, second, _, _ = stores
    placed = await resolve(first, await prepare(first))
    body = {"order_id": placed["result"]["order_id"], "reason": "测试取消"}
    one = await prepare(first, operation_id="cancel-a", action="cancel", body=body)
    two = await prepare(second, operation_id="cancel-b", action="cancel", body=body)
    await resolve(first, one)
    await assert_error("ORDER_CHANGED", resolve(second, two))
    assert await second.get_inventory() == {"sku-1": 5}
    assert (await second.get_confirmation(two["confirmation_id"], buyer_id="buyer-1", session_id="session-1"))["status"] == "pending"


async def test_cancel_checks_owner_and_order_mutation(stores):
    first, _, engine, _ = stores
    placed = await resolve(first, await prepare(first))
    order_id = placed["result"]["order_id"]
    body = {"order_id": order_id, "reason": "测试取消"}
    await assert_error("OWNER_MISMATCH", first.get_order(order_id, buyer_id="buyer-2"))
    await assert_error("OWNER_MISMATCH", prepare(first, operation_id="wrong", buyer_id="buyer-2", action="cancel", body=body))
    cancel = await prepare(first, operation_id="cancel", action="cancel", body=body)
    async with engine.begin() as db:
        await db.execute(update(OrderRow).where(OrderRow.order_id == order_id).values(shipping_address_json={**ADDRESS, "city": "北京"}))
    await assert_error("ORDER_CHANGED", resolve(first, cancel))
    assert await first.get_inventory() == {"sku-1": 3}


@pytest.mark.parametrize("failed_sql", ["INSERT INTO orders", "INSERT INTO order_items", "INSERT INTO trade_operations", "UPDATE trade_confirmations"])
async def test_save_failures_roll_back_all_debits_and_rows(stores, failed_sql):
    first, second, engine, _ = stores
    confirmation = await prepare(first)

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(failed_sql):
            raise RuntimeError("模拟持久化失败")

    event.listen(engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="模拟持久化失败"):
            await resolve(first, confirmation)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail)
    assert await second.get_inventory() == {"sku-1": 5}
    assert await counts(engine) == {"orders": 0, "order_items": 0, "trade_operations": 0}
    assert (await second.get_confirmation(confirmation["confirmation_id"], buyer_id="buyer-1", session_id="session-1"))["status"] == "pending"
    assert (await resolve(second, confirmation))["status"] == "approved"


async def test_commit_failure_rolls_back_whole_transaction(stores):
    first, second, engine, _ = stores
    confirmation = await prepare(first)

    def fail(_connection):
        raise RuntimeError("模拟数据库提交失败")

    event.listen(engine.sync_engine, "commit", fail)
    try:
        with pytest.raises(RuntimeError, match="模拟数据库提交失败"):
            await resolve(first, confirmation)
    finally:
        event.remove(engine.sync_engine, "commit", fail)
    assert await second.get_inventory() == {"sku-1": 5}
    assert await counts(engine) == {"orders": 0, "order_items": 0, "trade_operations": 0}
    assert (await second.get_confirmation(confirmation["confirmation_id"], buyer_id="buyer-1", session_id="session-1"))["status"] == "pending"
    assert (await resolve(second, confirmation))["status"] == "approved"


async def test_later_sku_shortage_rolls_back_earlier_sku_debit(stores):
    first, second, engine, _ = stores
    await first.initialize_inventory([product(sku_id="sku-2", product_id="p-2")])
    body = payload()
    body["items"].extend(payload(sku_id="sku-2", product_id="p-2", quantity=4)["items"])
    multi = await prepare(first, body=body)
    other = await prepare(second, operation_id="other", body=payload(sku_id="sku-2", product_id="p-2", quantity=4))
    await resolve(second, other)
    await assert_error("INSUFFICIENT_STOCK", resolve(first, multi))
    assert await second.get_inventory() == {"sku-1": 5, "sku-2": 1}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 1}


async def test_task_cancellation_before_commit_rolls_back_and_releases_database_lock(stores):
    first, second, engine, _ = stores
    confirmation = await prepare(first)
    wrote = asyncio.Event()

    class InterruptedStore(SqlTradeStore):
        async def _create_order(self, db, confirmation):
            result = await super()._create_order(db, confirmation)
            wrote.set()
            await asyncio.Future()
            return result

    task = asyncio.create_task(resolve(InterruptedStore(engine, clock=lambda: NOW), confirmation))
    await asyncio.wait_for(wrote.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await second.get_inventory() == {"sku-1": 5}
    assert await counts(engine) == {"orders": 0, "order_items": 0, "trade_operations": 0}
    assert (await asyncio.wait_for(resolve(second, confirmation), 3))["status"] == "approved"


async def test_cancel_save_failure_rolls_back_status_and_stock_restore(stores):
    first, second, engine, _ = stores
    placed = await resolve(first, await prepare(first))
    order_id = placed["result"]["order_id"]
    confirmation = await prepare(first, operation_id="cancel", action="cancel", body={"order_id": order_id, "reason": "测试"})

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO trade_operations"):
            raise RuntimeError("模拟取消保存失败")

    event.listen(engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            await resolve(first, confirmation)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail)
    assert await second.get_inventory() == {"sku-1": 3}
    assert (await second.get_order(order_id, buyer_id="buyer-1"))["status"] == "CONFIRMED"


async def test_first_inventory_migration_accounts_for_existing_sql_orders(stores):
    first, second, engine, _ = stores
    legacy = Order.place("GBX-000001", "buyer-1", Address(**ADDRESS), [
        OrderLine("legacy-p", "legacy-sku", "旧商品", Money.of(100, "CNY"), 2)])
    await SqlOrderRepository(engine).save(legacy)
    await first.initialize_inventory([product(stock=5, price=100, sku_id="legacy-sku", product_id="legacy-p")])
    assert (await second.get_inventory())["legacy-sku"] == 3
    confirmation = await prepare(first, operation_id="legacy-cancel", action="cancel", body={"order_id": legacy.order_id, "reason": "测试迁移"})
    await resolve(second, confirmation)
    assert (await first.get_inventory())["legacy-sku"] == 5


async def test_migration_does_not_silently_ignore_oversold_legacy_orders(stores):
    first, _, engine, _ = stores
    legacy = Order.place("GBX-000001", "buyer-1", Address(**ADDRESS), [
        OrderLine("legacy-p", "legacy-sku", "旧商品", Money.of(100, "CNY"), 6)])
    await SqlOrderRepository(engine).save(legacy)
    await assert_error("INVENTORY_MIGRATION_REQUIRED", first.initialize_inventory([
        product(stock=5, price=100, sku_id="legacy-sku", product_id="legacy-p")]))
    assert "legacy-sku" not in await first.get_inventory()


async def test_list_confirmations_returns_newest_twenty_for_exact_owner(stores):
    first = stores[0]
    for index in range(22):
        dated = SqlTradeStore(stores[2], clock=lambda index=index: NOW + timedelta(seconds=index))
        await prepare(dated, operation_id=f"op-{index}")
    await prepare(first, operation_id="other-buyer", buyer_id="buyer-2")
    await prepare(first, operation_id="other-session", session_id="session-2")
    found = await first.list_confirmations(buyer_id="buyer-1", session_id="session-1", limit=999)
    assert len(found) == 20
    assert found[0]["operation_id"] == "op-21"
    assert found[-1]["operation_id"] == "op-2"
    assert len(await first.list_confirmations(buyer_id="buyer-1", session_id="session-1", limit=3)) == 3

async def test_order_list_filters_buyer_status_and_pagination(stores):
    store=stores[0]
    a=await prepare(store);await resolve(store,a)
    b=await prepare(store,operation_id='op-b',buyer_id='buyer-2',session_id='s2')
    await resolve(store,b,buyer_id='buyer-2',session_id='s2')
    result=await store.list_orders(buyer_id='buyer-1',status='CONFIRMED',limit=1)
    assert result['total']==1 and len(result['orders'])==1
    assert result['orders'][0]['buyer_id']=='buyer-1'
    assert (await store.list_orders(buyer_id='buyer-1',offset=1))['orders']==[]
    assert (await store.list_orders(buyer_id='buyer-1',status='CANCELLED'))['total']==0
    assert (await store.list_orders(buyer_id='nobody'))['total']==0


async def test_large_catalog_refresh_avoids_per_sku_reads_and_preserves_sales(stores):
    first, second, engine, _ = stores
    confirmation = await prepare(first)
    await resolve(first, confirmation)
    reads = []

    def count_reads(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            reads.append(statement)

    event.listen(engine.sync_engine, 'before_cursor_execute', count_reads)
    try:
        expanded = [product(stock=999)] + [product(sku_id=f'new-{i}',product_id=f'new-p-{i}') for i in range(1000)]
        await first.initialize_inventory(expanded)
        # 防止目录扩充后重新退回每个 SKU 都访问数据库的启动路径。
        assert len(reads) < 10
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', count_reads)
    inventory = await second.get_inventory()
    assert len(inventory) == 1001
    assert inventory['sku-1'] == 3
    assert all(inventory[f'new-{i}'] == 5 for i in range(1000))
    assert (await resolve(second, confirmation))['result']['order_id']
    assert (await first.get_inventory())['sku-1'] == 3
