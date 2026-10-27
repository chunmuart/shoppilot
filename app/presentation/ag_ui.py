# -*- coding: utf-8 -*-
"""AG-UI 首版传输：直接在 API 进程执行，提供标准 POST + SSE。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator, Callable

import anyio
from ag_ui.core import BaseEvent, RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import StreamingResponse

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.usecases.confirmation_service import ConfirmationService
from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
from app.application.agents.selected_skill import SelectedSkill, SelectedSkillError
from app.infrastructure.eventbus import observe_run_events
from app.infrastructure.ag_ui_journal import JournalConflict, JournalForbidden, JournalNotFound
from app.presentation.ag_ui_runtime import AGUIRuntime
from app.presentation.identity import require_buyer, require_session
from app.infrastructure.capability_registry import CapabilityVersionChanged
import json

logger = logging.getLogger(__name__)


def parse_intent(body: RunAgentInput) -> SubmitIntentInput:
    """只提取本轮用户文本；历史由后端 AgentState 维护，客户端 state 不能覆盖业务事实。"""
    if not body.thread_id.strip() or not body.run_id.strip():
        raise HTTPException(status_code=422, detail="threadId 和 runId 不能为空")
    confirmations=[]
    for entry in body.resume or []:
        if entry.status=="cancelled":approved=False
        elif isinstance(entry.payload,dict) and set(entry.payload)=={"approved"} and type(entry.payload["approved"]) is bool:
            approved=entry.payload["approved"]
        else:raise HTTPException(422,"确认只接受 approved 布尔值")
        confirmations.append({"interrupt_id":entry.interrupt_id,"approved":approved})
    if len(confirmations)>20:raise HTTPException(422,"单次确认过多")
    if body.tools:
        raise HTTPException(status_code=422, detail="当前接口只执行后端工具，尚未支持前端 tools")
    if not body.messages or body.messages[-1].role != "user":
        raise HTTPException(status_code=422, detail="messages 最后一项必须是本轮 user 消息")
    content = body.messages[-1].content
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="当前接口需要非空的纯文本 user 消息")
    props = body.forwarded_props if isinstance(body.forwarded_props, dict) else {}
    buyer_id = props.get("buyerId")
    if not isinstance(buyer_id, str) or not buyer_id.strip():
        raise HTTPException(status_code=422, detail="forwardedProps.buyerId 不能为空")
    try:
        selected_skill = SelectedSkill.parse(props["selectedSkill"]) if "selectedSkill" in props else None
    except SelectedSkillError as err:
        raise HTTPException(status_code=422, detail=str(err)) from None
    return SubmitIntentInput(
        shopping_session_id=body.thread_id,
        buyer_id=buyer_id,
        locale=str(props.get("locale", "zh-CN")),
        currency=str(props.get("currency", "CNY")),
        raw_query=content.strip(),
        selected_skill=selected_skill,
        confirmations=tuple(confirmations),
    )


async def stream_run(
    orchestrator: MainAgentOrchestrator,
    body: RunAgentInput,
    intent: SubmitIntentInput,
    confirmations: ConfirmationService | None = None,
) -> AsyncIterator[str]:
    """HTTP 取消传播到正在执行的 Agent；不留下后台生产任务。"""
    queue: asyncio.Queue[BaseEvent | None] = asyncio.Queue()
    adapter = AGUIRunAdapter(body, queue.put_nowait)
    encoder = EventEncoder()

    async def produce() -> None:
        adapter.start()
        try:
            if confirmations is not None:
                saved = await confirmations.list(intent.buyer_id, intent.shopping_session_id)
                adapter.state["confirmations"] = saved["confirmations"]
                adapter.snapshot()
            with observe_run_events(adapter.on_trade_event):
                # 现有语义缓存只存最终文本，无法恢复商品事实；AG-UI 首版不读写此缓存。
                result = await orchestrator.handle_intent(
                    intent, event_observer=adapter.on_agent_event, use_semantic_cache=False,
                )
            error = result.error or adapter.error
            if error:
                adapter.fail(result.error if getattr(result,"error_code",None) in {"SESSION_VERSION_CHANGED","CONTEXT_CAPACITY_EXCEEDED"} else adapter.error or "本轮服务暂时未能完成请求，请重试。", code=getattr(result,"error_code",None))
            else:
                adapter.finish(result.final_text)
        except asyncio.CancelledError:
            adapter.fail("本轮执行已中断", cancelled=True)
            logger.info("AG-UI 执行已中断：thread=%s run=%s", body.thread_id, body.run_id)
            raise
        except Exception:  # noqa: BLE001 —— 流已开始，只能用协议错误收口
            logger.exception("AG-UI 执行失败：run=%s", body.run_id)
            adapter.fail("本轮服务暂时未能完成请求，请重试。")
        finally:
            queue.put_nowait(None)

    producer = asyncio.create_task(produce(), name=f"ag-ui:{body.run_id}")
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                break
            yield encoder.encode(event)
    finally:
        # Starlette 断连会取消整个流任务组；清理期间屏蔽外层重复取消。
        with anyio.CancelScope(shield=True):
            if not producer.done():
                producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer


def register_ag_ui_routes(
    api: FastAPI, get_orchestrator: Callable[[], MainAgentOrchestrator],
    get_confirmations: Callable[[], ConfirmationService] | None = None,
    get_runtime: Callable[[], AGUIRuntime] | None = None,
) -> None:
    def runtime():
        if get_runtime is None:
            raise HTTPException(status_code=503, detail="持久运行日志尚未配置")
        return get_runtime()

    def translate(err):
        code = 403 if isinstance(err, JournalForbidden) else 404 if isinstance(err, JournalNotFound) else 409
        return HTTPException(status_code=code, detail=str(err))

    def cursor(request: Request, run_id: str, after: int):
        header = request.headers.get("last-event-id")
        if header:
            prefix, separator, value = header.rpartition(":")
            if not separator or prefix != run_id or not value.isdigit():
                raise HTTPException(status_code=409, detail="Last-Event-ID 与当前运行不匹配")
            return int(value)
        return after

    async def replay(manager, run_id, buyer_id, after):
        while True:
            events, status, last = await manager.journal.events(run_id, buyer_id, after)
            for item in events:
                after = item["seq"]
                yield f"id: {run_id}:{after}\ndata: {json.dumps(item['event'], ensure_ascii=False)}\n\n"
            if status != "running" and after >= last:
                return
            if not events:
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.15)

    def response(manager, run_id, buyer_id, after):
        return StreamingResponse(replay(manager, run_id, buyer_id, after), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "X-Run-Id": run_id})

    @api.get("/commerce/skills")
    async def available_skills(request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        try:
            orchestrator = get_orchestrator()
            if getattr(getattr(getattr(orchestrator, "_sessions", None), "_main_factory", None), "buyer_skill_store", None) is not None:
                return await orchestrator.available_skills(buyer_id)
            return await orchestrator.available_skills()
        except CapabilityVersionChanged as err:
            raise HTTPException(status_code=409, detail="选购方案刚刚更新，请刷新列表后重试。") from err
        except Exception as err:
            logger.warning("读取已发布选购方案失败：%s", type(err).__name__)
            raise HTTPException(status_code=503, detail="选购方案暂时无法读取，请稍后刷新。") from err

    @api.post("/commerce/ag-ui/run")
    async def run_agent(body: RunAgentInput, request: Request) -> StreamingResponse:
        intent = parse_intent(body)
        await require_buyer(request, intent.buyer_id)
        await require_session(request, intent.buyer_id, intent.shopping_session_id, create=True)
        if get_runtime is None:
            # 保留不含恢复契约的旧嵌入式测试/调用；正式服务必须装配持久 runtime。
            return StreamingResponse(stream_run(get_orchestrator(), body, intent,
                get_confirmations() if get_confirmations else None), media_type="text/event-stream")
        manager = runtime()
        try:
            after = cursor(request, body.run_id, 0)
            if after:
                await manager.journal.events(body.run_id, intent.buyer_id, after)
            await manager.start(body, intent)
            await manager.journal.events(body.run_id, intent.buyer_id, after)
        except (JournalConflict, JournalForbidden, JournalNotFound) as err:
            raise translate(err) from err
        return response(manager, body.run_id, intent.buyer_id, after)

    @api.get("/commerce/ag-ui/runs/{run_id}/events")
    async def events(run_id: str, request: Request, buyer_id: str = Query(min_length=1), after: int = Query(0, ge=0)):
        await require_buyer(request, buyer_id)
        manager = runtime()
        after = cursor(request, run_id, after)
        try:
            run = await manager.journal.run(run_id, buyer_id)
            await require_session(request, buyer_id, run["threadId"], create=False)
            await manager.journal.events(run_id, buyer_id, after)
        except (JournalConflict, JournalForbidden, JournalNotFound) as err:
            raise translate(err) from err
        return response(manager, run_id, buyer_id, after)

    @api.get("/commerce/ag-ui/runs/{run_id}")
    async def get_run(run_id: str, request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        try:
            run = await runtime().journal.run(run_id, buyer_id)
            await require_session(request, buyer_id, run["threadId"], create=False)
            return run
        except (JournalConflict, JournalForbidden, JournalNotFound) as err:
            raise translate(err) from err

    @api.post("/commerce/ag-ui/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        try:
            run = await runtime().journal.run(run_id, buyer_id)
            await require_session(request, buyer_id, run["threadId"], create=False)
            return await runtime().cancel(run_id, buyer_id)
        except (JournalConflict, JournalForbidden, JournalNotFound) as err:
            raise translate(err) from err

    @api.get("/commerce/ag-ui/runs/{run_id}/tools/{tool_call_id}/products")
    async def tool_products(run_id: str, tool_call_id: str, request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        try:
            journal = runtime().journal
            session_id = await journal.run_session_id(run_id, buyer_id)
            await require_session(request, buyer_id, session_id, create=False)
            return await journal.tool_products(run_id, tool_call_id, buyer_id)
        except (JournalConflict, JournalForbidden, JournalNotFound) as err:
            raise translate(err) from err

    @api.get("/commerce/ag-ui/sessions")
    async def sessions(request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        return {"sessions": await runtime().journal.sessions(buyer_id)}

    @api.get("/commerce/ag-ui/sessions/{session_id}")
    async def session(session_id: str, request: Request, buyer_id: str = Query(min_length=1)):
        await require_buyer(request, buyer_id)
        await require_session(request, buyer_id, session_id, create=False)
        try:
            return await runtime().journal.session(session_id, buyer_id)
        except (JournalConflict, JournalForbidden, JournalNotFound) as err:
            raise translate(err) from err
