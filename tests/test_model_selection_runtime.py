"""真实工厂 + 隔离 SQLite 验证模型选择、审批冻结、刷新与重启恢复。"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agentscope.message import UserMsg, AssistantMsg, ToolCallBlock, ToolCallState, TextBlock
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.agents.main_agent import MainAgentFactory, SessionRegistry
from app.application.agents.search_agent import SearchAgentFactory
from app.application.agents.trade_agent import TradeAgentFactory
from app.application.agents.context_service import ContextService
from app.application.agents.model_selection import ModelSelectionService, close_agent_model
from app.domain.session.ports.session_store import SessionOwnerMismatch, StaleSessionWrite
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.model_registry import ModelRegistry
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
from app.infrastructure.resilience import CircuitBreakerRegistry
from app.infrastructure.throttle import GatewayThrottle
from tests.test_retrieval import _settings


@pytest.fixture
async def env(tmp_path):
    settings = replace(_settings(tmp_path), llm_api_key='test-model-key-private',
        llm_base_url='https://fixture.invalid/v1', llm_model='fixture-main', llm_fallback_model='',
        llm_max_retries=0, context_strategy='legacy')
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'sessions.db'))
    store = SqlFencedSessionStore(engine)
    bus, circuits, throttle = TradeEventBus(), CircuitBreakerRegistry(), GatewayThrottle(1, 0)
    search = SearchAgentFactory(settings, None, bus, None, circuits, throttle)
    trade = TradeAgentFactory(settings, None, None, None, bus, circuits, throttle)
    main = MainAgentFactory(settings, search, trade, bus, None, circuits, throttle)
    models = ModelRegistry(settings)
    sessions = SessionRegistry(main, store, model_registry=models)
    orchestrator = SimpleNamespace(_sessions=sessions, _session_locks={})
    selection = ModelSelectionService(models, sessions, orchestrator, store)
    context = ContextService(orchestrator, store, None, settings=settings)
    token = ShoppingContext.set(ShoppingContextSnapshot('session', 'buyer', 'zh-CN', 'CNY'))
    resources = []
    data = SimpleNamespace(settings=settings, engine=engine, store=store, search=search, trade=trade,
        main=main, models=models, sessions=sessions, orchestrator=orchestrator, selection=selection,
        context=context, resources=resources)
    try:
        yield data
    finally:
        for agent in resources:
            await close_agent_model(agent)
        for session in list(sessions._agents):
            await sessions.invalidate(session)
        ShoppingContext.reset(token)
        await engine.dispose()


async def choose(env, window, *, expected_revision=0):
    return await env.selection.select('session', 'buyer', {
        'profile_id': 'current-gateway', 'window_id': f'budget-{window}',
        'thinking': 'default', 'expected_revision': expected_revision})


async def persisted_agent(env):
    agent = await env.sessions.get_or_create('session')
    agent.state.context.append(UserMsg('buyer', '推荐一个轻便旅行背包'))
    await env.sessions.persist('session')
    return agent


async def test_real_main_search_trade_factories_share_frozen_runtime(env):
    await env.store.model_binding('session', 'buyer')
    await choose(env, 64000)
    main = await env.sessions.get_or_create('session')
    search, trade = env.search.build(), env.trade.build()
    env.resources.extend([search, trade])
    assert {agent.model.context_size for agent in (main, search, trade)} == {64000}
    assert {agent.model.model for agent in (main, search, trade)} == {'fixture-main'}
    assert len({agent.model._shoppilot_runtime['fingerprint'] for agent in (main, search, trade)}) == 1
    assert len({id(agent.model) for agent in (main, search, trade)}) == 3
    await env.sessions.persist('session')
    raw = await env.store.load('session')
    assert 'test-model-key-private' not in raw and 'api_key' not in raw


async def test_pending_selection_does_not_mutate_active_turn_or_later_subagent(env):
    agent = await persisted_agent(env)
    lock = env.orchestrator._session_locks.setdefault('session', asyncio.Lock())
    async with lock:
        result = await choose(env, 64000)
        assert result['selection']['context_size'] == 64000
        assert result['active_selection']['context_size'] == 128000
        assert agent.model.context_size == 128000
        child = env.search.build()
        env.resources.append(child)
        assert child.model.context_size == 128000
        assert (await env.context.view('session', 'buyer'))['usage']['context_window'] == 128000
    maintenance = await env.sessions.get_or_create('session', apply_model_selection=False)
    assert maintenance.model.context_size == 128000
    assert (await env.store.model_binding('session', 'buyer'))['pending']['context_size'] == 64000
    next_agent = await env.sessions.get_or_create('session')
    assert next_agent.model.context_size == 64000
    assert agent.model.client.is_closed()
    assert (await env.store.model_binding('session', 'buyer'))['pending'] is None


async def test_native_ask_keeps_old_snapshot_and_pending_choice_across_restart(env):
    agent = await persisted_agent(env)
    agent.state.context.append(AssistantMsg(agent.name, [ToolCallBlock(
        id='pending-memory', name='remember_preference_tool', input='{"statement":"喜欢背包"}',
        state=ToolCallState.ASKING)]))
    await env.sessions.persist('session')
    await choose(env, 64000)
    await env.sessions.invalidate('session')
    restarted = SessionRegistry(env.main, env.store, model_registry=ModelRegistry(env.settings))
    try:
        restored = await restarted.get_or_create('session')
        assert restored.model.context_size == 128000
        assert restored.state.get_awaiting_tool_calls(restored.name)[0].id == 'pending-memory'
        binding = await env.store.model_binding('session', 'buyer')
        assert binding['pending']['context_size'] == 64000
        assert (await env.context.view('session', 'buyer'))['usage']['context_window'] == 128000
        # 走真实 SDK 拒绝恢复，只替换模型网络；拒绝不会访问偏好存储。
        from app.application.agents.tool_confirmation import awaiting_event, confirmation_inputs
        async def finish(*args, **kwargs):
            return ChatResponse(content=[TextBlock(text='已拒绝记忆写入')], is_last=True)
        restored.model._invoke_upstream = finish
        event = awaiting_event(restored)
        incoming = confirmation_inputs(restored, ({
            'interrupt_id': f'{event.reply_id}:pending-memory', 'approved': False},))
        _ = [event async for event in restored.reply_stream(incoming)]
        assert awaiting_event(restored) is None
        assert restored.model.context_size == 128000
        await restarted.persist('session')
        next_agent = await restarted.get_or_create('session')
        assert next_agent.model.context_size == 64000
    finally:
        await restarted.invalidate('session')


async def test_legacy_history_uses_first_import_not_new_buyer_default_or_preferences(env):
    state = AgentState(context=[UserMsg('buyer', '这是升级前已有的旅行包对话')])
    claim = await env.store.claim('session', buyer_id='buyer')
    await env.store.save_claim(claim, state.model_dump_json())
    await env.models.initialize()
    await env.models.set_preferences('current-gateway', 'buyer', default_window='budget-64000')
    await env.models.set_default('buyer', 'current-gateway', window_id='budget-32000')
    view = await env.selection.view('session', 'buyer')
    assert view['active_selection']['context_size'] == 128000
    assert (await env.context.view('session', 'buyer'))['usage']['context_window'] == 128000
    restored = await env.sessions.get_or_create('session')
    snapshot = restored.state.middle_context['shoppilot_model']
    assert restored.model.context_size == 128000 and snapshot['profile_revision'] == 1
    assert snapshot['source']['binding'] == 'legacy_migration_inferred'
    await env.sessions.persist('session')
    new = await env.selection.view('new-session', 'buyer')
    assert new['active_selection']['context_size'] == 32000


@pytest.mark.parametrize('source', ['active', 'saved_only'])
async def test_bound_session_never_resolves_unrelated_broken_default(env, monkeypatch, source):
    from unittest.mock import AsyncMock
    from sqlalchemy import delete
    from app.infrastructure.persistence.sql.session_store import SessionModelBindingRow
    await persisted_agent(env)
    if source == 'saved_only':
        async with env.engine.begin() as connection:
            await connection.execute(delete(SessionModelBindingRow))
    forbidden = AsyncMock(side_effect=AssertionError('有效旧会话不能读取无关的新默认'))
    monkeypatch.setattr(env.models, 'resolve_default', forbidden)
    assert (await env.selection.view('session', 'buyer'))['active_selection']['context_size'] == 128000
    assert (await env.context.view('session', 'buyer'))['usage']['context_window'] == 128000
    assert (await env.sessions.get_or_create('session')).model.context_size == 128000
    forbidden.assert_not_awaited()


async def test_context_meter_uses_pending_window_then_active_binding_before_new_save(env):
    await persisted_agent(env)
    before = (await env.context.view('session', 'buyer'))['usage']
    await choose(env, 64000)
    pending = (await env.context.view('session', 'buyer'))['usage']
    assert before['context_window'] == 128000 and pending['context_window'] == 64000
    assert pending['estimated_input_tokens'] == before['estimated_input_tokens']
    assert pending['utilization_ratio'] == 2 * before['utilization_ratio']
    lock = env.orchestrator._session_locks.setdefault('session', asyncio.Lock())
    async with lock:
        await env.sessions.get_or_create('session')
        # 尚未保存新 AgentState，读取上一轮消息但分母必须来自已冻结的新运行。
        during = (await env.context.view('session', 'buyer'))['usage']
        assert during['context_window'] == 64000


async def test_smaller_window_capacity_rejects_without_changing_selection_or_history(env):
    agent = await persisted_agent(env)
    agent.state.context.append(UserMsg('buyer', '必须保持的有效商品约束。' * 6000))
    await env.sessions.persist('session')
    raw = await env.store.load('session')
    with pytest.raises(StaleSessionWrite, match='无法安全容纳'):
        await choose(env, 32000)
    assert (await env.store.model_binding('session', 'buyer'))['pending'] is None
    assert await env.store.load('session') == raw
    assert not agent.model.client.is_closed()


async def test_selection_and_context_restore_keep_buyer_ownership(env):
    await persisted_agent(env)
    await choose(env, 64000)
    with pytest.raises(SessionOwnerMismatch):
        await env.selection.view('session', 'someone-else')
    with pytest.raises(SessionOwnerMismatch):
        await env.context.view('session', 'someone-else')
    token = ShoppingContext.set(ShoppingContextSnapshot('session', 'someone-else', 'zh-CN', 'CNY'))
    try:
        with pytest.raises(SessionOwnerMismatch):
            await env.sessions.get_or_create('session')
    finally:
        ShoppingContext.reset(token)
    restarted = SessionRegistry(env.main, env.store, model_registry=ModelRegistry(env.settings))
    try:
        agent = await restarted.get_or_create('session')
        assert agent.model.context_size == 64000
        assert agent.state.context[0].get_text_content() == '推荐一个轻便旅行背包'
    finally:
        await restarted.invalidate('session')
