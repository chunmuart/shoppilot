"""会话快照的持久 epoch/revision：SQL 事务阻止旧执行者延迟写回。

采用附属表迁移，既有 agent_session_states 不需要 ALTER 或清空。
Redis 租约负责执行互斥；本地数据库 epoch 负责最终保存时的 fencing。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import Integer, String, Text, Float, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.session.ports.session_store import (
    SessionClaim, SessionNotFound, SessionOwnerMismatch, SessionOwnerUnbound,
    SessionStateCorrupt, SessionStore, StaleSessionWrite,
)
from app.infrastructure.persistence.sql.tables import AgentSessionStateRow, Base, ConversationSessionRow


class SessionWriteClaimRow(Base):
    __tablename__ = "session_write_claims"

    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)


class TaskAccessBindingRow(Base):
    __tablename__ = "task_access_bindings"

    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256))
    buyer_id: Mapped[str] = mapped_column(String(128))


class SessionModelBindingRow(Base):
    __tablename__ = "session_model_bindings"
    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    active_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionModelRunRow(Base):
    __tablename__ = "session_model_runs"
    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    fence: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_json: Mapped[str] = mapped_column(Text)


class ContextCheckpointRow(Base):
    __tablename__ = "context_checkpoints"
    checkpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), index=True)
    buyer_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class ContextOperationRow(Base):
    __tablename__ = "context_operations"
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), index=True)
    buyer_id: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(String(128))
    expected_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text, default='{}')
    deadline: Mapped[float] = mapped_column(Float, default=0)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError(f"{label} 必须为不带首尾空格的非空标识，最长 256 字符")
    return value


def _valid_json(state_json: str) -> None:
    try:
        if not isinstance(json.loads(state_json), dict):
            raise ValueError
    except (TypeError, ValueError) as error:
        raise SessionStateCorrupt("会话快照不是有效 JSON 对象，未覆盖现有状态") from error


class SqlFencedSessionStore(SessionStore):
    def __init__(self, engine: AsyncEngine) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("持久会话 fencing 当前仅交付 SQLite 实现")
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            async with self._engine.connect() as connection:
                try:
                    await connection.execute(text("BEGIN IMMEDIATE"))
                    await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                        AgentSessionStateRow.__table__, ConversationSessionRow.__table__, SessionWriteClaimRow.__table__,
                        TaskAccessBindingRow.__table__, ContextCheckpointRow.__table__, ContextOperationRow.__table__,
                        SessionModelBindingRow.__table__, SessionModelRunRow.__table__,
                    ]))
                    await connection.commit()
                    self._ready = True
                except BaseException:
                    await connection.rollback()
                    raise

    @asynccontextmanager
    async def _transaction(self):
        await self.initialize()
        async with self._sessions() as db:
            try:
                await db.execute(text("BEGIN IMMEDIATE"))
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    async def _ownership(db, session_id: str, buyer_id: str, *, create: bool, enforce_owner: bool, allow_legacy: bool = False):
        row = await db.get(SessionWriteClaimRow, session_id)
        state = await db.get(AgentSessionStateRow, session_id)
        if row is None:
            if not create:
                raise SessionNotFound("会话不存在")
            legacy = await db.get(ConversationSessionRow, session_id)
            owner = legacy.buyer_id if legacy and legacy.buyer_id else None
            if owner is None and state is not None and enforce_owner and not allow_legacy:
                raise SessionOwnerUnbound("历史会话没有可信归属记录，请先显式迁移 owner")
            row = SessionWriteClaimRow(session_id=session_id, owner_id=owner or buyer_id, revision=0, fence=0)
            db.add(row)
        if row.owner_id is None:
            if state is not None and enforce_owner and not allow_legacy:
                raise SessionOwnerUnbound("历史会话归属未绑定，请先迁移 owner")
            row.owner_id = buyer_id
        if enforce_owner and row.owner_id != buyer_id:
            raise SessionOwnerMismatch("无权访问其他买家的会话")
        return row, state

    async def assert_owner(self, session_id: str, buyer_id: str, *, create: bool = False, enforce_owner: bool = True) -> None:
        _identifier(session_id, "session_id")
        _identifier(buyer_id, "buyer_id")
        async with self._transaction() as db:
            await self._ownership(db, session_id, buyer_id, create=create, enforce_owner=enforce_owner)

    async def bind_legacy_owner(self, session_id: str, buyer_id: str) -> None:
        """仅供可信迁移程序调用；仍不允许更改已经绑定的 owner。"""
        _identifier(session_id, "session_id")
        _identifier(buyer_id, "buyer_id")
        async with self._transaction() as db:
            await self._ownership(db, session_id, buyer_id, create=True, enforce_owner=True, allow_legacy=True)

    async def bind_task_owner(self, task_id: str, session_id: str, buyer_id: str) -> None:
        _identifier(task_id, "task_id")
        _identifier(session_id, "session_id")
        _identifier(buyer_id, "buyer_id")
        async with self._transaction() as db:
            row = await db.get(TaskAccessBindingRow, task_id)
            if row is None:
                db.add(TaskAccessBindingRow(task_id=task_id, session_id=session_id, buyer_id=buyer_id))
            elif row.buyer_id != buyer_id or row.session_id != session_id:
                raise SessionOwnerMismatch("任务已经属于其他买家或会话")

    async def assert_task_owner(self, task_id: str, buyer_id: str) -> str:
        _identifier(task_id, "task_id")
        _identifier(buyer_id, "buyer_id")
        await self.initialize()
        async with self._sessions() as db:
            row = await db.get(TaskAccessBindingRow, task_id)
            if row is None:
                raise SessionNotFound("任务不存在或没有可信归属记录")
            if row.buyer_id != buyer_id:
                raise SessionOwnerMismatch("无权读取其他买家的任务")
            return row.session_id

    async def claim(self, session_id: str, *, buyer_id: str, enforce_owner: bool = True) -> SessionClaim:
        _identifier(session_id, "session_id")
        _identifier(buyer_id, "buyer_id")
        async with self._transaction() as db:
            row, state = await self._ownership(db, session_id, buyer_id, create=True, enforce_owner=enforce_owner)
            row.fence += 1
            await db.flush()
            return SessionClaim(session_id, row.owner_id, row.revision, row.fence, state.state_json if state else None)

    async def save_claim(self, claim: SessionClaim, state_json: str) -> SessionClaim:
        _valid_json(state_json)
        async with self._transaction() as db:
            updated = await db.execute(update(SessionWriteClaimRow).where(
                SessionWriteClaimRow.session_id == claim.session_id,
                SessionWriteClaimRow.owner_id == claim.owner_id,
                SessionWriteClaimRow.revision == claim.revision,
                SessionWriteClaimRow.fence == claim.fence,
            ).values(revision=SessionWriteClaimRow.revision + 1))
            if updated.rowcount != 1:
                raise StaleSessionWrite("会话执行权或版本已更新，拒绝旧执行者覆盖最新状态，请重新读取会话")
            state = await db.get(AgentSessionStateRow, claim.session_id)
            if state is None:
                db.add(AgentSessionStateRow(session_id=claim.session_id, state_json=state_json))
            else:
                state.state_json = state_json
                state.updated_at = datetime.now(timezone.utc)
            governance = json.loads(state_json).get('middle_context', {}).get('shoppilot_context', {})
            checkpoint_id = governance.get('checkpoint_id')
            if checkpoint_id and await db.get(ContextCheckpointRow, checkpoint_id) is None:
                db.add(ContextCheckpointRow(checkpoint_id=checkpoint_id, session_id=claim.session_id,
                    buyer_id=claim.owner_id, revision=claim.revision+1,
                    payload=json.dumps({'summary':json.loads(state_json).get('summary'), **governance}, ensure_ascii=False)))
            operation_id = governance.get('operation_id')
            if operation_id:
                operation = await db.get(ContextOperationRow, operation_id)
                if operation and operation.expected_revision == claim.revision and (operation.status != 'running' or operation.deadline < __import__('time').time()):
                    raise StaleSessionWrite('整理操作已中断，拒绝提交迟到的快照')
                if operation and operation.buyer_id == claim.owner_id and operation.status == 'running':
                    operation.status = governance.get('last_compaction',{}).get('status','completed')
                    operation.payload = json.dumps({'statistics':governance.get('last_compaction',{}), 'message':'当前无需整理' if operation.status=='noop' else '上下文已整理，原始记录保留'},ensure_ascii=False)
            await db.flush()
            return replace(claim, revision=claim.revision + 1, state_json=state_json)

    async def save(self, session_id: str, state_json: str) -> None:
        """兼容未迁移快照的初始化；已进入 fencing 的会话禁止绕过版本校验。"""
        _identifier(session_id, "session_id")
        _valid_json(state_json)
        async with self._transaction() as db:
            if await db.get(SessionWriteClaimRow, session_id) is not None:
                raise StaleSessionWrite("受保护的会话必须使用 save_claim 保存")
            await db.merge(AgentSessionStateRow(session_id=session_id, state_json=state_json))

    async def import_legacy(self, session_id: str, state_json: str, *, owner_id: str | None = None) -> None:
        """幂等迁移文件快照；目标存在时始终保留数据库的较新版本。"""
        _identifier(session_id, "session_id")
        _valid_json(state_json)
        async with self._transaction() as db:
            if await db.get(SessionWriteClaimRow, session_id) is not None or await db.get(AgentSessionStateRow, session_id) is not None:
                return
            db.add(AgentSessionStateRow(session_id=session_id, state_json=state_json))
            db.add(SessionWriteClaimRow(session_id=session_id, owner_id=owner_id, revision=0, fence=0))

    async def load(self, session_id: str) -> str | None:
        _identifier(session_id, "session_id")
        await self.initialize()
        async with self._sessions() as db:
            row = await db.get(AgentSessionStateRow, session_id)
            return row.state_json if row else None

    async def context_view(self, session_id: str, buyer_id: str) -> dict:
        view, _ = await self.context_snapshot(session_id, buyer_id)
        return view

    async def model_binding(self, session_id: str, buyer_id: str) -> dict:
        async with self._transaction() as db:
            await self._ownership(db, session_id, buyer_id, create=True, enforce_owner=True)
            row = await db.get(SessionModelBindingRow, session_id)
            return self._model_binding(row)

    @staticmethod
    def _model_binding(row) -> dict:
        return {'revision': row.revision if row else 0,
                'active': json.loads(row.active_json) if row and row.active_json else None,
                'pending': json.loads(row.pending_json) if row and row.pending_json else None}

    async def select_model(self, session_id, buyer_id, snapshot, expected_revision):
        # 只写下一轮意向；不会增加执行 fence 或让当前 AgentState 的 CAS 失效。
        async with self._transaction() as db:
            await self._ownership(db, session_id, buyer_id, create=True, enforce_owner=True)
            row = await db.get(SessionModelBindingRow, session_id)
            if (row.revision if row else 0) != expected_revision:
                raise StaleSessionWrite('模型选择已更新，请刷新后重试')
            if row is None:
                row = SessionModelBindingRow(session_id=session_id, revision=0)
                db.add(row)
            row.pending_json = json.dumps(snapshot, ensure_ascii=False)
            row.revision += 1
            return self._model_binding(row)

    async def bind_run_model(self, claim, snapshot, *, selection_revision, consume_pending=True):
        async with self._transaction() as db:
            owner, _ = await self._ownership(db, claim.session_id, claim.owner_id, create=False, enforce_owner=True)
            if owner.fence != claim.fence or owner.revision != claim.revision:
                raise StaleSessionWrite('模型绑定执行权已失效')
            row = await db.get(SessionModelBindingRow, claim.session_id)
            if row is None:
                row = SessionModelBindingRow(session_id=claim.session_id, revision=0)
                db.add(row)
            # 预检过程中更新的选择保留给下一轮，不覆盖本次已冻结的配置。
            if consume_pending and row.revision == selection_revision:
                row.pending_json = None
            row.active_json = json.dumps(snapshot, ensure_ascii=False)
            db.add(SessionModelRunRow(session_id=claim.session_id, fence=claim.fence, snapshot_json=row.active_json))

    async def context_snapshot(self, session_id: str, buyer_id: str) -> tuple[dict, str | None]:
        """同一事务读取展示信息和模型快照；原始快照只供服务内部估算。"""
        async with self._transaction() as db:
            row, state = await self._ownership(db, session_id, buyer_id, create=False, enforce_owner=True)
            payload = json.loads(state.state_json) if state else {}
            governance = payload.get('middle_context', {}).get('shoppilot_context', {})
            active = await db.scalar(select(ContextOperationRow).where(ContextOperationRow.session_id==session_id, ContextOperationRow.buyer_id==buyer_id, ContextOperationRow.status=='running'))
            view = {'operation':self._context_operation(active) if active else None, 'session_id':session_id, 'revision':row.revision, 'summary':payload.get('summary') or '',
                    'working':governance.get('working',{}), 'statistics':governance.get('last_compaction',{}),
                    'checkpoint_id':governance.get('checkpoint_id'), 'failures':governance.get('failures',0)}
            return view, state.state_json if state else None

    async def create_context_operation(self, session_id, buyer_id, request_id, expected_revision):
        import hashlib
        _identifier(request_id, 'request_id')
        identifier = hashlib.sha256(json.dumps([session_id,buyer_id,request_id]).encode()).hexdigest()
        async with self._transaction() as db:
            row, _ = await self._ownership(db, session_id, buyer_id, create=False, enforce_owner=True)
            existing = await db.get(ContextOperationRow, identifier)
            if existing:
                if existing.expected_revision != expected_revision:
                    raise StaleSessionWrite('相同 request_id 不可用于不同会话版本')
                return self._context_operation(existing), False
            if row.revision != expected_revision:
                raise StaleSessionWrite('会话已更新，请读取最新摘要后重试')
            await db.execute(update(ContextOperationRow).where(ContextOperationRow.session_id==session_id, ContextOperationRow.status=='running', ContextOperationRow.deadline < __import__('time').time()).values(status='interrupted'))
            current = await db.scalar(select(ContextOperationRow).where(ContextOperationRow.session_id==session_id, ContextOperationRow.status=='running'))
            if current:
                raise StaleSessionWrite('会话正在整理，请等待完成')
            operation = ContextOperationRow(operation_id=identifier, session_id=session_id, buyer_id=buyer_id,
                request_id=request_id, expected_revision=expected_revision, status='running', payload='{}', deadline=__import__('time').time()+30)
            db.add(operation)
            return self._context_operation(operation), True

    @staticmethod
    def _context_operation(row):
        return {'operation_id':row.operation_id, 'session_id':row.session_id, 'status':row.status,
                'expected_revision':row.expected_revision, **json.loads(row.payload)}

    async def context_operation(self, operation_id, buyer_id):
        await self.initialize()
        async with self._transaction() as db:
            row = await db.get(ContextOperationRow, operation_id)
            if row is None: raise SessionNotFound('整理操作不存在')
            if row.buyer_id != buyer_id: raise SessionOwnerMismatch('无权读取该整理操作')
            if row.status=='running' and row.deadline < __import__('time').time():
                row.status, row.payload = 'interrupted', '{"message":"整理任务已中断，原始记录保留"}'
            return self._context_operation(row)

    async def finish_context_operation(self, operation_id, buyer_id, status, payload):
        async with self._transaction() as db:
            row = await db.get(ContextOperationRow, operation_id)
            if row is None or row.buyer_id != buyer_id:
                raise SessionOwnerMismatch('无权写入整理操作')
            if row.status == 'running':
                row.status, row.payload = status, json.dumps(payload,ensure_ascii=False)

    async def recover_context_operations(self):
        async with self._transaction() as db:
            await db.execute(update(ContextOperationRow).where(ContextOperationRow.status=='running', ContextOperationRow.deadline < __import__('time').time()).values(status='interrupted',payload='{"message":"服务重启，未提交的整理已中断；原记录保留"}'))

    async def renew_context_operation(self, operation_id):
        async with self._transaction() as db:
            updated = await db.execute(update(ContextOperationRow).where(ContextOperationRow.operation_id==operation_id, ContextOperationRow.status=='running', ContextOperationRow.deadline >= __import__('time').time()).values(deadline=__import__('time').time()+30))
            if updated.rowcount != 1: raise StaleSessionWrite('整理操作租约已失效')
