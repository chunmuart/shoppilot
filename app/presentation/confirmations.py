"""订单意向确认资源。决定只来自明确的 HTTP 用户动作，不接受模型代行。"""
from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.application.usecases.confirmation_service import ConfirmationService
from app.application.usecases.order_usecases import OrderItemInput
from app.domain.order.address import Address
from app.domain.order.ports.trade_store import TradeStoreError
from app.presentation.identity import require_buyer, require_session


class IdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    buyer_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)


class AddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    recipient_name: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=2, max_length=2)
    state: str = Field(default="", max_length=100)
    city: str = Field(min_length=1, max_length=100)
    address_line: str = Field(min_length=1, max_length=300)
    postal_code: str = Field(default="", max_length=30)
    phone: str = Field(default="", max_length=40)


class OrderItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    quantity: StrictInt = Field(gt=0, le=10000)


class PrepareOrderRequest(IdentityRequest):
    items: list[OrderItemRequest] = Field(min_length=1, max_length=50)
    shipping_address: AddressRequest


class ResolveConfirmationRequest(IdentityRequest):
    snapshot_hash: str = Field(min_length=1, max_length=128)
    approved: StrictBool


def confirmation_error(err: ValueError) -> HTTPException:
    code = err.code if isinstance(err, TradeStoreError) else "invalid_request"
    status = 404 if code.lower() in {"not_found", "order_not_found", "confirmation_not_found"} else 409 if isinstance(err, TradeStoreError) else 422
    return HTTPException(status_code=status, detail={"code": code, "message": str(err)})


def register_confirmation_routes(api: FastAPI, service: Callable[[], ConfirmationService]) -> None:
    @api.post("/commerce/confirmations/orders")
    async def prepare_order(request: Request, body: PrepareOrderRequest) -> dict:
        body.buyer_id = await require_buyer(request, body.buyer_id)
        await require_session(request, body.buyer_id, body.session_id, create=True)
        try:
            return await service().prepare_order(
                body.buyer_id, body.session_id,
                [OrderItemInput(**item.model_dump()) for item in body.items],
                Address(**body.shipping_address.model_dump()),
            )
        except ValueError as err:
            raise confirmation_error(err) from err

    @api.get("/commerce/confirmations")
    async def list_confirmations(request: Request, buyer_id: str = Query(min_length=1), session_id: str = Query(min_length=1)) -> dict:
        buyer_id = await require_buyer(request, buyer_id)
        await require_session(request, buyer_id, session_id)
        try:
            return await service().list(buyer_id, session_id)
        except ValueError as err:
            raise confirmation_error(err) from err

    @api.get("/commerce/confirmations/{confirmation_id}")
    async def get_confirmation(request: Request, confirmation_id: str, buyer_id: str = Query(min_length=1), session_id: str = Query(min_length=1)) -> dict:
        buyer_id = await require_buyer(request, buyer_id)
        await require_session(request, buyer_id, session_id)
        try:
            return await service().get(confirmation_id, buyer_id, session_id)
        except ValueError as err:
            raise confirmation_error(err) from err

    @api.post("/commerce/confirmations/{confirmation_id}/resolve")
    async def resolve_confirmation(request: Request, confirmation_id: str, body: ResolveConfirmationRequest) -> dict:
        body.buyer_id = await require_buyer(request, body.buyer_id)
        await require_session(request, body.buyer_id, body.session_id)
        try:
            return await service().resolve(confirmation_id, **body.model_dump())
        except ValueError as err:
            raise confirmation_error(err) from err
