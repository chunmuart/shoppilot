# -*- coding: utf-8 -*-
"""FastAPI 服务入口

路由：
    POST /commerce/intents                 提交买家意图（同步返回最终回复；启用队列时内部入队后等结果）
    POST /commerce/intents/async           提交买家意图（立即返回 task_id，结果走 WS 或轮询）
    GET  /commerce/tasks/{task_id}         查任务状态（queued / running / done / failed）
    WS   /commerce/events                  订阅会话事件流
    GET  /commerce/orders/{order_id}       查询订单（直连 UseCase，不过 Agent）
    POST /commerce/orders/{order_id}/cancel  取消订单（直连 UseCase）
    GET  /health                           健康检查（含依赖连通性与队列深度）

启动：
    uv run uvicorn app.presentation.server:app --port 8000
    uv run python -m app.worker          # 启用队列时另起消费进程

同步接口为什么保留：13 case 评测脚本与前端都依赖它直接返回 final_text，
改成纯异步会一次性搞挂回归与前端。削峰由 worker 并发度保证，与接口形态无关。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.application.agents.orchestrator import SubmitIntentInput
from app.composition import Container, build_container
from app.domain.queue.ports.task_queue import IntentTask
from app.presentation.connection import ConnectionManager
from app.presentation.confirmations import register_confirmation_routes, confirmation_error
from app.presentation.ag_ui import register_ag_ui_routes
from app.presentation.buyer_workspace import register_buyer_workspace_routes
from app.presentation.identity import require_buyer, require_session, require_task, require_metrics_reader
from app.infrastructure.tracing import TracingASGIMiddleware, inject_task_context, install_log_correlation
from app.presentation.dto import (
    CancelOrderRequest,
    SubmitIntentRequest,
    SubmitIntentResponse,
)

install_log_correlation()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s request=%(request_id)s task=%(task_id)s session=%(session_id)s trace=%(trace_id)s prompt=%(prompt_version)s %(message)s")

logger = logging.getLogger(__name__)

# 轮数计数器存活时长：比幂等窗口长得多，让一整段会话都能被正确分类
_TURN_COUNTER_TTL_SECONDS = 86400


def build_app() -> FastAPI:
    state: dict = {}

    async def _forward_remote_events(c: Container) -> None:
        """把其他进程（worker）广播的事件转发给本进程的 WS 订阅者。"""
        if c.backplane is None:
            return
        try:
            async for event in c.backplane.listen():
                c.bus.deliver_local(event)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("事件背板监听中断：%s", err)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        c = await build_container()
        state["c"] = c
        application.state.identity_policy = getattr(c, "identity_policy", None)
        application.state.session_store = getattr(c, "session_store", None)
        application.state.session_owner_binding = getattr(getattr(c, "settings", None), "session_owner_binding", True)
        application.state.metrics_reader_buyers = getattr(getattr(c, "settings", None), "metrics_reader_buyers", ())
        state["connections"] = ConnectionManager(c.bus)
        await c.startup()
        if c.backplane is not None:
            # 跨进程事件转发：不开这个任务，worker 产生的流式事件到不了前端
            state["forwarder"] = asyncio.create_task(_forward_remote_events(c))
        try:
            yield
        finally:
            forwarder = state.pop("forwarder", None)
            if forwarder is not None:
                forwarder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await forwarder
            state.pop("c", None)
            await c.shutdown()

    api = FastAPI(title="ShopPilot 跨境电商 Agent", version="0.4.0", lifespan=lifespan)
    api.add_middleware(TracingASGIMiddleware)

    def container() -> Container:
        if "c" not in state:
            raise HTTPException(status_code=503, detail="服务尚未就绪")
        return state["c"]

    settings_origins = build_container_origins()
    api.add_middleware(
        CORSMiddleware,
        allow_origins=settings_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Trace-ID"],
    )

    # AG-UI 首版在本进程直跑；旧 intents 接口继续使用原有 Redis 队列语义。
    register_ag_ui_routes(api, lambda: container().orchestrator, lambda: container().confirmations,
                         lambda: container().ag_ui_runtime)
    from app.presentation.context_workspace import register_context_routes
    register_context_routes(api, lambda: container().context_service)
    from app.presentation.model_workspace import register_model_routes
    register_model_routes(api, container)
    from app.presentation.automations import register_automation_routes
    register_automation_routes(api, lambda: getattr(container(), 'automation_service', None))
    register_confirmation_routes(api, lambda: container().confirmations)
    register_buyer_workspace_routes(api, lambda: container().orchestrator)
    from app.presentation.product_media import register_product_media_routes
    register_product_media_routes(api)
    from app.presentation.shopping_forms import register_shopping_form_routes
    register_shopping_form_routes(api, lambda: container().orchestrator._sessions._main_factory.shopping_form_store)
    from app.presentation.favorites import register_favorite_routes
    from app.infrastructure.buyer_favorites import BuyerFavoriteStore
    register_favorite_routes(api, lambda: BuyerFavoriteStore(container().settings.data_dir / "buyer_favorites.db"))

    @api.get("/health")
    async def health() -> dict:
        """依赖连通性一并报出，避免"进程活着但存储已挂"被当成健康。"""
        c = container()
        database = "disabled"
        if c.db_engine is not None:
            try:
                async with c.db_engine.connect() as conn:
                    await conn.execute(text("select 1"))
                database = c.db_engine.url.get_backend_name()
            except Exception as err:  # noqa: BLE001
                database = f"error: {err}"
        trade_database = "disabled"
        trade_engine = getattr(c, "trade_db_engine", None)
        if trade_engine is not None:
            try:
                async with trade_engine.connect() as conn:
                    await conn.execute(text("select 1"))
                trade_database = trade_engine.url.get_backend_name()
            except Exception:
                trade_database = "error"
        redis_state = "disabled"
        if c.cache.enabled:
            redis_state = "ok" if await c.cache.ping() else "error"
        ready = not database.startswith("error") and trade_database != "error" and redis_state != "error"
        result = {
            "status": "ok" if ready else "degraded",
            "model": c.settings.llm_model,
            "runtime": getattr(c, "runtime", {}),
            "database": database,
            "trade_database": trade_database,
            "redis": redis_state,
            "semantic_cache": c.semantic_cache.enabled,
            "queue": "enabled" if c.task_queue is not None else "disabled",
            "queue_depth": await c.task_queue.depth() if c.task_queue is not None else 0,
        }
        if getattr(c, "prompt_registry", None) is not None:
            try:
                result["prompt_registry"] = await asyncio.to_thread(c.prompt_registry.describe)
            except ValueError:
                ready = False
                result["status"] = "degraded"
                result["prompt_registry"] = {"status": "unavailable"}
        return result if ready else JSONResponse(status_code=503, content=result)

    @api.get("/internal/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        await require_metrics_reader(request)
        from app.infrastructure.operational_metrics import registry
        return Response(registry.prometheus(), media_type="text/plain; version=0.0.4",
                        headers={"Cache-Control": "no-store"})

    @api.get("/internal/metrics/summary", include_in_schema=False)
    async def metrics_summary(request: Request) -> JSONResponse:
        await require_metrics_reader(request)
        from app.infrastructure.operational_metrics import registry
        return JSONResponse({"metrics": registry.snapshot(), "alerts": registry.alerts()},
                            headers={"Cache-Control": "no-store"})

    @api.post("/commerce/intents", response_model=SubmitIntentResponse)
    async def submit_intent(request: Request, body: SubmitIntentRequest) -> SubmitIntentResponse:
        c = container()
        session_id = body.shopping_session_id or f"session-{uuid.uuid4().hex[:8]}"
        body.buyer_id = await require_buyer(request, body.buyer_id)
        await require_session(request, body.buyer_id, session_id, create=True)
        intent = SubmitIntentInput(
            shopping_session_id=session_id,
            buyer_id=body.buyer_id,
            locale=body.locale,
            currency=body.currency,
            raw_query=body.raw_query,
        )
        if c.task_queue is None:
            result = await c.orchestrator.handle_intent(intent)
            return SubmitIntentResponse(
                shopping_session_id=result.shopping_session_id, final_text=result.final_text,
            )

        try:
            task_id = await _enqueue(c, intent, request_id=body.request_id)
        except ValueError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        final_text = await _await_result(c, task_id, session_id)
        return SubmitIntentResponse(shopping_session_id=session_id, final_text=final_text)

    @api.post("/commerce/intents/async")
    async def submit_intent_async(request: Request, body: SubmitIntentRequest) -> dict:
        c = container()
        session_id = body.shopping_session_id or f"session-{uuid.uuid4().hex[:8]}"
        body.buyer_id = await require_buyer(request, body.buyer_id)
        await require_session(request, body.buyer_id, session_id, create=True)
        intent = SubmitIntentInput(
            shopping_session_id=session_id,
            buyer_id=body.buyer_id,
            locale=body.locale,
            currency=body.currency,
            raw_query=body.raw_query,
        )
        if c.task_queue is None:
            raise HTTPException(status_code=503, detail="队列未启用，请使用 /commerce/intents")
        try:
            task_id = await _enqueue(c, intent, request_id=body.request_id)
        except ValueError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        status = await c.task_queue.get_status(task_id)
        return {"shopping_session_id": session_id, "task_id": task_id, "state": status.state if status else "queued"}

    @api.get("/commerce/tasks/{task_id}")
    async def get_task(request: Request, task_id: str, buyer_id: str = Query(min_length=1)) -> dict:
        c = container()
        buyer_id = await require_buyer(request, buyer_id)
        await require_task(request, buyer_id, task_id)
        if c.task_queue is None:
            raise HTTPException(status_code=503, detail="队列未启用")
        status = await c.task_queue.get_status(task_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"任务不存在或已过期：{task_id}")
        return {
            "task_id": status.task_id,
            "state": status.state,
            "final_text": status.final_text,
            "error": status.error,
            "queue_position": status.queue_position,
        }

    @api.websocket("/commerce/events")
    async def commerce_events(websocket: WebSocket) -> None:
        await state["connections"].serve(websocket)

    @api.get("/commerce/orders")
    async def list_orders(request: Request, buyer_id: str = Query(min_length=1),
                          status: str | None = Query(default=None, pattern="^(CONFIRMED|CANCELLED|DRAFT)$"),
                          offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)) -> dict:
        buyer = await require_buyer(request, buyer_id)
        return await container().trade_store.list_orders(buyer_id=buyer, status=status, offset=offset, limit=limit)

    @api.get("/commerce/orders/{order_id}")
    async def get_order(request: Request, order_id: str, buyer_id: str = Query(min_length=1)) -> dict:
        buyer_id = await require_buyer(request, buyer_id)
        try:
            return await container().query_order.execute(order_id, buyer_id=buyer_id)
        except ValueError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @api.post("/commerce/orders/{order_id}/cancel")
    async def cancel_order_endpoint(request: Request, order_id: str, body: CancelOrderRequest) -> dict:
        body.buyer_id = await require_buyer(request, body.buyer_id)
        await require_session(request, body.buyer_id, body.session_id, create=True)
        try:
            return await container().cancel_order.execute(order_id, body.reason, buyer_id=body.buyer_id, session_id=body.session_id)
        except ValueError as err:
            raise confirmation_error(err) from err

    return api


async def _queue_priority(c: Container, session_id: str) -> int:
    """按对话轮数定队列优先级（0 = 正常，1 = 大请求）。

    长会话上下文大、单次耗时长，分到低优先流，避免堵住新会话。
    轮数计数存 Redis（计数不原子，但优先级本身是启发式，差一两次无影响）；
    Redis 不可用或开关关闭时一律返回 0，退化为单队列。
    """
    if not c.settings.queue_priority_enabled or not c.cache.enabled:
        return 0
    key = f"shoppilot:turns:{session_id}"
    try:
        current = int(await c.cache.get_raw(key) or 0) + 1
        await c.cache.set_json(key, current, _TURN_COUNTER_TTL_SECONDS)
    except Exception:  # noqa: BLE001 —— 计数失败不能影响入队
        return 0
    return 1 if current >= c.settings.queue_large_request_turns else 0


async def _enqueue(c: Container, intent: SubmitIntentInput, *, request_id: str | None = None) -> str:
    """请求 ID 标识一次提交；同一句话的新提交可以形成新的任务。"""
    # task_id 从请求身份确定，Redis enqueue 原子去重；没有客户端 ID 就视为新意图。
    identity = f"{intent.buyer_id}\n{intent.shopping_session_id}\n{request_id or uuid.uuid4().hex}"
    task_id = f"task-{hashlib.sha256(identity.encode()).hexdigest()}"
    if getattr(c, "session_store", None) is not None:
        await c.session_store.bind_task_owner(task_id, intent.shopping_session_id, intent.buyer_id)
    await c.task_queue.enqueue(  # type: ignore[union-attr]
        IntentTask(
            task_id=task_id, shopping_session_id=intent.shopping_session_id,
            buyer_id=intent.buyer_id, locale=intent.locale, currency=intent.currency,
            raw_query=intent.raw_query,
            priority=await _queue_priority(c, intent.shopping_session_id),
            **inject_task_context(session_id=intent.shopping_session_id, task_id=task_id),
        ),
    )
    status = await c.task_queue.get_status(task_id)  # type: ignore[union-attr]
    if status is not None and status.state == "queued":
        c.bus.publish(intent.shopping_session_id, "task.queued", {"task_id": task_id})
    return task_id


async def _await_result(c: Container, task_id: str, session_id: str) -> str:
    """等 worker 跑完。

    优先等 final.result 事件（实时）；同时定期查任务状态兜底——
    worker 崩溃或任务进死信时事件永远不会来，只靠等事件会把请求挂死。
    """
    queue = c.bus.subscribe(session_id)
    deadline = time.monotonic() + c.settings.queue_wait_seconds
    try:
        while time.monotonic() < deadline:
            # 会话事件只负责唤醒。结果必须来自该 task_id 的已持久化终态。
            status = await c.task_queue.get_status(task_id)  # type: ignore[union-attr]
            if status is not None and status.state == "done":
                return status.final_text
            if status is not None and status.state == "failed":
                return f"[error] {status.error}"
            try:
                await asyncio.wait_for(queue.get(), timeout=min(2.0, max(0.01, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                pass
        return "[error] 处理超时，请稍后重试或改用异步接口查询任务状态"
    finally:
        c.bus.unsubscribe(session_id, queue)


def build_container_origins() -> list[str]:
    """CORS 需要在 app 构造期就确定，此处单独读一次配置。"""
    from app.infrastructure.settings import load_settings

    return load_settings().cors_origins


app = build_app()


if __name__ == "__main__":
    import uvicorn

    from app.infrastructure.settings import load_settings

    uvicorn.run(app, host="0.0.0.0", port=load_settings().port)
