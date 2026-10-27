"""买家自动化接口。状态变化用 revision 与请求幂等键保护。"""

from __future__ import annotations
import time
from typing import Literal
from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from app.domain.automation.models import AutomationDefinition, Schedule
from app.infrastructure.automation_store import AutomationConflict, AutomationNotFound
from app.infrastructure.model_registry import ModelConfigurationError
from app.application.automation import TEMPLATES
from app.presentation.identity import require_buyer


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_id: str
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_revision: int = Field(ge=1)


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_id: str
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_revision: int | None = None
    definition: AutomationDefinition


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_id: str
    schedule: Schedule


class TaskAction(Command):
    action: Literal["pause", "resume", "archive"]


class RunAction(Command):
    action: Literal["start", "stop", "retry"]


def register_automation_routes(api, get_service):
    def service():
        result = get_service()
        if result is None:
            raise HTTPException(503, "自动化服务尚未配置")
        return result

    async def call(operation):
        try:
            return await operation
        except AutomationNotFound as error:
            raise HTTPException(404, str(error)) from None
        except AutomationConflict as error:
            raise HTTPException(409, str(error)) from None
        except ModelConfigurationError as error:
            raise HTTPException(422, str(error)) from None

    @api.get("/commerce/automations/templates")
    async def templates(request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        return {"items": TEMPLATES}

    @api.get("/commerce/automations/options")
    async def options(request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        s = service()
        catalog = await call(s.registry.catalog(buyer))
        skills = await call(s.runtime.orchestrator.available_skills(buyer))
        return {"models": catalog["models"], "skills": skills["skills"]}

    @api.get("/commerce/automations/session-origins")
    async def session_origins(request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        return {"session_ids": await call(service().store.session_origins(buyer))}

    @api.post("/commerce/automations/preview")
    async def preview(body: Preview, request: Request):
        await require_buyer(request, body.buyer_id)
        return {
            "times": body.schedule.preview(time.time()),
            "timezone": body.schedule.timezone,
        }

    @api.get("/commerce/automations")
    async def listing(
        request: Request,
        buyer_id: str = Query(min_length=1),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        buyer = await require_buyer(request, buyer_id)
        return await call(service().store.list(buyer, limit, offset))

    @api.post("/commerce/automations")
    async def create(body: Save, request: Request):
        buyer = await require_buyer(request, body.buyer_id)
        return await call(
            service().save(
                buyer, body.definition.model_dump(mode="json"), body.request_id
            )
        )

    @api.patch("/commerce/automations/{identity}")
    async def update(identity: str, body: Save, request: Request):
        buyer = await require_buyer(request, body.buyer_id)
        if body.expected_revision is None:
            raise HTTPException(422, "编辑需要 expected_revision")
        await call(service().store.get(identity, buyer))
        return await call(
            service().save(
                buyer,
                body.definition.model_dump(mode="json"),
                body.request_id,
                identity=identity,
                expected_revision=body.expected_revision,
            )
        )

    @api.post("/commerce/automations/{identity}/actions")
    async def task_action(identity: str, body: TaskAction, request: Request):
        buyer = await require_buyer(request, body.buyer_id)
        return await call(
            service().store.action(
                identity, buyer, body.action, body.expected_revision, body.request_id
            )
        )

    @api.post("/commerce/automations/{identity}/runs")
    async def manual(identity: str, body: Command, request: Request):
        buyer = await require_buyer(request, body.buyer_id)
        return await call(
            service().store.manual(
                identity, buyer, body.request_id, body.expected_revision
            )
        )

    @api.get("/commerce/automation-runs")
    async def runs(
        request: Request,
        buyer_id: str = Query(min_length=1),
        automation_id: str | None = None,
        status: str | None = None,
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        buyer = await require_buyer(request, buyer_id)
        return await call(
            service().store.runs(
                buyer, identity=automation_id, status=status, limit=limit, offset=offset
            )
        )

    @api.get("/commerce/automation-runs/{identity}")
    async def run(identity: str, request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        return await call(service().store.run(identity, buyer))

    @api.post("/commerce/automation-runs/{identity}/actions")
    async def run_action(identity: str, body: RunAction, request: Request):
        buyer = await require_buyer(request, body.buyer_id)
        return await call(
            service().store.run_action(
                identity, buyer, body.action, body.expected_revision, body.request_id
            )
        )
