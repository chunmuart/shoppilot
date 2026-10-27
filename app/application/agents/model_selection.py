"""会话模型选择：可变的下一轮意向与不可变的执行快照分开保存。"""
from __future__ import annotations

from agentscope.state import AgentState
from app.infrastructure.context import ShoppingContext
from app.infrastructure.model_counting import count_model_input, input_limits
from app.domain.session.ports.session_store import StaleSessionWrite


async def resolve_session_snapshot(registry, buyer, binding, state=None, *, allow_pending=True):
    """旧历史先固定已有配置；只有真正的新会话才读取买家的新默认。"""
    saved = state.middle_context.get('shoppilot_model') if state else None
    awaiting = bool(state and state.context and state.get_awaiting_tool_calls(state.context[-1].name))
    if awaiting and saved:
        return saved
    if allow_pending and not awaiting and binding.get('pending'):
        return binding['pending']
    if binding.get('active'):
        return binding['active']
    if saved:
        return saved
    if state and (state.context or state.summary):
        # 老版本没有保存模型身份，只能明确推断为首次导入配置；不能跟随买家新默认。
        return await registry.resolve_imported_default(buyer_id=buyer)
    return await registry.resolve_default(buyer_id=buyer)


async def close_agent_model(agent):
    """工厂副本及被替换的模型客户端及时释放，不影响其它会话。"""
    models, seen = [getattr(agent, 'model', None)], set()
    while models:
        model = models.pop()
        if model is None or id(model) in seen:
            continue
        seen.add(id(model))
        models.append(getattr(model, '_fallback', None))
        client = getattr(model, 'client', None)
        if client is not None and hasattr(client, 'close'):
            await client.close()


def public_selection(snapshot):
    if not snapshot:
        return None
    return {key: snapshot.get(key) for key in (
        'profile_id', 'profile_revision', 'model_id', 'label', 'window_id',
        'thinking', 'context_size', 'context_max', 'fingerprint')}


class ModelSelectionService:
    def __init__(self, registry, sessions, orchestrator, store):
        self.registry, self.sessions, self.orchestrator, self.store = registry, sessions, orchestrator, store

    async def view(self, session, buyer):
        catalog = await self.registry.catalog(buyer)
        binding = await self.store.model_binding(session, buyer)
        _, raw = await self.store.context_snapshot(session, buyer)
        state = AgentState.model_validate_json(raw) if raw else None
        active = await resolve_session_snapshot(self.registry, buyer, binding, state, allow_pending=False)
        pending = binding['pending']
        # 页面直接显示已保存的下一轮选择，并同时说明当前执行模型。
        return {**catalog, 'revision': binding['revision'],
                'selection': public_selection(pending or active),
                'active_selection': public_selection(active),
                'pending_selection': public_selection(pending)}

    async def select(self, session, buyer, body):
        snapshot = await self.registry.resolve(body['profile_id'], body.get('window_id'),
            body.get('thinking'), buyer_id=buyer, revision=body.get('profile_revision'))
        # 切换不改历史；缩小窗口时先用同一工厂预检，运行开始时再检查最新状态。
        _, raw = await self.store.context_snapshot(session, buyer)
        if raw:
            await self.sessions.check_model_capacity(session, buyer, snapshot, AgentState.model_validate_json(raw))
        await self.store.select_model(session, buyer, snapshot, body['expected_revision'])
        return await self.view(session, buyer)


async def check_capacity(agent):
    prepared = await agent._prepare_model_input()
    count = await count_model_input(agent.model, **prepared,
        calibration=agent.state.middle_context.get('shoppilot_context', {}).get('calibration'))
    if count['estimated_input_tokens'] > input_limits(agent.model)['input_budget']:
        raise StaleSessionWrite('所选窗口无法安全容纳当前上下文。请先整理上下文，或选择更大的窗口；原模型与历史保留。')
