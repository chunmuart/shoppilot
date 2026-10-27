"""SQLite 交易台账：确认、库存、订单和操作幂等键共用一个真实事务。

仅支持 SQLite。BEGIN IMMEDIATE 在读取确认前取得写锁，因此不同连接、
不同进程执行同一确认也只会产生一次交易。文件仓储不能提供这个保证。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import datetime, timezone
from typing import Callable, Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.order.address import Address
from app.domain.order.order import Order, OrderStatus
from app.domain.order.order_line import OrderLine
from app.domain.order.ports.trade_store import TradeStore, TradeStoreError
from app.infrastructure.persistence.sql.tables import Base, OrderLineRow, OrderRow
from app.infrastructure.persistence.sql.trade_tables import (
    SkuInventoryRow, TradeConfirmationRow, TradeOperationRow,
)

_SCOPE = {"amount_scope": "merchandise_only", "order_kind": "purchase_intent"}
_MAX_SQLITE_INT = 2**63 - 1


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TradeStoreError("INVALID_ARGUMENT", "时间必须包含时区")
    return value.astimezone(timezone.utc)


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _string(value: object, name: str, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise TradeStoreError("INVALID_ARGUMENT", f"{name} 必须为非空字符串，最长 {maximum} 字符")
    return value.strip()


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SQLITE_INT:
        raise TradeStoreError("INVALID_ARGUMENT", f"{name} 必须为不小于 {minimum} 的整数")
    return value


def _normalize_create(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise TradeStoreError("INVALID_ARGUMENT", "订单至少需要一个商品")
    address = payload.get("shipping_address")
    if not isinstance(address, dict):
        raise TradeStoreError("INVALID_ARGUMENT", "缺少完整收货地址")
    normalized_address = {}
    for field in fields(Address):
        value = address.get(field.name, "")
        if not isinstance(value, str):
            raise TradeStoreError("INVALID_ARGUMENT", "地址字段必须为字符串")
        normalized_address[field.name] = value.strip()
    try:
        Address(**normalized_address)
    except ValueError as exc:
        raise TradeStoreError("INVALID_ARGUMENT", str(exc)) from exc
    items: dict[str, dict] = {}
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise TradeStoreError("INVALID_ARGUMENT", "商品明细格式错误")
        sku_id = _string(item.get("sku_id"), "sku_id", 64)
        quantity = _integer(item.get("quantity"), "quantity", 1)
        price = _integer(item.get("unit_price_minor"), "unit_price_minor")
        currency = _string(item.get("currency"), "currency", 8)
        try:
            Money.of(price, currency)
        except ValueError as exc:
            raise TradeStoreError("INVALID_ARGUMENT", str(exc)) from exc
        line = {
            "product_id": _string(item.get("product_id"), "product_id", 64),
            "sku_id": sku_id, "title": _string(item.get("title"), "title"),
            "unit_price_minor": price, "currency": currency, "quantity": quantity,
        }
        if sku_id in items:
            if any(items[sku_id][key] != line[key] for key in line if key != "quantity"):
                raise TradeStoreError("INVALID_ARGUMENT", "相同 SKU 的价格或商品信息不一致")
            items[sku_id]["quantity"] = _integer(items[sku_id]["quantity"] + quantity, "quantity", 1)
        else:
            items[sku_id] = line
    ordered = [items[key] for key in sorted(items)]
    currencies = {line["currency"] for line in ordered}
    if len(currencies) != 1:
        raise TradeStoreError("INVALID_ARGUMENT", "一张订单的商品币种必须相同")
    total = _integer(sum(line["unit_price_minor"] * line["quantity"] for line in ordered), "total_amount_minor")
    return {
        "items": ordered, "shipping_address": normalized_address,
        "currency": ordered[0]["currency"], "total_amount_minor": total, **_SCOPE,
    }


class SqlTradeStore(TradeStore):
    def __init__(self, engine: AsyncEngine, *, clock: Callable[[], datetime] | None = None) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SqlTradeStore 当前仅支持 SQLite，不能对其他数据库声称事务兼容")
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @asynccontextmanager
    async def _transaction(self):
        async with self._sessions() as db:
            try:
                # 必须先取得数据库写锁，再检查库存、状态和幂等键。
                await db.execute(text("BEGIN IMMEDIATE"))
                yield db
                await db.commit()
            except BaseException:
                # 包含 asyncio.CancelledError；在提交之前中断不能留下半张订单。
                await db.rollback()
                raise

    async def initialize_inventory(self, products: list[Product]) -> None:
        async with self._engine.connect() as connection:
            try:
                # create_all 的 checkfirst 也必须串行，否则首次多进程启动会同时建表。
                await connection.execute(text("BEGIN IMMEDIATE"))
                await connection.run_sync(Base.metadata.create_all)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        async with self._transaction() as db:
            # 扩充目录后逐 SKU 的 SELECT / 历史占用查询会拖慢启动。
            # 在同一写锁内批量读快照，仍由一次事务提交报价更新和新增库存。
            inventory = {row.sku_id: row for row in (await db.scalars(select(SkuInventoryRow))).all()}
            occupied_by_sku = dict((await db.execute(
                select(OrderLineRow.sku_id, func.sum(OrderLineRow.quantity))
                .join(OrderRow, OrderRow.order_id == OrderLineRow.order_id)
                .where(OrderRow.status == "CONFIRMED")
                .group_by(OrderLineRow.sku_id)
            )).all())
            seen: set[str] = set()
            for product in products:
                for sku in product.skus:
                    if sku.sku_id in seen:
                        raise TradeStoreError("INVALID_ARGUMENT", "目录中存在重复 SKU")
                    seen.add(sku.sku_id)
                    seed = _integer(sku.stock, "stock")
                    Money.of(sku.price.amount_in_minor_units, sku.price.currency)
                    row = inventory.get(sku.sku_id)
                    if row is None:
                        # 旧 SQL 订单仍是同一订单真相；首次迁移不得忽略其库存占用。
                        occupied = int(occupied_by_sku.get(sku.sku_id, 0))
                        if occupied < 0 or occupied > seed:
                            raise TradeStoreError("INVENTORY_MIGRATION_REQUIRED", f"SKU {sku.sku_id} 的历史订单占用超出库存种子，请核对迁移")
                        row = SkuInventoryRow(sku_id=sku.sku_id, stock=seed - occupied)
                        db.add(row)
                    elif row.product_id != product.product_id:
                        raise TradeStoreError("INVALID_ARGUMENT", "已有 SKU 不可迁移到另一个商品")
                    # 重启或目录刷新只更新报价，绝不能重置已扣减的持久库存。
                    row.product_id = product.product_id
                    row.title = f"{product.title}（{sku.spec}）"
                    row.unit_price_minor = sku.price.amount_in_minor_units
                    row.currency = sku.price.currency
            await db.flush()

    async def get_inventory(self, sku_ids: list[str] | None = None) -> dict[str, int]:
        async with self._sessions() as db:
            query = select(SkuInventoryRow.sku_id, SkuInventoryRow.stock)
            if sku_ids is not None:
                query = query.where(SkuInventoryRow.sku_id.in_(sku_ids))
            return {sku: stock for sku, stock in (await db.execute(query)).all()}

    async def prepare_confirmation(
        self, *, operation_id: str, buyer_id: str, session_id: str,
        action: Literal["create", "cancel"], payload: dict, expires_at: datetime,
    ) -> dict:
        operation_id = _string(operation_id, "operation_id", 128)
        buyer_id = _string(buyer_id, "buyer_id", 64)
        session_id = _string(session_id, "session_id", 64)
        if action == "create":
            request = _normalize_create(payload)
        elif action == "cancel" and isinstance(payload, dict):
            request = {"order_id": _string(payload.get("order_id"), "order_id", 32),
                       "reason": _string(payload.get("reason"), "reason")}
        else:
            raise TradeStoreError("INVALID_ARGUMENT", "不支持的交易操作")
        request_hash = _hash({"action": action, "payload": request})
        expiry = _utc(expires_at)
        async with self._transaction() as db:
            existing = await db.scalar(select(TradeConfirmationRow).where(TradeConfirmationRow.operation_id == operation_id))
            if existing is not None:
                self._owner(existing, buyer_id, session_id)
                if existing.request_hash != request_hash:
                    raise TradeStoreError("OPERATION_CONFLICT", "同一操作编号不能更改商品、数量、地址或操作")
                return self._confirmation(existing)
            now = _utc(self._clock())
            if expiry <= now:
                raise TradeStoreError("CONFIRMATION_EXPIRED", "确认有效期已过，请重新生成确认")
            if action == "create":
                normalized = request
                for item in normalized["items"]:
                    inventory = await self._quoted_inventory(db, item)
                    if inventory.stock < item["quantity"]:
                        raise TradeStoreError("INSUFFICIENT_STOCK", f"SKU {item['sku_id']} 库存不足")
                    item["title"] = inventory.title
            else:
                order, lines = await self._owned_order(db, request["order_id"], buyer_id)
                if order.status != "CONFIRMED":
                    raise TradeStoreError("ORDER_CHANGED", "只有已确认且未取消的订单可以发起取消")
                normalized = self._cancel_payload(order, lines, request["reason"])
            confirmation_id = uuid.uuid4().hex
            binding = {"buyer_id": buyer_id, "session_id": session_id, "action": action,
                       "payload": normalized, "expires_at": expiry.isoformat()}
            row = TradeConfirmationRow(
                confirmation_id=confirmation_id, operation_id=operation_id,
                buyer_id=buyer_id, session_id=session_id, action=action,
                request_hash=request_hash, payload=normalized, snapshot_hash=_hash(binding),
                expires_at=expiry.isoformat(), status="pending", result=None,
                created_at=now.isoformat(), resolved_at=None,
            )
            db.add(row)
            await db.flush()
            return self._confirmation(row)

    async def get_confirmation(self, confirmation_id: str, *, buyer_id: str, session_id: str) -> dict:
        async with self._sessions() as db:
            row = await self._load_confirmation(db, confirmation_id, buyer_id, session_id)
            return self._confirmation(row)

    async def list_confirmations(self, *, buyer_id: str, session_id: str, limit: int = 20) -> list[dict]:
        _string(buyer_id, "buyer_id", 64)
        _string(session_id, "session_id", 64)
        limit = min(_integer(limit, "limit", 1), 20)
        async with self._sessions() as db:
            rows = await db.scalars(select(TradeConfirmationRow).where(
                TradeConfirmationRow.buyer_id == buyer_id, TradeConfirmationRow.session_id == session_id,
            ).order_by(TradeConfirmationRow.created_at.desc(), TradeConfirmationRow.confirmation_id.desc()).limit(limit))
            return [self._confirmation(row) for row in rows]

    async def resolve_confirmation(
        self, confirmation_id: str, *, buyer_id: str, session_id: str,
        snapshot_hash: str, approved: bool,
    ) -> dict:
        if type(approved) is not bool:
            raise TradeStoreError("INVALID_ARGUMENT", "确认决议必须为布尔值")
        async with self._transaction() as db:
            row = await self._load_confirmation(db, confirmation_id, buyer_id, session_id)
            if (not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64
                    or any(character not in "0123456789abcdef" for character in snapshot_hash)
                    or not hmac.compare_digest(row.snapshot_hash, snapshot_hash)):
                raise TradeStoreError("SNAPSHOT_MISMATCH", "确认内容已经变化，请重新查看完整确认")
            status = "approved" if approved else "rejected"
            if row.status != "pending":
                if row.status != status:
                    raise TradeStoreError("DECISION_CONFLICT", "此确认已经作出另一项决议，不能修改")
                # 幂等读取已提交结果应先于过期检查，防止网络重试变成第二笔交易。
                return self._confirmation(row)
            if datetime.fromisoformat(row.expires_at) <= _utc(self._clock()):
                raise TradeStoreError("CONFIRMATION_EXPIRED", "确认已过期，请重新生成确认")
            result = None
            if approved:
                if row.action == "create":
                    result = await self._create_order(db, row)
                else:
                    result = await self._cancel_order(db, row)
            resolved_at = _utc(self._clock()).isoformat()
            db.add(TradeOperationRow(operation_id=row.operation_id, confirmation_id=row.confirmation_id,
                approved=approved, result=result, resolved_at=resolved_at))
            row.status, row.result, row.resolved_at = status, result, resolved_at
            await db.flush()
            return self._confirmation(row)

    async def get_order(self, order_id: str, *, buyer_id: str) -> dict:
        async with self._sessions() as db:
            order, lines = await self._owned_order(db, order_id, buyer_id)
            return self._order_snapshot(order, lines)

    async def _create_order(self, db: AsyncSession, confirmation: TradeConfirmationRow) -> dict:
        payload = confirmation.payload
        for item in payload["items"]:
            await self._quoted_inventory(db, item)
            outcome = await db.execute(update(SkuInventoryRow).where(
                SkuInventoryRow.sku_id == item["sku_id"], SkuInventoryRow.product_id == item["product_id"],
                SkuInventoryRow.stock >= item["quantity"],
                SkuInventoryRow.unit_price_minor == item["unit_price_minor"],
                SkuInventoryRow.currency == item["currency"],
            ).values(stock=SkuInventoryRow.stock - item["quantity"]))
            if outcome.rowcount != 1:
                raise TradeStoreError("INSUFFICIENT_STOCK", f"SKU {item['sku_id']} 库存不足，请重新选择数量")
        now = _utc(self._clock())
        order = OrderRow(
            order_id=uuid.uuid4().hex, buyer_id=confirmation.buyer_id, status="CONFIRMED",
            currency=payload["currency"], total_amount_minor=payload["total_amount_minor"],
            shipping_address_json=payload["shipping_address"], created_at=now, confirmed_at=now,
            cancelled_at=None, cancel_reason=None,
        )
        db.add(order)
        await db.flush()
        lines = [OrderLineRow(order_id=order.order_id, **item) for item in payload["items"]]
        db.add_all(lines)
        await db.flush()
        return self._order_snapshot(order, lines)

    async def _cancel_order(self, db: AsyncSession, confirmation: TradeConfirmationRow) -> dict:
        payload = confirmation.payload
        order, lines = await self._owned_order(db, payload["order_id"], confirmation.buyer_id)
        if self._cancel_payload(order, lines, payload["reason"]) != payload:
            raise TradeStoreError("ORDER_CHANGED", "订单内容或状态已变化，请重新生成取消确认")
        changed = await db.execute(update(OrderRow).where(
            OrderRow.order_id == order.order_id, OrderRow.buyer_id == confirmation.buyer_id,
            OrderRow.status == "CONFIRMED",
        ).values(status="CANCELLED", cancelled_at=_utc(self._clock()), cancel_reason=payload["reason"]))
        if changed.rowcount != 1:
            raise TradeStoreError("ORDER_CHANGED", "订单状态已变化，无法重复取消")
        for line in lines:
            outcome = await db.execute(update(SkuInventoryRow).where(
                SkuInventoryRow.sku_id == line.sku_id, SkuInventoryRow.product_id == line.product_id,
                SkuInventoryRow.stock <= _MAX_SQLITE_INT - line.quantity,
            ).values(stock=SkuInventoryRow.stock + line.quantity))
            if outcome.rowcount != 1:
                raise TradeStoreError("INVENTORY_MIGRATION_REQUIRED", "旧订单 SKU 缺少持久库存记录，请完成迁移后再取消")
        await db.flush()
        return self._order_snapshot(order, lines)

    @staticmethod
    async def _quoted_inventory(db: AsyncSession, item: dict) -> SkuInventoryRow:
        row = await db.get(SkuInventoryRow, item["sku_id"])
        if row is None or row.product_id != item["product_id"]:
            raise TradeStoreError("NOT_FOUND", "商品 SKU 不存在或尚未初始化持久库存")
        if row.unit_price_minor != item["unit_price_minor"] or row.currency != item["currency"]:
            raise TradeStoreError("PRICE_CHANGED", "商品价格或币种已变化，请重新生成确认")
        return row

    @staticmethod
    def _owner(row: TradeConfirmationRow, buyer_id: str, session_id: str) -> None:
        if row.buyer_id != buyer_id or row.session_id != session_id:
            raise TradeStoreError("OWNER_MISMATCH", "无权访问此买家或会话的交易确认")

    async def _load_confirmation(self, db: AsyncSession, confirmation_id: str, buyer_id: str, session_id: str) -> TradeConfirmationRow:
        _string(buyer_id, "buyer_id", 64)
        _string(session_id, "session_id", 64)
        row = await db.get(TradeConfirmationRow, confirmation_id)
        if row is None:
            raise TradeStoreError("NOT_FOUND", "交易确认不存在")
        self._owner(row, buyer_id, session_id)
        return row

    async def list_orders(self, *, buyer_id: str, status: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        _string(buyer_id, "buyer_id", 64)
        if status not in (None, "CONFIRMED", "CANCELLED", "DRAFT") or offset < 0 or not 1 <= limit <= 100:
            raise TradeStoreError("INVALID_QUERY", "订单筛选或分页参数无效")
        conditions=[OrderRow.buyer_id == buyer_id]
        if status: conditions.append(OrderRow.status == status)
        async with self._sessions() as db:
            total=int(await db.scalar(select(func.count()).select_from(OrderRow).where(*conditions)) or 0)
            rows=list(await db.scalars(select(OrderRow).where(*conditions).order_by(OrderRow.created_at.desc(), OrderRow.order_id.desc()).offset(offset).limit(limit)))
            ids=[r.order_id for r in rows]
            lines=list(await db.scalars(select(OrderLineRow).where(OrderLineRow.order_id.in_(ids)).order_by(OrderLineRow.id))) if ids else []
            grouped={id:[] for id in ids}
            for line in lines:grouped[line.order_id].append(line)
            return {"orders":[self._order_snapshot(r,grouped[r.order_id]) for r in rows],"total":total,"offset":offset,"limit":limit}

    @staticmethod
    async def _owned_order(db: AsyncSession, order_id: str, buyer_id: str) -> tuple[OrderRow, list[OrderLineRow]]:
        _string(buyer_id, "buyer_id", 64)
        row = await db.get(OrderRow, order_id)
        if row is None:
            raise TradeStoreError("NOT_FOUND", "订单不存在")
        if row.buyer_id != buyer_id:
            raise TradeStoreError("OWNER_MISMATCH", "无权访问其他买家的订单")
        lines = list(await db.scalars(select(OrderLineRow).where(OrderLineRow.order_id == order_id)
            .order_by(OrderLineRow.sku_id, OrderLineRow.id)))
        if not lines or any(type(line.quantity) is not int or line.quantity <= 0 for line in lines):
            raise TradeStoreError("INVENTORY_MIGRATION_REQUIRED", "旧订单明细不合法，请核对迁移")
        return row, lines

    @staticmethod
    def _cancel_payload(order: OrderRow, lines: list[OrderLineRow], reason: str) -> dict:
        return {"order_id": order.order_id, "reason": reason, "order_status": order.status,
                "items": [{"product_id": line.product_id, "sku_id": line.sku_id, "title": line.title,
                    "unit_price_minor": line.unit_price_minor, "currency": line.currency,
                    "quantity": line.quantity} for line in lines],
                "shipping_address": order.shipping_address_json,
                "currency": order.currency, "total_amount_minor": order.total_amount_minor, **_SCOPE}

    @staticmethod
    def _order_snapshot(row: OrderRow, lines: list[OrderLineRow]) -> dict:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        order = Order(order_id=row.order_id, buyer_id=row.buyer_id, shipping_address=Address(**row.shipping_address_json),
            lines=[OrderLine(product_id=line.product_id, sku_id=line.sku_id, title=line.title,
                unit_price=Money.of(line.unit_price_minor, line.currency), quantity=line.quantity) for line in lines],
            status=OrderStatus(row.status), created_at=created_at, confirmed_at=row.confirmed_at,
            cancelled_at=row.cancelled_at, cancel_reason=row.cancel_reason)
        return {**order.snapshot(), "total_amount_minor": row.total_amount_minor, **_SCOPE}

    def _confirmation(self, row: TradeConfirmationRow) -> dict:
        return {"confirmation_id": row.confirmation_id, "operation_id": row.operation_id,
                "buyer_id": row.buyer_id, "session_id": row.session_id, "action": row.action,
                "payload": row.payload, "snapshot_hash": row.snapshot_hash, "expires_at": row.expires_at,
                "status": row.status, "result": row.result, "created_at": row.created_at,
                "resolved_at": row.resolved_at,
                "expired": row.status == "pending" and datetime.fromisoformat(row.expires_at) <= _utc(self._clock())}
