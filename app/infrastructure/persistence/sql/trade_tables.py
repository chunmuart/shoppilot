"""交易台账表。订单继续复用 orders/order_items，避免产生第二套订单真相。"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.sql.tables import Base


class SkuInventoryRow(Base):
    __tablename__ = "trade_sku_inventory"

    sku_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    stock: Mapped[int] = mapped_column(Integer)
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_trade_stock_nonnegative"),
        CheckConstraint("unit_price_minor >= 0", name="ck_trade_price_nonnegative"),
    )


class TradeConfirmationRow(Base):
    __tablename__ = "trade_confirmations"

    confirmation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    resolved_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    __table_args__ = (
        Index("ix_trade_confirmation_owner", "buyer_id", "session_id", "created_at"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_trade_decision"),
    )


class TradeOperationRow(Base):
    __tablename__ = "trade_operations"

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String(32), unique=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved_at: Mapped[str] = mapped_column(String(40))
