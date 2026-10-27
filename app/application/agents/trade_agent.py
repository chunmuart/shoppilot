# -*- coding: utf-8 -*-
"""TradeAgent

下单交易专家。基于 AgentScope 2.0 Agent，工具集：
create_order_tool / query_order_tool / cancel_order_tool。

同 SearchAgent 一样，通过 task_dispatch 工具被 MainAgent 调度，每次调度新建独立实例；
`build_tools()` 同时供 MainAgent 复用——主 Agent 持有同一批业务工具，可以不派发自己单干。
"""
from __future__ import annotations
from app.infrastructure.context_governance import ContextAwareAgent

from agentscope.agent import Agent, ReActConfig
from agentscope.tool import FunctionTool, Toolkit

from app.application.agents.context_policy import build_context_config
from app.application.agents.permissions import allow_business_tools
from app.application.prompts.loader import load_prompts
from app.application.tools.order_tools import (
    build_cancel_order_tool,
    build_create_order_tool,
    build_query_order_tool,
)
from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.infrastructure.llm import create_chat_model
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.resilience import (
    CircuitBreakerRegistry,
    ToolResilienceMiddleware,
)
from app.infrastructure.settings import Settings
from app.infrastructure.tracing import build_agent_middlewares
from app.application.harness.assertions import SequencingTracker
from app.application.harness.loop_detector import LoopDetector
from app.infrastructure.harness_middleware import HarnessToolMiddleware


class TradeAgentFactory:
    def __init__(
        self,
        settings: Settings,
        place_order: PlaceOrderUseCase,
        query_order: QueryOrderUseCase,
        cancel_order: CancelOrderUseCase,
        bus: TradeEventBus,
        circuit_registry: CircuitBreakerRegistry,
        throttle: GatewayThrottle,
        throttle_factory=None,
    ) -> None:
        self._settings = settings
        self._place_order = place_order
        self._query_order = query_order
        self._cancel_order = cancel_order
        self._bus = bus
        # 与搜索工厂共享持久证据文件，主/子 Agent 和恢复会话均按买家、会话核对。
        self.evidence_store = ContextEvidenceStore(settings.data_dir / "context_evidence.db")
        self._circuit_registry = circuit_registry
        self._throttle = throttle
        self._throttle_factory = throttle_factory
        self.bind_harness(SequencingTracker(), LoopDetector(repeat_threshold=settings.loop_repeat_threshold))

    def bind_harness(self, sequencing, loop_detector) -> None:
        """与搜索及主 Agent 共享顺序和循环状态；不改变原生权限审批。"""
        self._sequencing, self._loop_detector = sequencing, loop_detector

    def _resilience(self) -> list:
        chain = [HarnessToolMiddleware(sequencing=self._sequencing,
            loop_detector=self._loop_detector, bus=self._bus)] if self._settings.harness_enabled else []
        return [*chain, ToolResilienceMiddleware(self._circuit_registry, self._bus)]

    def build_tools(self) -> list[FunctionTool]:
        """TradeAgent 的业务工具集，MainAgent 单干时持有同一批（均带超时+熔断保护）。"""
        return [
            FunctionTool(
                build_create_order_tool(self._place_order, self._bus, self.evidence_store),
                middlewares=self._resilience(),
            ),
            FunctionTool(
                build_query_order_tool(self._query_order, self._bus),
                is_read_only=True,
                middlewares=self._resilience(),
            ),
            FunctionTool(
                build_cancel_order_tool(self._cancel_order, self._bus),
                middlewares=self._resilience(),
            ),
        ]

    def build(self) -> Agent:
        prompts = load_prompts()["sub_agents"]["trade"]
        return allow_business_tools(
            ContextAwareAgent(
                name=prompts["name"],
                system_prompt=prompts["system_prompt"],
                model=create_chat_model(self._settings, throttle=self._throttle, bus=self._bus, throttle_factory=self._throttle_factory),
                toolkit=Toolkit(tools=list(self.build_tools())),
                middlewares=build_agent_middlewares(self._settings),
                context_config=build_context_config(
                    self._settings.context_size,
                    self._settings.tool_result_limit,
                ),
                react_config=ReActConfig(max_iters=6),
            ),
        )
