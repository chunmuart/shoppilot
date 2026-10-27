"""买家直接维护个人 Skill 与偏好；所有入口共用身份校验。"""
import asyncio
from dataclasses import asdict
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.domain.buyer.preference import BuyerPreference, MemoryConflict
from app.infrastructure.buyer_skills import BuyerSkillConflict
from app.presentation.identity import require_buyer


class SkillWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1,max_length=120)
    description: str = Field(min_length=1,max_length=400)
    body: str = Field(min_length=1,max_length=12000)
    expected_version: str | None = None


class PreferenceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["like","dislike"]
    statement: str = Field(min_length=1,max_length=500)
    memory_id: str | None = Field(default=None,min_length=1,max_length=128)
    expected_version: int | None = Field(default=None,ge=1)
    previous_statement: str | None = Field(default=None,min_length=1,max_length=500)


class PreferenceDelete(BaseModel):
    memory_id: str | None = Field(default=None,min_length=1,max_length=128)
    expected_version: int | None = Field(default=None,ge=1)
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1,max_length=500)


def register_buyer_workspace_routes(api, get_orchestrator):
    def stores():
        o=get_orchestrator()
        return o._sessions._main_factory.buyer_skill_store, o._preference_store

    @api.get("/commerce/my-skills")
    async def skills(request: Request,buyer_id: str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        skill_store,_=stores()
        return {"skills":await asyncio.to_thread(skill_store.list,buyer,body=True)}

    async def save(request,buyer_id,body,skill_id=None):
        buyer=await require_buyer(request,buyer_id)
        skill_store,_=stores()
        try:
            result=await asyncio.to_thread(skill_store.save,buyer,body.title,body.description,body.body,
                                          skill_id=skill_id,expected_version=body.expected_version)
            return {"skill":result}
        except BuyerSkillConflict as error:
            raise HTTPException(409,str(error)) from error
        except LookupError as error:
            raise HTTPException(404,str(error)) from error
        except ValueError as error:
            raise HTTPException(422,str(error)) from error

    @api.post("/commerce/my-skills")
    async def create_skill(body: SkillWrite,request: Request,buyer_id: str=Query(min_length=1)):
        return await save(request,buyer_id,body)

    @api.put("/commerce/my-skills/{skill_id}")
    async def update_skill(skill_id: str,body: SkillWrite,request: Request,buyer_id: str=Query(min_length=1)):
        if not body.expected_version:
            raise HTTPException(422,"编辑需要原版本")
        return await save(request,buyer_id,body,skill_id)

    @api.delete("/commerce/my-skills/{skill_id}")
    async def delete_skill(skill_id: str,request: Request,buyer_id: str=Query(min_length=1),expected_version: str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        skill_store,_=stores()
        try:
            await asyncio.to_thread(skill_store.delete,buyer,skill_id,expected_version)
            return {"deleted":True}
        except BuyerSkillConflict as error:
            raise HTTPException(409,str(error)) from error
        except LookupError as error:
            raise HTTPException(404,str(error)) from error

    @api.get("/commerce/preferences")
    async def preferences(request: Request,buyer_id: str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        _,store=stores()
        return {"preferences":[asdict(p) for p in await store.list_by_buyer(buyer)]}

    @api.post("/commerce/preferences")
    async def write_preference(body: PreferenceWrite,request: Request,buyer_id: str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        _,store=stores()
        try:
            preference=BuyerPreference(buyer,body.kind,body.statement)
            if body.memory_id is not None:
                if body.expected_version is None:raise HTTPException(422,"编辑需要原版本")
                await store.replace_by_id(buyer,body.memory_id,body.expected_version,preference)
            elif body.previous_statement is not None and getattr(store,'semantic_memory',False):
                raise HTTPException(409,"编辑需要记忆 ID 和版本，请刷新页面")
            elif body.previous_statement is None:
                result=await store.append(preference)
            elif not await store.replace(buyer,body.previous_statement,preference):
                raise HTTPException(409,"原偏好已变化，请刷新后再编辑")
            return {"saved":True,"preferences":[asdict(p) for p in await store.list_by_buyer(buyer)]}
        except MemoryConflict as error:
            raise HTTPException(409,str(error)) from error
        except ValueError as error:
            raise HTTPException(422,str(error)) from error

    @api.delete("/commerce/preferences")
    async def delete_preference(body: PreferenceDelete,request: Request,buyer_id: str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        _,store=stores()
        try:
            if getattr(store,'semantic_memory',False):
                if not body.memory_id or body.expected_version is None:raise HTTPException(409,"删除需要记忆 ID 和版本，请刷新页面")
                return {"deleted":await store.delete_by_id(buyer,body.memory_id,body.expected_version)}
            return {"deleted":await store.delete(buyer,body.statement)}
        except MemoryConflict as error:raise HTTPException(409,str(error)) from error

    @api.get("/commerce/preferences/{memory_id}/history")
    async def preference_history(memory_id: str,request: Request,buyer_id: str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        _,store=stores()
        return {"events":await store.audit(buyer,memory_id)}
