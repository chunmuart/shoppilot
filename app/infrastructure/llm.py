# -*- coding: utf-8 -*-
"""llm

统一创建 AgentScope 2.0 大模型对象。全项目只从这里拿 model，
主 / 子 Agent 各自持有独立实例。

2.0 的模型接入方式：OpenAICredential（携带 api_key + base_url，天然支持
OpenAI 兼容网关）→ OpenAIChatModel(credential=..., model=...)。

四期在此加两层框架不覆盖的东西：
    1. 配额闸门：闸门必须持有到「流耗尽」。流式调用返回的是异步生成器，
       若在 `async with slot()` 内直接 return，名额会在数据还没读完时释放，
       限流等于没做；
    2. 限流回退：实测网关配额池紧张时主模型单发也会 429，退避重试用尽后换用
       备用模型，并发 model.fallback 事件如实告知，不静默降级。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from copy import copy
from collections.abc import AsyncIterable
from contextvars import ContextVar
from typing import Any, AsyncGenerator, Optional, Callable

from agentscope.credential import OpenAICredential
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatResponse, FinishedReason, OpenAIChatModel
from agentscope.tool import ToolChoice

from app.infrastructure.budget import current_tier, get_budget, MINIMAL_MODE_HINT, estimate_input_tokens, rule_fallback_text
from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.model_stream import StreamFinalizer, current_stream_finalizer
from app.infrastructure.settings import Settings
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.transient import is_transient_error
from app.infrastructure.operational_metrics import observe_model, observe_model_started
from app.infrastructure.prompt_cache import (
    CacheAttempt, CacheRejected, PromptCacheFormatter, UsageCapturingStream, PrefixTracker,
    attach_usage, cache_attempt, cache_disabled, cache_retry_target,
    is_cache_rejection, read_cache_usage,
)

logger = logging.getLogger(__name__)


class BudgetCall:
    """一个逻辑调用的逐次请求预算；失败重试也要重新预留。"""
    def __init__(self, messages, tools, kwargs):
        self.budget = get_budget()
        self.input_tokens = estimate_input_tokens(messages, tools) if self.budget else 0
        self.base_input_tokens = self.input_tokens
        self.minimal_hint_tokens = estimate_input_tokens(
            [Msg(name="system", content=[TextBlock(text=MINIMAL_MODE_HINT)], role="system")], None,
        ) if self.budget else 0
        self.output_limit = min(1024, int(kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 1024))
        self.reservation = None
        self.tier = current_tier()
        self.maximum_output = self.output_limit
        self.started_at = None
        self.first_text_at = None
        self.last_usage_response = None
        self.cache_attempt = None
        self.started_wall_ns = None
        self.streamed = False
        self.upstream_usage = None
        self.model_identity = None

    def mark_started(self):
        observe_model_started()
        self.started_at = time.monotonic()
        self.first_text_at = None
        self.last_usage_response = None
        self.cache_attempt = None
        self.started_wall_ns = time.time_ns()
        self.streamed = False
        self.upstream_usage = None
        self.model_identity = None

    def observe_chunk(self, response):
        if _usage_tokens(response) is not None:
            self.last_usage_response = response
        if self.first_text_at is None and any(isinstance(block, TextBlock) and block.text for block in (_safe_field(response, "content") or [])):
            self.first_text_at = time.monotonic()

    def acquire(self) -> bool:
        if self.budget is None:
            return True
        self.tier = self.budget.tier
        self.input_tokens = self.base_input_tokens + (self.minimal_hint_tokens if self.tier == "minimal" else 0)
        self.maximum_output = min(self.output_limit, self.budget.remaining - self.input_tokens)
        if self.maximum_output < 64:
            return False
        self.reservation = self.budget.reserve(self.input_tokens + self.maximum_output)
        return self.reservation is not None

    def settle(self, response) -> None:
        self.observe_chunk(response)
        usage_response = self.last_usage_response or response
        if self.upstream_usage is not None:
            # 协议违约发生在SDK解析前，也必须结算这次已经产生的真实消耗。
            usage_response = {'usage': self.upstream_usage}
        if self.reservation is not None:
            self.reservation.settle(_usage_tokens(usage_response))
            self.reservation = None
        if self.started_at is not None:
            usage = _safe_field(usage_response, "usage")
            input_tokens = _safe_field(usage, "input_tokens")
            output_tokens = _safe_field(usage, "output_tokens")
            observe_model(input_tokens=input_tokens if input_tokens is not None else _safe_field(usage, "prompt_tokens"),
                          output_tokens=output_tokens if output_tokens is not None else _safe_field(usage, "completion_tokens"),
                          ttft_ms=(self.first_text_at-self.started_at)*1000 if self.first_text_at is not None else None,
                          elapsed_ms=(time.monotonic()-self.started_at)*1000, cost_usd=None)
            from app.infrastructure.context_usage import record_context_usage
            record_context_usage(input_tokens if input_tokens is not None else _safe_field(usage, "prompt_tokens"),
                                 output_tokens if output_tokens is not None else _safe_field(usage, "completion_tokens"),
                                 (time.monotonic()-self.started_at)*1000,
                                 cache=self.cache_attempt.metrics() if self.cache_attempt else None,
                                 start_time=self.started_wall_ns,
                                 ttft_ms=(self.first_text_at-self.started_at)*1000
                                 if self.streamed and self.first_text_at is not None else None,
                                 model_identity=self.model_identity)
            self.started_at = None


_budget_call: ContextVar[BudgetCall | None] = ContextVar("shoppilot_budget_call", default=None)
_structured_call: ContextVar[bool] = ContextVar("shoppilot_structured_call", default=False)
_structured_finalizers: ContextVar[list | None] = ContextVar("shoppilot_structured_finalizers", default=None)


class StreamClosingOpenAIChatModel(OpenAIChatModel):
    """捕获 SDK 生成器内部的 HTTP stream，让尚未开始读取的流也能显式关闭。"""

    def __init__(self, *, runtime=None, **kwargs):
        super().__init__(**kwargs)
        self._shoppilot_runtime = runtime
        self._shoppilot_counter_metadata = {'method': 'agentscope_estimate'}
        self._prefix_tracker = PrefixTracker()
        # 在原生 SDK 最终 create 参数处观察，包含 formatter 与工具 schema 的实际输出。
        # 业务只记指纹；隔离评测可显式安装本地原文证据 sink，不改变供应商参数。
        create = self.client.chat.completions.create
        async def observed_create(*args, **request):
            from app.infrastructure.context_usage import record_evaluation_evidence
            record_evaluation_evidence('model_request', {key: request[key] for key in
                ('model', 'messages', 'tools', 'tool_choice', 'response_format', 'temperature', 'max_tokens', 'max_completion_tokens', 'stream', 'stream_options')
                if key in request and isinstance(request[key], (dict, list, str, int, float, bool))})
            attempt = cache_attempt.get()
            if attempt is not None:
                from app.infrastructure.context_usage import context_call_kind
                ctx = ShoppingContext.current()
                scope = (ctx.buyer_id, ctx.shopping_session_id) if ctx else None
                attempt.prefix = self._prefix_tracker.observe(request, scope=scope, kind=context_call_kind.get())
            from app.infrastructure.model_protocol import ResponseContract, ValidatedStream
            call = _budget_call.get()
            from app.infrastructure.model_recovery import response_observation
            observation = response_observation.get()
            response_ids = set()
            def observe(*, response=None, protocol_status=None):
                if observation is not None:
                    observation.observe(response, protocol_status)
                if response is not None:
                    response_id = _safe_field(response, 'id')
                    if isinstance(response_id, str):
                        response_ids.add(response_id)
                    if call is not None and _safe_field(response, 'usage') is not None:
                        call.upstream_usage = response.usage
                    if attempt is not None:
                        returned = _safe_field(response, 'model')
                        if returned:
                            attempt.response_models.add(returned)
                        usage = _safe_field(response, 'usage')
                        if usage is not None:
                            attempt.usage = read_cache_usage(usage)
                    if call is not None:
                        from app.infrastructure.model_counting import execution_identity
                        call.model_identity = execution_identity(self, attempt.response_models if attempt else ())
                if protocol_status is not None:
                    if attempt is not None:
                        attempt.protocol_status = protocol_status
                    record_evaluation_evidence('model_response_contract', {
                        'requested_model': request.get('model'),
                        'response_models': sorted(attempt.response_models) if attempt else [],
                        'response_ids': sorted(response_ids),
                        'protocol_status': protocol_status})
            contract = ResponseContract(request, observe)
            response = await create(*args, **request)
            return ValidatedStream(response, contract) if request.get('stream') else contract.completion(response)
        self.client.chat.completions.create = observed_create

    async def count_tokens(self, messages, tools=None):
        from app.infrastructure.model_counting import local_token_count, CounterUnavailable
        try:
            count = await local_token_count(self, messages, tools)
            self._shoppilot_counter_metadata = {'method': 'local_chat_template'}
            return count
        except (CounterUnavailable, ImportError, OSError, ValueError, TypeError, KeyError) as error:
            # 原因只用枚举，不把本机路径、模型配置或买家输入写进接口。
            reason = str(error) if isinstance(error, CounterUnavailable) else 'tokenizer_unavailable'
            self._shoppilot_counter_metadata = {'method': 'agentscope_estimate', 'fallback_reason': reason}
            return await super().count_tokens(messages, tools)

    def _attach_model_identity(self, response, attempt):
        from app.infrastructure.model_counting import execution_identity
        if isinstance(response, ChatResponse):
            response.metadata['model_execution'] = execution_identity(self, attempt.response_models if attempt else ())
        return response

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        if self._shoppilot_runtime:
            from app.infrastructure.model_counting import count_model_input, input_limits
            from app.infrastructure.context_governance import ContextCapacityError
            maximum = self._shoppilot_runtime.get('max_output_tokens')
            requested = kwargs.get('max_completion_tokens') or kwargs.get('max_tokens') or self.parameters.max_tokens or 8192
            output = min(int(requested), int(maximum)) if maximum else int(requested)
            kwargs.pop('max_tokens', None)
            kwargs['max_completion_tokens'] = output
            estimate = await count_model_input(self, messages, tools)
            if estimate['estimated_input_tokens'] > input_limits(self, output)['input_budget']:
                raise ContextCapacityError('所选模型的安全输入空间不足，请整理上下文或选择更大窗口；未发送模型请求')
        if self.stream and getattr(tool_choice, 'mode', None) == 'product_search_tool':
            # 兼容网关的强制函数参数流可能缺少完整结束标记；该只读取证使用完整响应。
            # 请求局部副本保留原生解析、配额和计量，不切换模型、不重试，也不改共享流式配置。
            request_model = copy(self)
            request_model.stream = False
            return await request_model._call_api(model_name, messages, tools, tool_choice, **kwargs)
        formatter = self.formatter
        attempt = CacheAttempt(model_name, getattr(formatter, "mode", "passthrough"),
                               getattr(formatter, "policy", "static"), cache_disabled.get())
        if attempt.retry_without_cache:
            attempt.reason = "parameter_rejected_retry"
        call = _budget_call.get()
        if call is not None:
            call.cache_attempt = attempt
            from app.infrastructure.model_counting import execution_identity
            call.model_identity = execution_identity(self)
        token = cache_attempt.set(attempt)
        try:
            return await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)
        except Exception as error:
            if attempt.marker_count and is_cache_rejection(error):
                attempt.reason = "parameter_rejected"
                raise CacheRejected(model_name) from None
            raise
        finally:
            cache_attempt.reset(token)

    def _parse_completion_response(self, start_datetime, response, audio_format="wav"):
        attempt = cache_attempt.get()
        if attempt is not None:
            attempt.usage = read_cache_usage(response.usage)
        parsed = super()._parse_completion_response(start_datetime, response, audio_format)
        return self._attach_model_identity(attach_usage(parsed, attempt), attempt)

    def _parse_stream_response(self, start_datetime, response):
        attempt = cache_attempt.get()
        captured = UsageCapturingStream(response, attempt) if attempt else response
        parsed = super()._parse_stream_response(start_datetime, captured)
        async def decorated():
            async for chunk in parsed:
                yield self._attach_model_identity(attach_usage(chunk, attempt), attempt)
        wrapped = decorated()
        finalizer = current_stream_finalizer.get()
        if finalizer is not None:
            finalizer.add(response)
            finalizer.add(parsed)
            finalizer.add(wrapped)
        return wrapped


class ThrottledChatModel(StreamClosingOpenAIChatModel):
    """带配额闸门、退避重试与限流回退的 OpenAIChatModel。"""

    def __init__(
        self,
        *,
        throttle: GatewayThrottle,
        fallback: Optional[OpenAIChatModel] = None,
        max_transient_retries: int = 2,
        retry_base_seconds: float = 6.0,
        bus: Optional[TradeEventBus] = None,
        recovery_mode: str = 'off',
        fallback_throttle: GatewayThrottle | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._throttle = throttle
        self._fallback = fallback
        self._fallback_throttle = fallback_throttle
        self._max_transient_retries = max_transient_retries
        self._retry_base_seconds = retry_base_seconds
        self._bus = bus
        if recovery_mode not in {'off', 'bounded'}:
            raise ValueError('MODEL_RECOVERY_MODE 只支持 off / bounded')
        self._recovery_mode = recovery_mode

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        from app.infrastructure.context_usage import context_call_kind
        if self.stream and context_call_kind.get() == "summary":
            # 摘要只在结构校验通过后提交，不向买家逐段展示。
            # 完整响应避免兼容网关遗漏函数流结束标记；保留严格工具校验与计量。
            # 用本次请求副本，不能把同一模型后续的正常聊天改成非流式。
            request_model = copy(self)
            request_model.stream = False
            return await request_model.generate_structured_output(messages, structured_model, **kwargs)
        # SDK 2.0.8 的结构化生成直接调用 _call_api，不经过 __call__。
        token = _structured_call.set(True)
        resources = []
        resource_token = _structured_finalizers.set(resources)
        cache_token = cache_disabled.set(cache_disabled.get())
        try:
            return await super().generate_structured_output(messages, structured_model, **kwargs)
        finally:
            try:
                for resource in reversed(resources):
                    await resource.close(sys.exc_info())
            finally:
                _structured_finalizers.reset(resource_token)
                _structured_call.reset(token)
                cache_disabled.reset(cache_token)

    async def _call_api_with_structured_output(self, *args, **kwargs):
        # 2.0.8 会在 forced/auto/none 策略间回退。每次解析完成必须先关闭
        # 上一次流；否则解析失败时它仍占着唯一闸门名额，下一次请求会自锁。
        resources = _structured_finalizers.get()
        boundary = len(resources) if resources is not None else 0
        try:
            return await super()._call_api_with_structured_output(*args, **kwargs)
        finally:
            if resources is not None:
                owned = resources[boundary:]
                del resources[boundary:]
                for resource in reversed(owned):
                    await resource.close(sys.exc_info())

    async def _call_api_with_structured_output(self, *args, **kwargs):
        # 2.0.8 会在 forced/auto/none 策略间回退。每次解析完成必须先关闭
        # 上一次流；否则解析失败时它仍占着唯一闸门名额，下一次请求会自锁。
        resources = _structured_finalizers.get()
        boundary = len(resources) if resources is not None else 0
        try:
            return await super()._call_api_with_structured_output(*args, **kwargs)
        finally:
            if resources is not None:
                owned = resources[boundary:]
                del resources[boundary:]
                for resource in reversed(owned):
                    await resource.close(sys.exc_info())

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        if not _structured_call.get():
            return await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)
        try:
            return await self._structured_api_attempt(model_name, messages, tools, tool_choice, **kwargs)
        except CacheRejected:
            # 已结束并结算被拒绝的请求，重新获取共享闸门和预算；只重试模型请求。
            # 整个结构化生成的后续 forced/auto/none 尝试都不再重复探测；外层 finally 恢复。
            cache_disabled.set(True)
            return await self._structured_api_attempt(model_name, messages, tools, tool_choice, **kwargs)

    async def _structured_api_attempt(self, model_name, messages, tools, tool_choice, **kwargs):
        # 在 SDK 添加 schema 和提示后计预算；每个真实请求独立计量，
        # 包括 SDK 的 tool_choice 兼容重试以及输出格式校验失败的请求。
        call = BudgetCall(messages, tools, kwargs)
        if not call.acquire():
            raise RuntimeError("本轮预算不足，保留最后有效摘要")
        if call.budget is not None:
            kwargs.pop("max_tokens", None)
            kwargs["max_completion_tokens"] = call.maximum_output
        slot = self._throttle.slot()
        try:
            await slot.__aenter__()
        except BaseException:
            if call.reservation is not None:
                call.reservation.settle(0)
            raise
        finalizer = StreamFinalizer(slot, call.settle)
        _structured_finalizers.get().append(finalizer)
        token = current_stream_finalizer.set(finalizer)
        budget_token = _budget_call.set(call)
        try:
            call.mark_started()
            result = await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)
            if isinstance(result, AsyncIterable):
                finalizer.add(result)
                finalizer.bind_current_task()
                wrapped = self._release_after_stream(finalizer, result, call)
                await anext(wrapped)
                return wrapped
            finalizer.last = result
            _raise_if_interrupted(result)
        except BaseException:
            await finalizer.close(sys.exc_info())
            raise
        finally:
            current_stream_finalizer.reset(token)
            _budget_call.reset(budget_token)
        await finalizer.close()
        return result

    async def __call__(  # type: ignore[override]
        self, messages, tools=None, tool_choice=None, **kwargs,
    ):
        from app.infrastructure.model_recovery import recover_response
        from app.infrastructure.context_usage import context_call_kind
        return await recover_response(self, messages, tools, tool_choice, kwargs,
            enabled=self._recovery_mode == 'bounded' and context_call_kind.get() == 'business')

    async def _call_once(self, messages, tools=None, tool_choice=None, **kwargs):
        try:
            return await self._call_throttled(messages, tools, tool_choice, **kwargs)
        except CacheRejected as error:
            disabled = cache_disabled.set(True)
            target = cache_retry_target.set(error.model)
            try:
                return await self._call_throttled(messages, tools, tool_choice, **kwargs)
            finally:
                cache_retry_target.reset(target)
                cache_disabled.reset(disabled)

    async def _call_throttled(
        self,
        messages: list[Msg],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[ToolChoice] = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        budget_call = BudgetCall(messages, tools, kwargs)
        if not budget_call.acquire():
            return self._budget_fallback()
        if budget_call.budget is not None:
            kwargs.pop("max_tokens", None)
            kwargs["max_completion_tokens"] = budget_call.maximum_output
        # 手动进出上下文而非 async with：流式分支要把名额移交给包装生成器
        slot = self._throttle.slot()
        try:
            await slot.__aenter__()
        except BaseException:
            if budget_call.reservation is not None:
                budget_call.reservation.settle(0)  # 尚未请求上游，没有模型成本。
            raise
        finalizer = StreamFinalizer(slot, budget_call.settle)
        token = current_stream_finalizer.set(finalizer)
        budget_token = _budget_call.set(budget_call)
        try:
            result = await self._call_with_fallback(messages, tools, tool_choice, **kwargs)
            # ChatResponse 继承 DictMixin，hasattr 会把缺失字段变成 KeyError。
            # 按类型检查异步迭代协议，且在退出异常保护前识别 SDK 吞掉的取消。
            if isinstance(result, AsyncIterable):
                finalizer.add(result)
                finalizer.bind_current_task()
                wrapped = self._release_after_stream(finalizer, result, budget_call)
                # AgentScope 要求原生 async generator。预启动只消费内部标记，不等首个 token；
                # finally 先就绪，调用方尚未读流就 aclose 也会释放名额和 HTTP 连接。
                await anext(wrapped)
                return wrapped
            _raise_if_interrupted(result)
        except BaseException:
            await finalizer.close(sys.exc_info())
            raise
        finally:
            current_stream_finalizer.reset(token)
            _budget_call.reset(budget_token)

        finalizer.last = result
        await finalizer.close()
        return result

    def _budget_fallback(self) -> ChatResponse:
        budget = get_budget()
        if budget is not None:
            budget.fallback_used = True
        if self._bus is not None:
            self._bus.publish(ShoppingContext.current_session_id(), "model.fallback", {
                "from": self.model, "to": "rules", "reason": "本轮预算不足以开始新的模型调用",
                "budget_tier": "fallback",
            })
        return ChatResponse(content=[TextBlock(text=rule_fallback_text())], is_last=True,
                            metadata={"budget_fallback": True})

    @staticmethod
    async def _release_after_stream(finalizer: StreamFinalizer, stream: Any, budget_call: BudgetCall) -> AsyncGenerator[ChatResponse, None]:
        """把闸门名额持有到流真正读完（含调用方提前中断的情况）。"""
        try:
            yield None  # type: ignore[misc]  # 内部启动标记仅由 __call__ 消费。
            finalizer.bind_current_task()
            budget_call.streamed = True
            async for chunk in stream:
                finalizer.last = chunk
                budget_call.observe_chunk(chunk)
                _raise_if_interrupted(chunk)
                yield chunk
        finally:
            await finalizer.close(sys.exc_info())

    async def _invoke_upstream(
        self,
        messages: list[Msg],
        tools: Optional[list[dict]],
        tool_choice: Optional[ToolChoice],
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """真正打上游的一跳。抽成方法便于替换与测试。

        同时是四档预算降级（16-4 章）的作用点：
        预算充足（main）走主模型；剩余不足时切到更便宜的备用模型，
        并发 model.fallback 事件如实告知——**降级不静默**。
        minimal 档额外注入简洁模式提示，压住 Think 长度。
        """
        call = _budget_call.get()
        tier = call.tier if call is not None else current_tier()
        from app.infrastructure.context_usage import context_call_kind
        if tier != "main" and context_call_kind.get() != 'summary':
            # 使用预留之前确定的档位；预留本身会降低 remaining，不能因此漏掉 hint。
            hint = MINIMAL_MODE_HINT if tier == "minimal" else None
            if hint:
                messages = [*messages, Msg(name="system", content=[TextBlock(text=hint)], role="system")]
            if self._fallback is not None:
                logger.info("Token 预算档位 %s，切用备用模型 %s", tier, self._fallback.model)
                self._publish_budget_tier(tier)
                return await self._invoke_fallback(messages, tools, tool_choice, **kwargs)
        if self._fallback is not None and cache_retry_target.get() == self._fallback.model:
            return await self._invoke_fallback(messages, tools, tool_choice, **kwargs)
        return await super().__call__(messages, tools, tool_choice, **kwargs)

    async def _invoke_fallback(self, messages, tools, tool_choice, **kwargs):
        # 回退有自己的窗口和计数合同，不能拿主模型通过的容量检查作为授权。
        if isinstance(self._fallback, OpenAIChatModel):
            from app.infrastructure.model_counting import count_model_input, input_limits
            from app.infrastructure.context_governance import ContextCapacityError
            count = await count_model_input(self._fallback, messages, tools)
            output = kwargs.get('max_completion_tokens') or kwargs.get('max_tokens')
            if count['estimated_input_tokens'] > input_limits(self._fallback, output)['input_budget']:
                raise ContextCapacityError('备用模型窗口不足，未发送回退请求；请整理上下文或改用更大窗口')
        throttle = self._fallback_throttle
        if throttle is not None and throttle is not self._throttle:
            # 主调用持有共享配额组时无需重复获取；不同供应商必须走自己的闸门。
            slot = throttle.slot()
            await slot.__aenter__()
            class HeldSlot:
                closed = False
                async def aclose(self):
                    if not self.closed:
                        self.closed = True
                        await slot.__aexit__(None, None, None)
            held = HeldSlot()
            finalizer = current_stream_finalizer.get()
            if finalizer is not None:
                finalizer.add(held)
            try:
                response = await self._fallback(messages, tools, tool_choice, **kwargs)
            except BaseException:
                await held.aclose()
                raise
            if not isinstance(response, AsyncIterable):
                await held.aclose()
            elif finalizer is None:
                # 正常业务必有外层 finalizer；单独调用时也要把闸门持有到流关闭。
                async def wrapped():
                    try:
                        yield None
                        async for chunk in response:
                            yield chunk
                    finally:
                        if hasattr(response, 'aclose'):
                            await response.aclose()
                        await held.aclose()
                stream = wrapped()
                await anext(stream)
                return stream
            return response
        return await self._fallback(messages, tools, tool_choice, **kwargs)

    def _publish_budget_tier(self, tier: str) -> None:
        if self._bus is None or self._fallback is None:
            return
        self._bus.publish(
            ShoppingContext.current_session_id(),
            "model.fallback",
            {
                "from": self.model,
                "to": self._fallback.model,
                "reason": f"Token 预算档位 {tier}",
                "budget_tier": tier,
            },
        )

    async def _call_with_fallback(
        self,
        messages: list[Msg],
        tools: Optional[list[dict]],
        tool_choice: Optional[ToolChoice],
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        last_error: Optional[BaseException] = None
        budget_call = _budget_call.get()
        for attempt in range(self._max_transient_retries + 1):
            if attempt and budget_call is not None:
                if not budget_call.acquire():
                    return self._budget_fallback()
                if budget_call.budget is not None:
                    kwargs["max_completion_tokens"] = budget_call.maximum_output
            try:
                if budget_call is not None:
                    budget_call.mark_started()
                return await self._invoke_upstream(messages, tools, tool_choice, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if budget_call is not None:
                    budget_call.settle(None)
                if not is_transient_error(err):
                    raise
                last_error = err
                if attempt < self._max_transient_retries:
                    # 指数退避：网关速率类限流对固定间隔重试不敏感
                    delay = self._retry_base_seconds * (3**attempt)
                    logger.warning(
                        "模型 %s 遇上游瞬时故障，%.0fs 后重试（第 %d/%d 次）：%s",
                        self.model, delay, attempt + 1, self._max_transient_retries, err,
                    )
                    await asyncio.sleep(delay)

        from app.infrastructure.context_usage import context_call_kind
        if self._fallback is None or context_call_kind.get() == 'summary':
            raise last_error  # type: ignore[misc]

        logger.warning("模型 %s 重试用尽，回退到 %s：%s", self.model, self._fallback.model, last_error)
        if budget_call is not None:
            if not budget_call.acquire():
                return self._budget_fallback()
            if budget_call.budget is not None:
                kwargs["max_completion_tokens"] = budget_call.maximum_output
        self._publish_fallback(str(last_error))
        if budget_call is not None:
            budget_call.mark_started()
        return await self._invoke_fallback(messages, tools, tool_choice, **kwargs)

    def _publish_fallback(self, reason: str) -> None:
        if self._bus is None or self._fallback is None:
            return
        session_id = ShoppingContext.current_session_id()
        self._bus.publish(
            session_id,
            "model.fallback",
            {"from": self.model, "to": self._fallback.model, "reason": reason},
        )


def _safe_field(source: Any, name: str) -> Any:
    """宽容取字段。

    坑：ChatResponse.usage 的 `__getattr__` 对缺失字段抛 **KeyError**，
    而 `getattr(obj, name, default)` 只吃 AttributeError——直接用 getattr 带默认值
    依旧会把 KeyError 抛到主链路，把整轮对话搞成 [error]。实测踩过。
    """
    if isinstance(source, dict):
        return source.get(name)
    try:
        return getattr(source, name, None)
    except Exception:  # noqa: BLE001 —— 包含 KeyError 等非标准实现
        return None


def _raise_if_interrupted(response: Any) -> None:
    """SDK 会把 CancelledError 转成响应或尾块；还原取消供请求层终止本轮。"""
    if _safe_field(response, "finished_reason") == FinishedReason.INTERRUPTED:
        raise asyncio.CancelledError("模型调用已中断")


def _usage_tokens(response: Any) -> int | None:
    usage = _safe_field(response, "usage") if response is not None else None
    if usage is None:
        return None
    def count(value):
        return value if type(value) is int and value >= 0 else None

    total = count(_safe_field(usage, "total_tokens"))
    prompt = count(_safe_field(usage, "input_tokens"))
    if prompt is None:
        prompt = count(_safe_field(usage, "prompt_tokens"))
    output = count(_safe_field(usage, "output_tokens"))
    if output is None:
        output = count(_safe_field(usage, "completion_tokens"))
    if prompt is not None and output is not None:
        return max(prompt + output, total or 0)
    # 单边用量缺失不可补成零；明显小于已知单边数量的 total 同样不可信。
    if total is not None and total >= max(prompt or 0, output or 0):
        return total
    return None


def _charge_budget(response: Any) -> None:
    """把一次模型调用的 token 记进当前意图的预算账本。

    未启用预算（TOKEN_BUDGET_TOTAL=0）时直接返回，零开销。
    usage 字段各网关存在差异，取不到就不计——**记账失败绝不能影响主链路**。
    """
    try:
        budget = get_budget()
        if budget is None or response is None:
            return
        usage = _safe_field(response, "usage")
        if usage is None:
            return
        total = _safe_field(usage, "total_tokens")
        if total is None:
            prompt = _safe_field(usage, "input_tokens") or _safe_field(usage, "prompt_tokens") or 0
            completion = (
                _safe_field(usage, "output_tokens")
                or _safe_field(usage, "completion_tokens")
                or 0
            )
            total = int(prompt) + int(completion)
        budget.charge("llm", int(total))
    except Exception as err:  # noqa: BLE001
        logger.debug("Token 记账跳过（不影响主链路）：%s", err)


def create_chat_model(
    settings: Settings,
    stream: bool = True,
    throttle: Optional[GatewayThrottle] = None,
    bus: Optional[TradeEventBus] = None,
    throttle_factory: Optional[Callable[[str], GatewayThrottle]] = None,
) -> OpenAIChatModel:
    snapshot = ShoppingContext.current()
    runtime = getattr(snapshot, 'model_runtime', None) if snapshot else None
    if runtime:
        return _create_profile_model(runtime, settings, stream, throttle, bus, throttle_factory)
    credential = OpenAICredential(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )
    common = {
        "credential": credential,
        "stream": stream,
        # 上下文窗口：压缩触发阈值（ContextConfig.trigger_ratio）按此值比例计算
        "context_size": settings.context_size,
        # 重试统一由外层逐次预算预留管理，避免 SDK 内部重试绕过账本。
        "max_retries": 0,
        "client_kwargs": {"max_retries": 0},
    }
    if settings.llm_enable_thinking is not None:
        # 供应商扩展参数：主模型和已配置回退模型使用同一显式选择；不改变模型名。
        common["extra_body"] = {"enable_thinking": settings.llm_enable_thinking}
    fallback = (
        StreamClosingOpenAIChatModel(model=settings.llm_fallback_model,
            formatter=PromptCacheFormatter(mode=settings.prompt_cache_mode, policy=settings.prompt_cache_policy), **common)
        if settings.llm_fallback_model and settings.llm_fallback_model != settings.llm_model
        else None
    )
    return ThrottledChatModel(
        model=settings.llm_model,
        throttle=throttle or GatewayThrottle(settings.llm_max_concurrency, settings.llm_min_interval_seconds),
        fallback=fallback,
        max_transient_retries=settings.llm_max_retries,
        bus=bus,
        recovery_mode=settings.model_recovery_mode,
        formatter=PromptCacheFormatter(mode=settings.prompt_cache_mode, policy=settings.prompt_cache_policy),
        **common,
    )


def _create_profile_model(runtime, settings, stream, throttle, bus, throttle_factory):
    """同一轮的冻结配置创建所有 Agent 模型；凭据只停留在内存。"""
    from copy import deepcopy
    def arguments(profile):
        reasoning = dict(profile.get('reasoning_parameters') or {})
        extra_body = dict(reasoning.pop('extra_body', {}) or {})
        # 不把未知配置键直接展开成 SDK 参数；协议选项由模型目录明确映射。
        native = {key: reasoning.pop(key) for key in list(reasoning)
                  if key in {'reasoning_effort'}}
        if native.get('reasoning_effort') is not None:
            # AgentScope 只有打开该开关才会把原生 reasoning_effort 序列化进请求。
            native['thinking_enable'] = True
        extra_body.update(reasoning)
        maximum = int(profile.get('max_output_tokens') or 8192)
        parameters = OpenAIChatModel.Parameters(max_tokens=min(8192, maximum), **native)
        client_kwargs = {'max_retries': 0}
        if str(profile.get('profile_id', '')).startswith('custom-'):
            import httpx
            from app.infrastructure.model_connection import ValidatedModelTransport
            client_kwargs['http_client'] = httpx.AsyncClient(
                transport=ValidatedModelTransport(allowed_local_hosts=getattr(settings, 'model_allowed_local_hosts', ())),
                follow_redirects=False, trust_env=False)
        return {
            'model': profile['model_id'], 'runtime': deepcopy(profile),
            'credential': OpenAICredential(api_key=profile['api_key'], base_url=profile['base_url']),
            'context_size': int(profile['context_size']), 'stream': stream,
            'max_retries': 0, 'client_kwargs': client_kwargs,
            'parameters': parameters, 'extra_body': extra_body or None,
            'formatter': PromptCacheFormatter(mode=settings.prompt_cache_mode, policy=settings.prompt_cache_policy),
        }
    primary_throttle = (throttle_factory(runtime.get('quota_group', 'default')) if throttle_factory else
                        throttle or GatewayThrottle(settings.llm_max_concurrency, settings.llm_min_interval_seconds))
    fallback_profile = runtime.get('fallback')
    fallback = None
    fallback_throttle = None
    if fallback_profile:
        fallback = StreamClosingOpenAIChatModel(**arguments(fallback_profile))
        if fallback_profile.get('quota_group') == runtime.get('quota_group'):
            fallback_throttle = primary_throttle
        elif throttle_factory:
            fallback_throttle = throttle_factory(fallback_profile.get('quota_group', 'default'))
        else:
            fallback_throttle = GatewayThrottle(settings.llm_max_concurrency, settings.llm_min_interval_seconds)
    return ThrottledChatModel(**arguments(runtime), throttle=primary_throttle,
        fallback=fallback, fallback_throttle=fallback_throttle, max_transient_retries=settings.llm_max_retries,
        recovery_mode=settings.model_recovery_mode, bus=bus)
