# -*- coding: utf-8 -*-
"""MainAgent（CommerceConcierge）

跨境电商超级框总调度。基于 AgentScope 2.0 Agent，工具集分四层：
    1. 全部业务工具（product_search / create_order / query_order / cancel_order / [web_search]）
       ——与子 Agent 持有同一批工具，简单任务主 Agent 直接单干；
    2. 内置 Task 计划四件套（TaskCreate / TaskUpdate / TaskList / TaskGet）
       ——2.0 官方计划管理，挂在 AgentState.tasks_context 上；
    3. task_dispatch——满足"可并行 / 上下文隔离 / 链深"任一条件时派发专家子 Agent；
    4. remember_preference_tool——长期记忆写路径（读路径由 orchestrator 注入 hint）。

每个 shopping_session_id 对应一个 MainAgent 实例，由 SessionRegistry 缓存；
AgentState 每轮落盘 DATA_DIR/sessions/，服务重启后恢复多轮对话；
子 Agent 则每次调度新建（上下文隔离）。
"""
from __future__ import annotations
from app.infrastructure.context_governance import ContextAwareAgent

import logging
from typing import Optional

from agentscope.agent import Agent, ReActConfig
from agentscope.state import AgentState
from agentscope.tool import (
    FunctionTool,
    TaskCreate,
    TaskGet,
    TaskList,
    TaskUpdate,
    Toolkit,
)

from app.application.agents.context_policy import build_context_config
from app.application.agents.permissions import allow_business_tools
from app.application.agents.search_agent import SearchAgentFactory
from app.application.agents.trade_agent import TradeAgentFactory
from app.application.harness.assertions import SequencingTracker
from app.application.harness.loop_detector import LoopDetector
from app.application.memory.preference_selector import PreferenceSelector
from app.application.prompts.loader import load_prompts
from app.application.tools.forget_preference_tool import build_forget_preference_tool
from app.application.tools.remember_preference_tool import build_remember_preference_tool
from app.application.tools.update_preference_tool import build_update_preference_tool
from app.application.tools.task_dispatch_tool import build_task_dispatch_tool
from app.application.tools.capability_tools import build_capability_tools, capability_hint, STABLE_CAPABILITY_POLICY
from app.application.agents.personal_skill_context import SkillCatalogMiddleware
from app.domain.buyer.preference import PreferenceStore
from app.domain.session.ports.session_store import SessionStore
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.harness_middleware import HarnessToolMiddleware
from app.infrastructure.llm import create_chat_model
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.resilience import (
    CircuitBreakerRegistry,
    ToolResilienceMiddleware,
)
from app.infrastructure.settings import Settings
from app.infrastructure.tracing import build_agent_middlewares, record_prompt_assignment

logger = logging.getLogger(__name__)


class MainAgentFactory:
    def __init__(
        self,
        settings: Settings,
        search_factory: SearchAgentFactory,
        trade_factory: TradeAgentFactory,
        bus: TradeEventBus,
        preference_store: PreferenceStore,
        circuit_registry: CircuitBreakerRegistry,
        throttle: GatewayThrottle,
        sequencing: Optional[SequencingTracker] = None,
        loop_detector: Optional[LoopDetector] = None,
        preference_selector: Optional[PreferenceSelector] = None,
        capability_registry=None,
        buyer_skill_store=None,
        shopping_form_store=None,
        throttle_factory=None,
    ) -> None:
        self._settings = settings
        self._search_factory = search_factory
        self._trade_factory = trade_factory
        self._bus = bus
        self._preference_store = preference_store
        self._circuit_registry = circuit_registry
        self._throttle = throttle
        self._throttle_factory = throttle_factory
        self._capability_registry = capability_registry
        self.capability_registry = capability_registry
        self.buyer_skill_store = buyer_skill_store
        self.skill_catalog_mode = settings.skill_catalog_mode
        if self.skill_catalog_mode not in {"legacy", "append_only"}:
            raise ValueError("SKILL_CATALOG_MODE 只支持 legacy / append_only")
        self.stable_prefix = settings.context_prompt_layout == 'stable_prefix'
        if self.stable_prefix:
            # 稳定布局必须覆盖目录链路，不能只稳定购物状态却继续回写旧 Skill。
            self.skill_catalog_mode = 'append_only'
        self.shopping_form_store = shopping_form_store
        # 与 orchestrator 共用同一个 selector，保证主/子 Agent 的偏好选取口径一致
        self._preference_selector = preference_selector or PreferenceSelector()
        # 护栏判定器按会话累积状态，须跨 Agent 实例共享（与熔断注册表同理）
        self._sequencing = sequencing or SequencingTracker()
        self._loop_detector = loop_detector or LoopDetector(
            repeat_threshold=settings.loop_repeat_threshold,
        )
        self._search_factory.bind_harness(self._sequencing, self._loop_detector)
        self._trade_factory.bind_harness(self._sequencing, self._loop_detector)

    def _resilience(self) -> list:
        """工具中间件链。

        洋葱顺序：Harness 在外、Resilience 在内，先检查顺序、再进入超时熔断；
        返回后比较参数与结果进展。被硬拒的调用不会占用一次熔断名额。
        """
        chain: list = []
        if self._settings.harness_enabled:
            chain.append(
                HarnessToolMiddleware(
                    sequencing=self._sequencing,
                    loop_detector=self._loop_detector,
                    bus=self._bus,
                ),
            )
        chain.append(ToolResilienceMiddleware(self._circuit_registry, self._bus))
        return chain

    def _memory_middlewares(self):
        from app.application.agents.tool_confirmation import MemoryPermissionMiddleware
        if not getattr(self._preference_store, "semantic_memory", False):
            return [MemoryPermissionMiddleware(self._preference_store)]
        from app.application.memory.middleware import BuyerMemoryMiddleware
        return [MemoryPermissionMiddleware(self._preference_store), BuyerMemoryMiddleware(self._preference_store, self._settings.preference_top_k)]

    def build(self, restored_state: Optional[AgentState] = None) -> Agent:
        prompts = load_prompts()["main_agent"]

        tools = [
            # 1. 业务工具：与子 Agent 同一批，主 Agent 可单干
            *self._search_factory.build_tools(),
            *self._trade_factory.build_tools(),
            # 2. 内置 Task 计划工具（is_state_injected，挂 AgentState.tasks_context）
            TaskCreate(),
            TaskUpdate(),
            TaskList(),
            TaskGet(),
            # 3. SubAgent as Tool 调度（is_concurrency_safe 默认为 True，
            #    主 Agent 同一轮发起的多个派发会被 2.0 并发批执行）
            FunctionTool(
                build_task_dispatch_tool(
                    self._search_factory,
                    self._trade_factory,
                    self._bus,
                    preference_store=self._preference_store,
                    preference_selector=self._preference_selector,
                    preference_top_k=self._settings.preference_top_k,
                    subagent_inject=self._settings.preference_subagent_inject,
                ),
                is_concurrency_safe=True,
                middlewares=self._resilience(),
            ),
            # 4. 长期记忆写路径
            FunctionTool(
                build_remember_preference_tool(self._preference_store, self._bus),
                middlewares=self._resilience(),
            ),
            FunctionTool(build_update_preference_tool(self._preference_store, self._bus),
                         middlewares=self._resilience()),
            # 5. 长期记忆撤回路径（买家说“以后不用避开塑料了”）
            FunctionTool(
                build_forget_preference_tool(self._preference_store, self._bus),
                middlewares=self._resilience(),
            ),
        ]

        system_prompt = prompts["system_prompt"]
        if self.shopping_form_store is not None:
            from app.application.tools.shopping_form_tool import build_shopping_form_tool
            from app.infrastructure.shopping_forms import ClarificationRequest
            tools.append(FunctionTool(build_shopping_form_tool(self.shopping_form_store, self._bus),
                                      input_schema=ClarificationRequest,
                                      middlewares=self._resilience()))
            system_prompt += ("\n选购需求澄清使用 show_shopping_form：缺少影响选择的关键条件，或买家明确要求表单澄清时调用，"
                              "由你根据每次上下文生成 questions：问题文字、顺序、输入类型、选项、说明和是否必填均由你决定，"
                              "前端只渲染，系统不会自动添加预算、选购目标或旅行字段。仅询问相关未知内容，不重复已知信息。"
                              "预算必须说明币种、数量及是否含税运；不要替买家预选答案，不知道的允许留空或选不确定。"
                              "调用后提示买家填写并结束本轮，不基于未提交条件继续搜索。"
                              "提交后以同一会话的新买家消息继续，重新核验商品/SKU、库存、报价和适用限制。"
                              "航司名称或买家填写的尺寸不等于已核实的行李政策；缺少航线/舱位信息或商品尺寸证据时继续澄清。"
                              "重量优先级只是本次取舍，不是具体重量上限。表单不写长期记忆，也不能批准记忆或订单操作。")
        skill_middlewares = []
        if self._capability_registry is not None:
            available_tools = {tool.name for tool in tools}
            system_prompt += "\n\n" + (STABLE_CAPABILITY_POLICY if self.skill_catalog_mode == "append_only"
                                        else capability_hint(self._capability_registry, available_tools))
            tools.extend(FunctionTool(tool, is_read_only=True, middlewares=self._resilience())
                         for tool in build_capability_tools(self._capability_registry, available_tools, self._bus, self.buyer_skill_store))
            if self.skill_catalog_mode == "append_only":
                skill_middlewares.append(SkillCatalogMiddleware(
                    self._capability_registry, self.buyer_skill_store, available_tools))

        return allow_business_tools(
            ContextAwareAgent(
                name=prompts["name"],
                system_prompt=system_prompt,
                model=create_chat_model(self._settings, throttle=self._throttle, bus=self._bus, throttle_factory=self._throttle_factory),
                toolkit=Toolkit(tools=tools),
                middlewares=build_agent_middlewares(self._settings) + self._memory_middlewares() + skill_middlewares,
                context_config=build_context_config(
                    self._settings.context_size,
                    self._settings.tool_result_limit,
                ),
                state=restored_state,
                react_config=ReActConfig(max_iters=15),
            ),
        )


class SessionRegistry:
    """按 shopping_session_id 缓存 MainAgent 实例，支撑多轮对话；
    AgentState 经 SessionStore 端口落盘（SQLite 或文件），服务重启后恢复。"""

    def __init__(self, main_factory: MainAgentFactory, session_store: SessionStore, *, enforce_owner: bool = True, prompt_registry=None, model_registry=None) -> None:
        self._main_factory = main_factory
        self._session_store = session_store
        self._agents: dict[str, Agent] = {}
        self._claims = {}
        self._enforce_owner = enforce_owner
        self._prompt_registry = prompt_registry
        self._model_registry = model_registry

    async def get_or_create(self, shopping_session_id: str, *, apply_model_selection: bool = True, model_override: dict | None = None) -> Agent:
        from app.infrastructure.context import ShoppingContext
        from app.domain.session.ports.session_store import SessionOwnerMismatch, SessionStateCorrupt
        context = ShoppingContext.current()
        if context is None or context.shopping_session_id != shopping_session_id:
            raise SessionOwnerMismatch("会话执行缺少可信的当前买家上下文")
        try:
            claim = await self._session_store.claim(shopping_session_id, buyer_id=context.buyer_id, enforce_owner=self._enforce_owner)
            ShoppingContext.set_session_fence(claim.fence)
            mode = getattr(self._main_factory, "skill_catalog_mode", "legacy")
            ShoppingContext.set_skill_catalog_mode(mode)
            # 在恢复 AgentState 之前校验资料版本；变更后阻断旧正文继续参与模型上下文。
            capabilities = getattr(self._main_factory, "capability_registry", None)
            if capabilities is not None:
                import asyncio
                digest = await asyncio.to_thread(capabilities.bind_session, shopping_session_id, context.buyer_id,
                                                **({"allow_skill_updates": True} if mode == "append_only" else {}))
                ShoppingContext.set_capability_digest(digest)
                record_prompt_assignment()
            if self._prompt_registry is not None:
                assignment = await self._prompt_registry.assign(shopping_session_id, context.buyer_id)
                ShoppingContext.set_prompt_assignment(assignment)
                record_prompt_assignment()
            try:
                restored = AgentState.model_validate_json(claim.state_json) if claim.state_json is not None else None
            except Exception as error:
                raise SessionStateCorrupt("持久会话快照损坏，未按空会话覆盖历史") from error
            snapshot = None
            if self._model_registry is not None:
                binding = await self._session_store.model_binding(shopping_session_id, context.buyer_id)
                saved = restored.middle_context.get('shoppilot_model') if restored else None
                awaiting = bool(restored and restored.context and restored.get_awaiting_tool_calls(restored.context[-1].name))
                # 原生 ASK 恢复必须使用产生该审批的配置，不消费待选模型。
                from app.application.agents.model_selection import resolve_session_snapshot
                snapshot = await resolve_session_snapshot(self._model_registry, context.buyer_id,
                    binding, restored, allow_pending=apply_model_selection)
                if model_override is not None:
                    if awaiting:
                        raise SessionStateCorrupt("自动化不能替用户恢复待确认操作")
                    snapshot = model_override
                    if restored:
                        await self.check_model_capacity(shopping_session_id, context.buyer_id, snapshot, restored)
                ShoppingContext.set_model(snapshot,
                    await self._model_registry.runtime_for_snapshot(snapshot, buyer_id=context.buyer_id))
                if restored and snapshot != saved and binding['pending'] == snapshot:
                    await self.check_model_capacity(shopping_session_id, context.buyer_id, snapshot, restored)
                await self._session_store.bind_run_model(claim, snapshot,
                    selection_revision=binding['revision'], consume_pending=apply_model_selection and not awaiting and model_override is None)
            previous = self._claims.get(shopping_session_id)
            cached = self._agents.get(shopping_session_id)
            changed = snapshot is not None and cached is not None and cached.state.middle_context.get('shoppilot_model') != snapshot
            if cached is None or previous is None or previous.revision != claim.revision or changed:
                if cached is not None:
                    from app.application.agents.model_selection import close_agent_model
                    await close_agent_model(cached)
                self._agents[shopping_session_id] = self._main_factory.build(restored)
            if snapshot is not None:
                self._agents[shopping_session_id].state.middle_context['shoppilot_model'] = snapshot
            self._claims[shopping_session_id] = claim
            return self._agents[shopping_session_id]
        except BaseException:
            await self.invalidate(shopping_session_id)
            raise

    async def invalidate(self, shopping_session_id: str) -> None:
        """取得跨进程执行权后丢弃本地缓存，下一次读取恢复持久状态。"""
        agent = self._agents.pop(shopping_session_id, None)
        if agent is not None:
            from app.application.agents.model_selection import close_agent_model
            await close_agent_model(agent)
        self._claims.pop(shopping_session_id, None)

    async def check_model_capacity(self, session, buyer, snapshot, state):
        from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
        from app.application.agents.model_selection import check_capacity, close_agent_model
        token = ShoppingContext.set(ShoppingContextSnapshot(session, buyer, 'zh-CN', 'CNY'))
        agent = None
        try:
            ShoppingContext.set_model(snapshot,
                await self._model_registry.runtime_for_snapshot(snapshot, buyer_id=buyer))
            if self._prompt_registry is not None:
                assignment = await self._prompt_registry.existing_assignment(session, buyer)
                if assignment is not None:
                    ShoppingContext.set_prompt_assignment(assignment)
            ShoppingContext.set_skill_catalog_mode(getattr(self._main_factory, 'skill_catalog_mode', 'legacy'))
            agent = self._main_factory.build(state.model_copy(deep=True))
            await check_capacity(agent)
        finally:
            try:
                if agent is not None:
                    await close_agent_model(agent)
            finally:
                ShoppingContext.reset(token)

    async def persist(self, shopping_session_id: str) -> bool:
        """捕获本轮票据后执行 CAS，旧执行者无法覆盖新 owner 的状态。"""
        agent = self._agents.get(shopping_session_id)
        claim = self._claims.get(shopping_session_id)
        if agent is None or claim is None:
            return False
        try:
            saved = await self._session_store.save_claim(claim, agent.state.model_dump_json())
            if self._claims.get(shopping_session_id) == claim:
                self._claims[shopping_session_id] = saved
            return True
        except BaseException:
            # 包括取消；丢弃缓存后下一轮必须重新读取，不把失败保存伪装为成功。
            if self._claims.get(shopping_session_id) == claim:
                await self.invalidate(shopping_session_id)
            raise
