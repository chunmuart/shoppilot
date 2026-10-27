"""上下文圆环读取已保存工作集；不执行模型，不取得会话执行权。"""
from __future__ import annotations

import math
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from agentscope.message import UserMsg
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.agents.context_service import ContextService
from app.application.prompts.loader import load_prompts
from app.domain.session.ports.session_store import SessionOwnerMismatch
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_governance import ContextAwareAgent, LayeredContextMiddleware
from app.infrastructure.persistence.sql.session_store import SessionWriteClaimRow, SqlFencedSessionStore
from app.infrastructure.prompt_registry import PromptRegistry, PromptRegistryError
from app.presentation.context_workspace import register_context_routes
from tests.test_native_memory_confirmation import Model


class CountingFactory:
    skill_catalog_mode = 'legacy'

    def __init__(self, *, layered=False, fail_count=False):
        self.layered, self.fail_count = layered, fail_count
        self.agents = []

    def build(self, state):
        async def inspect_product(product_id: str) -> str:
            """查询商品，不允许上下文统计执行此工具。

            Args:
                product_id (`str`): 目标商品的唯一标识。
            """
            raise AssertionError('只读估算不能执行工具')

        model = Model()
        model.context_size = 128000
        model._fallback = Model()
        if self.fail_count:
            model.count_tokens = AsyncMock(side_effect=ValueError('计数不可用'))
        middlewares = [LayeredContextMiddleware(None)] if self.layered else []
        agent = ContextAwareAgent('main', load_prompts()['main_agent']['system_prompt'], model,
                                  state=state, middlewares=middlewares,
                                  toolkit=Toolkit(tools=[FunctionTool(inspect_product)]))
        self.agents.append(agent)
        return agent


@pytest.fixture
async def context_env(tmp_path):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'sessions.db'))
    store = SqlFencedSessionStore(engine)
    state = AgentState(context=[UserMsg('buyer', '请推荐轻便旅行包。')], summary='已确认仅看旅行包。')
    state.middle_context['shoppilot_context'] = {'last_compaction': {'after_tokens': 1},
                                            'last_model': {'input_tokens': 999999}}
    claim = await store.claim('session', buyer_id='buyer')
    await store.save_claim(claim, state.model_dump_json())
    yield SimpleNamespace(engine=engine, store=store, state=state, tmp=tmp_path)
    await engine.dispose()


def service_for(env, factory, prompt_registry=None):
    sessions = SimpleNamespace(_main_factory=factory, _prompt_registry=prompt_registry,
                               get_or_create=AsyncMock(side_effect=AssertionError('不能 claim 会话')))
    orchestrator = SimpleNamespace(_sessions=sessions, _session_locks={})
    return ContextService(orchestrator, env.store, None,
                          settings=SimpleNamespace(context_strategy='layered' if factory and factory.layered else 'legacy'))


async def claims(env):
    async with env.store._sessions() as db:
        row = await db.scalar(select(SessionWriteClaimRow).where(SessionWriteClaimRow.session_id == 'session'))
        return row.revision, row.fence


@pytest.mark.parametrize('layered', [False, True])
async def test_usage_counts_complete_snapshot_without_model_or_session_mutations(context_env, layered):
    env = context_env
    factory = CountingFactory(layered=layered)
    service = service_for(env, factory)
    saved = await env.store.load('session')
    before = await claims(env)
    outer = ShoppingContextSnapshot('outer-session', 'outer-buyer', 'en-US', 'USD')
    token = ShoppingContext.set(outer)
    try:
        view = await service.view('session', 'buyer')
        assert ShoppingContext.current() is outer
    finally:
        ShoppingContext.reset(token)
    usage = view['usage']
    assert usage['source'] == 'saved_context_estimate'
    assert usage['saved_revision'] == view['revision'] == before[0]
    assert usage['context_window'] == 128000
    assert usage['output_reserve'] == 8192
    assert usage['safety_reserve'] == 6400
    assert usage['input_budget'] == 113408
    assert usage['utilization_ratio'] == usage['estimated_input_tokens'] / 128000
    assert usage['estimated_input_tokens'] not in (1, 999999)
    agent = factory.agents[-1]
    prepared = await agent._prepare_model_input()
    assert prepared['messages'][0].role == 'system'
    assert any('已确认仅看旅行包' in message.get_text_content() for message in prepared['messages'])
    assert any(tool['function']['name'] == 'inspect_product' for tool in prepared['tools'])
    raw = await agent.model.count_tokens(**prepared)
    assert usage['estimated_input_tokens'] == math.ceil(raw * 1.5)
    assert agent.model.calls == agent.model._fallback.calls == 0
    assert agent.model.client.is_closed() and agent.model._fallback.client.is_closed()
    assert await env.store.load('session') == saved
    assert await claims(env) == before
    service.orchestrator._sessions.get_or_create.assert_not_awaited()


async def test_usage_recomputes_new_revision_instead_of_returning_compaction_statistics(context_env):
    env = context_env
    service = service_for(env, CountingFactory())
    first = await service.view('session', 'buyer')
    state = AgentState.model_validate_json(await env.store.load('session'))
    state.context.append(UserMsg('buyer', '新增预算与配送限制。' * 200))
    claim = await env.store.claim('session', buyer_id='buyer')
    await env.store.save_claim(claim, state.model_dump_json())
    second = await service.view('session', 'buyer')
    assert second['usage']['saved_revision'] == first['usage']['saved_revision'] + 1
    assert second['usage']['estimated_input_tokens'] > first['usage']['estimated_input_tokens']
    assert second['statistics']['after_tokens'] == first['statistics']['after_tokens'] == 1


@pytest.mark.parametrize('same_model', [False, True])
async def test_layered_usage_applies_only_matching_model_calibration(context_env, same_model):
    env = context_env
    model = Model()
    identity = str(model.model) + '|' + str(model.client.base_url)
    await model.client.close()
    state = AgentState.model_validate_json(await env.store.load('session'))
    state.middle_context['shoppilot_context']['calibration'] = {
        'identity': identity if same_model else 'different-model|different-gateway', 'factor': 3,
    }
    claim = await env.store.claim('session', buyer_id='buyer')
    await env.store.save_claim(claim, state.model_dump_json())
    factory = CountingFactory(layered=True)
    usage = (await service_for(env, factory).view('session', 'buyer'))['usage']
    agent = factory.agents[-1]
    raw = await agent.model.count_tokens(**(await agent._prepare_model_input()))
    assert usage['estimated_input_tokens'] == math.ceil(raw * (3 if same_model else 1.5))


async def test_context_endpoint_does_not_expose_raw_snapshot_and_checks_owner(context_env):
    env = context_env
    factory = CountingFactory()
    service = service_for(env, factory)
    api = FastAPI()
    api.state.session_store = env.store
    register_context_routes(api, lambda: service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
        response = await client.get('/commerce/context?buyer_id=buyer&session_id=session')
        assert response.status_code == 200
        assert response.json()['usage']['estimated_input_tokens'] > 0
        assert '请推荐轻便旅行包' not in response.text
        assert 'middle_context' not in response.text and 'state_json' not in response.text
        count = len(factory.agents)
        assert (await client.get('/commerce/context?buyer_id=other&session_id=session')).status_code == 403
        assert (await client.get('/commerce/context?buyer_id=buyer&session_id=missing')).status_code == 404
        assert len(factory.agents) == count
    with pytest.raises(SessionOwnerMismatch):
        await env.store.context_snapshot('session', 'other')


async def test_unknown_usage_stays_null_and_failed_count_closes_clients(context_env):
    env = context_env
    assert (await service_for(env, None).view('session', 'buyer'))['usage'] is None
    factory = CountingFactory(fail_count=True)
    view = await service_for(env, factory).view('session', 'buyer')
    assert view['usage'] is None and view['summary'] == env.state.summary
    assert factory.agents[-1].model.client.is_closed()
    assert factory.agents[-1].model._fallback.client.is_closed()
    await env.store.assert_owner('empty', 'buyer', create=True)
    factory = CountingFactory()
    assert (await service_for(env, factory).view('empty', 'buyer'))['usage'] is None
    assert not factory.agents


def prompt_registry(env):
    prompt = {'main_agent': {'name': 'main', 'system_prompt': '本会话固定的旧版规则。' * 30},
              'sub_agents': {'search': {'name': 'search', 'system_prompt': '查询'},
                             'trade': {'name': 'trade', 'system_prompt': '交易'}}}
    path = env.tmp / 'old.yml'
    path.write_text(yaml.safe_dump(prompt, allow_unicode=True))
    registry = PromptRegistry(env.tmp / 'prompts.db', {'test_contract': 1})
    registry.bootstrap(path)
    return registry, path, prompt


async def test_usage_uses_existing_prompt_version_and_missing_assignment_is_unknown(context_env):
    env = context_env
    registry, path, prompt = prompt_registry(env)
    factory = CountingFactory()
    service = service_for(env, factory, registry)
    assert (await service.view('session', 'buyer'))['usage'] is None
    assert not factory.agents
    assignment = await registry.assign('session', 'buyer')
    prompt['main_agent']['system_prompt'] = '另一个待发布新版本。'
    path.write_text(yaml.safe_dump(prompt, allow_unicode=True))
    registry.import_version(path)
    before = registry.path.read_bytes()
    result = await service.view('session', 'buyer')
    assert result['usage'] is not None
    assert factory.agents[-1]._system_prompt == assignment['document']['main_agent']['system_prompt']
    assert factory.agents[-1]._system_prompt != prompt['main_agent']['system_prompt']
    assert registry.path.read_bytes() == before
    with sqlite3.connect(registry.path) as db:
        assert db.execute('SELECT count(*) FROM prompt_assignments').fetchone()[0] == 1


async def test_existing_assignment_is_read_only_and_keeps_contract_and_owner_checks(context_env):
    env = context_env
    missing = PromptRegistry(env.tmp / 'absent' / 'prompts.db', {'test_contract': 1})
    assert await missing.existing_assignment('session', 'buyer') is None
    assert not missing.path.parent.exists()
    registry, _, _ = prompt_registry(env)
    assigned = await registry.assign('session', 'buyer')
    before = registry.path.read_bytes()
    assert await registry.existing_assignment('session', 'buyer') == assigned
    assert await registry.existing_assignment('missing', 'buyer') is None
    with pytest.raises(PromptRegistryError, match='其他买家'):
        await registry.existing_assignment('session', 'other')
    changed = PromptRegistry(registry.path, {'test_contract': 2})
    with pytest.raises(PromptRegistryError, match='工具契约'):
        await changed.existing_assignment('session', 'buyer')
    assert registry.path.read_bytes() == before
