"""手动整理是独立维护操作，不生成买家消息、不调用交易工具。"""
from __future__ import annotations
import asyncio
import logging
import math
from contextlib import AsyncExitStack
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import LayeredContextMiddleware, update_working_state, ContextCapacityError
from app.application.agents.tool_confirmation import awaiting_event
from app.domain.session.ports.session_store import StaleSessionWrite

logger = logging.getLogger(__name__)


class ContextService:
    def __init__(self, orchestrator, store, evidence, confirmations=None, settings=None):
        self.orchestrator, self.store, self.evidence = orchestrator, store, evidence
        self.confirmations, self.settings = confirmations, settings
        self.tasks = {}

    async def startup(self):
        if hasattr(self.store, 'recover_context_operations'):
            await self.store.recover_context_operations()

    async def shutdown(self):
        for task in self.tasks.values(): task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def view(self, session, buyer):
        # revision 与快照来自同一事务，不把上次整理或累计用量冒充当前占用。
        if hasattr(self.store, 'context_snapshot'):
            result, state_json = await self.store.context_snapshot(session, buyer)
        else:
            result, state_json = await self.store.context_view(session, buyer), None
        result['strategy'] = getattr(self.settings,'context_strategy','legacy')
        result['usage'] = await self._snapshot_usage(session, buyer, result['revision'], state_json)
        return result

    async def _snapshot_usage(self, session, buyer, revision, state_json):
        """完整已保存工作集的本地估算；不取执行权、不运行模型、不更改真实状态。"""
        registry = self.orchestrator._sessions
        factory = getattr(registry, '_main_factory', None)
        if not state_json or factory is None:
            return None
        token = ShoppingContext.set(ShoppingContextSnapshot(session, buyer, 'zh-CN', 'CNY'))
        agent = None
        try:
            from agentscope.state import AgentState
            state = AgentState.model_validate_json(state_json)
            if not state.context and not state.summary:
                return None
            prompt_registry = getattr(registry, '_prompt_registry', None)
            if prompt_registry is not None:
                # 不能静默用当前 YAML 替换该会话固定的 Prompt 版本。
                assignment = await prompt_registry.existing_assignment(session, buyer)
                if assignment is None:
                    return None
                ShoppingContext.set_prompt_assignment(assignment)
            ShoppingContext.set_skill_catalog_mode(getattr(factory, 'skill_catalog_mode', 'legacy'))
            model_registry = getattr(registry, '_model_registry', None)
            model_snapshot = state.middle_context.get('shoppilot_model')
            if model_registry is not None:
                binding = await self.store.model_binding(session, buyer)
                locked = self.orchestrator._session_locks.get(session)
                from app.application.agents.model_selection import resolve_session_snapshot
                model_snapshot = await resolve_session_snapshot(model_registry, buyer, binding, state,
                    allow_pending=not (locked and locked.locked()))
                ShoppingContext.set_model(model_snapshot,
                    await model_registry.runtime_for_snapshot(model_snapshot, buyer_id=buyer))
            agent = factory.build(state)
            middleware = next((m for m in agent._model_call_middlewares
                               if isinstance(m, LayeredContextMiddleware)), None)
            if middleware is not None:
                estimated = await middleware.count(agent)
            else:
                # legacy 没有商品共享投影，按它实际准备的完整输入估算。
                prepared = await agent._prepare_model_input()
                from app.infrastructure.model_counting import count_model_input
                estimated = (await count_model_input(agent.model, **prepared, calibration=state.middle_context.get('shoppilot_context', {}).get('calibration')))['estimated_input_tokens']
            from app.infrastructure.model_counting import input_limits
            limits = input_limits(agent.model)
            window, reserve, safety, available = (limits[k] for k in ('context_window','output_reserve','safety_reserve','input_budget'))
            if not math.isfinite(estimated) or estimated < 0 or available <= 0:
                return None
            return {'estimated_input_tokens': estimated, 'context_window': window,
                    'utilization_ratio': estimated / window, 'input_budget': available,
                    'output_reserve': reserve, 'safety_reserve': safety,
                    'saved_revision': revision, 'source': 'saved_context_estimate',
                    'model': agent.model.model, 'profile_id': (model_snapshot or {}).get('profile_id'),
                    'context_max': (model_snapshot or {}).get('context_max', window),
                    'counter_method': (getattr(agent.model, '_shoppilot_counter_metadata', {}) or {}).get('method', 'agentscope_estimate')}
        except Exception as error:
            # 估算不可用不阻断摘要与手动整理，也不输出买家快照或配置内容。
            logger.warning('上下文占用估算不可用：%s', type(error).__name__)
            return None
        finally:
            try:
                if agent is not None:
                    # 工厂为副本创建的主/回退客户端都要释放；不会关闭会话里的模型。
                    models, seen, clients = [agent.model], set(), set()
                    while models:
                        model = models.pop()
                        if id(model) in seen:
                            continue
                        seen.add(id(model))
                        fallback = getattr(model, '_fallback', None)
                        if fallback is not None:
                            models.append(fallback)
                        client = getattr(model, 'client', None)
                        if client is not None and id(client) not in clients:
                            clients.add(id(client))
                            try:
                                await client.close()
                            except Exception as error:
                                logger.warning('上下文估算客户端释放失败：%s', type(error).__name__)
            finally:
                ShoppingContext.reset(token)

    async def start(self, session, buyer, request_id, expected_revision):
        lock = self.orchestrator._session_locks.setdefault(session, asyncio.Lock())
        # 重复请求即使运行中也能拿回原操作。
        import hashlib, json
        identifier = hashlib.sha256(json.dumps([session,buyer,request_id]).encode()).hexdigest()
        from app.domain.session.ports.session_store import SessionNotFound
        try:
            old = await self.store.context_operation(identifier,buyer)
            if old['expected_revision'] != expected_revision: raise StaleSessionWrite('相同请求不可换版本')
            return old
        except SessionNotFound:
            pass
        if lock.locked(): raise StaleSessionWrite('当前正在选购或整理，请等待完成')
        await lock.acquire()
        try:
            if self.confirmations is not None:
                saved = await self.confirmations.list(buyer, session)
                if any(c.get('status')=='pending' and not c.get('expired') for c in saved.get('confirmations',[])):
                    raise StaleSessionWrite('请先完成或拒绝待确认操作')
            # 不通过 get_or_create 改变执行 fence 来检查审批。
            from agentscope.state import AgentState
            from types import SimpleNamespace
            raw = await self.store.load(session)
            if raw:
                saved_state = AgentState.model_validate_json(raw)
                if saved_state.context and saved_state.get_awaiting_tool_calls(saved_state.context[-1].name):
                    raise StaleSessionWrite('请先批准或拒绝待处理的记忆操作')
            operation, created = await self.store.create_context_operation(session,buyer,request_id,expected_revision)
            if not created:
                lock.release()
                return operation
            task = asyncio.create_task(self._run(operation,buyer,lock),name='context:'+operation['operation_id'])
            self.tasks[operation['operation_id']] = task
            task.add_done_callback(lambda _: self.tasks.pop(operation['operation_id'],None))
            return operation
        except BaseException:
            lock.release()
            raise

    async def _run(self, operation, buyer, lock):
        session, identifier = operation['session_id'], operation['operation_id']
        token = ShoppingContext.set(ShoppingContextSnapshot(session,buyer,'zh-CN','CNY'))
        registry = self.orchestrator._sessions
        owner_task = asyncio.current_task()
        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(5)
                    await self.store.renew_context_operation(identifier)
            except asyncio.CancelledError:
                raise
            except Exception:
                owner_task.cancel()
        renewal = asyncio.create_task(heartbeat())
        try:
            async with AsyncExitStack() as stack:
                lease_factory = self.orchestrator._session_lease_factory
                lease = await stack.enter_async_context(lease_factory(session)) if lease_factory else None
                await registry.invalidate(session)
                view = await self.store.context_view(session,buyer)
                if view['revision'] != operation['expected_revision']:
                    raise StaleSessionWrite('选购记录已更新，请重试整理')
                agent = await registry.get_or_create(session, **({'apply_model_selection': False} if getattr(registry, '_model_registry', None) else {}))
                if awaiting_event(agent) is not None: raise StaleSessionWrite('尚有待审批工具')
                update_working_state(agent, agent.state.context)
                middleware = LayeredContextMiddleware(self.evidence,
                    product_tokens=getattr(self.settings,'context_product_tokens',6000),
                    target_tokens=getattr(self.settings,'context_target_tokens',48000),
                    prompt_layout=getattr(self.settings,"context_prompt_layout","legacy_system"), state_mode=getattr(self.settings,"context_state_mode","snapshot"),
                    prune_low_ratio=getattr(self.settings,"context_prune_low_ratio",1.0), compact_result_rules=getattr(self.settings,"context_compact_result_rules",False))
                result = await middleware.run(agent, force=True)
                if lease and not lease.is_valid(): raise StaleSessionWrite('整理执行权已失效')
                from app.infrastructure.context_governance import governance
                governance(agent)['operation_id'] = identifier
                if not await registry.persist(session): raise StaleSessionWrite('会话保存未完成')
                await self.store.finish_context_operation(identifier,buyer,result['status'],{'statistics':result,'message':'当前无需整理' if result['status']=='noop' else '上下文已整理，原始记录保留'})
        except asyncio.CancelledError:
            await registry.invalidate(session)
            await asyncio.shield(self.store.finish_context_operation(identifier,buyer,'interrupted',{'message':'整理已中断，原记录保留'}))
        except Exception as error:
            await registry.invalidate(session)
            await self.store.finish_context_operation(identifier,buyer,'failed',{'message': '受保护内容超过安全容量，原始记录已保留。请缩小范围或新建选购会话。' if isinstance(error,ContextCapacityError) else '会话版本或选购状态已变化，请先继续选购后再整理' if isinstance(error,StaleSessionWrite) or type(error).__name__ in {'PromptContractChanged','CapabilityVersionChanged'} else '本次整理未完成，原记录保留；可稍后重试', 'error_code':type(error).__name__})
        finally:
            renewal.cancel()
            await asyncio.gather(renewal,return_exceptions=True)
            ShoppingContext.reset(token)
            lock.release()
