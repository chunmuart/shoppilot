# -*- coding: utf-8 -*-
"""会话事件订阅：先核验签名主体和持久 owner，再接收对应会话的事件。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
import anyio

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from app.infrastructure.eventbus import TradeEventBus
from app.presentation.identity import require_buyer, require_session

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self, bus: TradeEventBus) -> None:
        self._bus = bus

    async def serve(self, websocket: WebSocket) -> None:
        protocols = websocket.scope.get("subprotocols", [])
        # 只回显公开协议名，认证令牌不进入响应头、URL 或事件正文。
        await websocket.accept(subprotocol="shoppilot-events" if "shoppilot-events" in protocols else None)
        try:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=422, detail="订阅需要身份与会话")
            buyer_id = await require_buyer(websocket, payload.get("buyer_id"))
            session_id = payload.get("shopping_session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise HTTPException(status_code=422, detail="缺少 shopping_session_id")
            await require_session(websocket, buyer_id, session_id, create=True)
        except WebSocketDisconnect:
            return
        except (HTTPException, ValueError) as error:
            status = error.status_code if isinstance(error, HTTPException) else 422
            await websocket.close(code={401: 4401, 403: 4403}.get(status, 4400), reason="会话订阅身份校验失败")
            return

        queue = self._bus.subscribe(session_id)

        async def watch_disconnect():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        disconnect = asyncio.create_task(watch_disconnect())
        delivery = None
        try:
            while True:
                delivery = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({disconnect, delivery}, return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    return
                payload = asdict(delivery.result())
                payload.pop("shopping_session_id", None)
                await websocket.send_json(payload)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            self._bus.unsubscribe(session_id, queue)
            pending = [task for task in (delivery, disconnect) if task is not None]
            for task in pending:
                task.cancel()
            # ASGI 关闭连接可能使用持续取消的 CancelScope，清理期间需屏蔽它。
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*pending, return_exceptions=True)
