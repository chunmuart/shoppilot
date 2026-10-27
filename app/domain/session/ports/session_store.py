# -*- coding: utf-8 -*-
"""SessionStore 端口

AgentState 快照的存取抽象。四期之前 SessionRegistry 直接依赖了具体的
JsonFileSessionStore，破坏了洋葱架构的依赖方向，这里补上端口。

接口是 async 的：文件实现同步即可完成，但数据库/Redis 实现必须异步，
端口按更严格的一方定义，避免换实现时改调用方。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class SessionStoreError(ValueError):
    code = "SESSION_STORE_ERROR"


class StaleSessionWrite(SessionStoreError):
    code = "STALE_SESSION_WRITE"


class SessionOwnerMismatch(SessionStoreError):
    code = "SESSION_OWNER_MISMATCH"


class SessionOwnerUnbound(SessionStoreError):
    code = "SESSION_OWNER_UNBOUND"


class SessionNotFound(SessionStoreError):
    code = "SESSION_NOT_FOUND"


class SessionStateCorrupt(SessionStoreError):
    code = "SESSION_STATE_CORRUPT"


@dataclass(frozen=True)
class SessionClaim:
    session_id: str
    owner_id: str | None
    revision: int
    fence: int
    state_json: str | None


class SessionStore(ABC):
    @abstractmethod
    async def save(self, session_id: str, state_json: str) -> None:
        """保存 AgentState 全量快照（JSON 字符串）。"""

    @abstractmethod
    async def load(self, session_id: str) -> Optional[str]:
        """读取快照；不存在返回 None。"""

    @abstractmethod
    async def claim(self, session_id: str, *, buyer_id: str, enforce_owner: bool = True) -> SessionClaim:
        """取得新的持久 fencing epoch，同时读取最新快照；旧 epoch 立即失效。"""

    @abstractmethod
    async def save_claim(self, claim: SessionClaim, state_json: str) -> SessionClaim:
        """在一个事务内校验 owner/fence/revision 并保存；过时写入必须拒绝。"""

    @abstractmethod
    async def assert_owner(self, session_id: str, buyer_id: str, *, create: bool = False, enforce_owner: bool = True) -> None:
        """验证或初次绑定会话归属，不改变执行 fencing epoch。"""

    @abstractmethod
    async def bind_task_owner(self, task_id: str, session_id: str, buyer_id: str) -> None:
        """持久绑定队列任务主体，供跨进程查询授权。"""

    @abstractmethod
    async def assert_task_owner(self, task_id: str, buyer_id: str) -> str:
        """验证任务归属并返回会话 ID。"""
