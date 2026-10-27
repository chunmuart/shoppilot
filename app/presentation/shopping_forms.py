"""表单动作仅保存本次需求；订单与记忆继续使用各自的确认入口。"""

from datetime import datetime
from typing import Literal
from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from app.infrastructure.shopping_forms import FormConflict
from app.presentation.identity import require_buyer, require_session


class NewForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=200)


class FormAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["applyShoppingRequirements"]
    surfaceId: str
    sourceComponentId: Literal["root"]
    timestamp: datetime
    context: dict


class SubmitForm(NewForm):
    expected_revision: int = Field(strict=True, ge=1)
    request_id: str = Field(min_length=1, max_length=100)
    version: Literal["v0.9"] = "v0.9"
    action: FormAction


def register_shopping_form_routes(api, get_store):
    @api.post("/commerce/shopping-forms")
    async def create(
        body: NewForm, request: Request, buyer_id: str = Query(min_length=1)
    ):
        await require_buyer(request, buyer_id)
        raise HTTPException(410, "固定选购表单入口已停用，请通过 Agent 调用澄清工具生成本次问题")

    @api.get("/commerce/shopping-forms")
    async def latest(
        request: Request,
        session_id: str | None = None,
        buyer_id: str = Query(min_length=1),
    ):
        await require_buyer(request, buyer_id)
        if session_id is None:
            return {"form": await get_store().latest_for_buyer(buyer_id)}
        await require_session(request, buyer_id, session_id)
        return {"form": await get_store().latest(buyer_id, session_id)}

    @api.get("/commerce/shopping-forms/{form_id}")
    async def get(
        form_id: str,
        request: Request,
        session_id: str,
        buyer_id: str = Query(min_length=1),
    ):
        await require_buyer(request, buyer_id)
        await require_session(request, buyer_id, session_id)
        try:
            return await get_store().get(buyer_id, session_id, form_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error

    @api.post("/commerce/shopping-forms/{form_id}/actions")
    async def submit(
        form_id: str,
        body: SubmitForm,
        request: Request,
        buyer_id: str = Query(min_length=1),
    ):
        await require_buyer(request, buyer_id)
        await require_session(request, buyer_id, body.session_id)
        if body.action.surfaceId != form_id:
            raise HTTPException(422, "动作与表单不匹配")
        if "requirements" in body.action.context and set(body.action.context) != {
            "requirements"
        }:
            raise HTTPException(422, "动作包含多余参数")
        try:
            return await get_store().submit(
                buyer_id,
                body.session_id,
                form_id,
                body.expected_revision,
                body.request_id,
                body.action.context.get("requirements", body.action.context),
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except FormConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
