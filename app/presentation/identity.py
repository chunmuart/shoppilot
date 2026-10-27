"""所有买家入口共用身份与会话归属检查；严格模式不接收 query 中的 token。"""
from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket

from app.domain.session.ports.session_store import SessionNotFound, SessionOwnerMismatch, SessionStoreError
from app.infrastructure.identity import IdentityError, IdentityPolicy, buyer_identity


def identity_policy(request: Request | WebSocket) -> IdentityPolicy:
    return getattr(request.app.state, "identity_policy", None) or IdentityPolicy()


def _token(request: Request | WebSocket) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, separator, value = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and value and " " not in value:
            return value
        return ""
    if isinstance(request, WebSocket):
        protocols = request.headers.get("sec-websocket-protocol", "").split(",")
        tokens = [item.strip()[len("shoppilot-auth."):] for item in protocols if item.strip().startswith("shoppilot-auth.")]
        if len(tokens) == 1:
            return tokens[0]
    return ""


async def require_buyer(request: Request | WebSocket, claimed_buyer: str) -> str:
    policy = identity_policy(request)
    if policy.mode == "hmac":
        try:
            verified = policy.verify(_token(request))
        except IdentityError as error:
            raise HTTPException(status_code=401, detail="需要有效的买家身份凭证", headers={"WWW-Authenticate": "Bearer"}) from error
        try:
            claimed = buyer_identity(claimed_buyer)
        except IdentityError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if verified != claimed:
            raise HTTPException(status_code=403, detail="请求买家与签名身份不一致")
        return verified
    try:
        return buyer_identity(claimed_buyer)
    except IdentityError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def session_error(error: SessionStoreError) -> HTTPException:
    code = 403 if isinstance(error, SessionOwnerMismatch) else 404 if isinstance(error, SessionNotFound) else 409
    return HTTPException(status_code=code, detail={"code": error.code, "message": str(error)})


async def require_session(request: Request | WebSocket, buyer_id: str, session_id: str, *, create: bool = False) -> None:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        # 独立演示路由可以不配置持久层；严格运行态绝不降级跳过归属检查。
        if identity_policy(request).mode == "hmac":
            raise HTTPException(status_code=503, detail="会话归属存储未就绪")
        return
    try:
        await store.assert_owner(session_id, buyer_id, create=create,
            enforce_owner=getattr(request.app.state, "session_owner_binding", True))
    except SessionStoreError as error:
        raise session_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def require_task(request: Request, buyer_id: str, task_id: str) -> None:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="任务归属存储未就绪")
    try:
        session_id = await store.assert_task_owner(task_id, buyer_id)
    except SessionStoreError as error:
        raise session_error(error) from error
    await require_session(request, buyer_id, session_id)


async def require_metrics_reader(request: Request) -> str:
    """全进程聚合指标只对服务端明确授权的签名主体开放。"""
    readers = getattr(request.app.state, "metrics_reader_buyers", ())
    if not readers:
        raise HTTPException(status_code=404, detail="运维指标接口未启用")
    policy = identity_policy(request)
    if policy.mode != "hmac":
        raise HTTPException(status_code=503, detail="运维指标要求配置严格签名身份")
    try:
        buyer_id = policy.verify(_token(request))
    except IdentityError as error:
        raise HTTPException(status_code=401, detail="需要有效的运维读取凭证", headers={"WWW-Authenticate": "Bearer"}) from error
    if buyer_id not in readers:
        raise HTTPException(status_code=403, detail="该主体没有运维指标读取权限")
    return buyer_id
