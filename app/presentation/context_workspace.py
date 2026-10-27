"""买家上下文维护 API，操作独立于请求连接。"""
from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from app.presentation.identity import require_buyer, require_session, session_error
from app.domain.session.ports.session_store import SessionStoreError

class CompactRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    buyer_id: str = Field(min_length=1,max_length=128)
    session_id: str = Field(min_length=1,max_length=256)
    request_id: str = Field(min_length=1,max_length=128)
    expected_revision: int = Field(ge=0)


def register_context_routes(api, get_service):
    def service():
        value = get_service()
        if value is None or not hasattr(value.store,'context_view'):
            raise HTTPException(503,'上下文整理需要数据库会话存储')
        return value

    @api.get('/commerce/context')
    async def read_context(request:Request,buyer_id:str=Query(min_length=1),session_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        await require_session(request,buyer,session_id)
        try:return await service().view(session_id,buyer)
        except SessionStoreError as error:raise session_error(error) from error

    @api.post('/commerce/context/compact',status_code=202)
    async def compact(body:CompactRequest,request:Request):
        buyer=await require_buyer(request,body.buyer_id)
        await require_session(request,buyer,body.session_id)
        try:return await service().start(body.session_id,buyer,body.request_id,body.expected_revision)
        except SessionStoreError as error:raise session_error(error) from error

    @api.get('/commerce/context/operations/{operation_id}')
    async def operation(operation_id:str,request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        try:return await service().store.context_operation(operation_id,buyer)
        except SessionStoreError as error:raise session_error(error) from error
