"""模型响应的有限恢复；每次续写重新经过预算、限流、HTTP 校验与计量。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from contextvars import ContextVar
from dataclasses import dataclass
from copy import deepcopy

import httpx
import openai
from agentscope.message import AssistantMsg, UserMsg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage
from agentscope.tool import ToolChoice
from app.infrastructure.context_usage import context_call_kind, context_request_details, record_context_diagnostic
from app.infrastructure.model_protocol import ModelProtocolViolation
from app.infrastructure.transient import is_transient_error

response_observation = ContextVar('model_response_observation', default=None)


@dataclass
class ResponseObservation:
    reason: str | None = None
    saw_tool: bool = False
    protocol_status: str | None = None

    def observe(self, response, protocol_status):
        if protocol_status is not None:
            self.protocol_status = protocol_status
        if response is None:
            return
        for choice in response.choices:
            if choice.finish_reason is not None:
                self.reason = choice.finish_reason
            message = getattr(choice, 'delta', None) or getattr(choice, 'message', None)
            self.saw_tool |= bool(getattr(message, 'tool_calls', None))


class IncompleteModelResponse(RuntimeError):
    """仅携带可见正文供 AgentState 保存，不保存残缺工具参数或内部思考。"""
    def __init__(self, text, reason):
        super().__init__('回答尚未完成，已保留生成内容；请继续或缩小问题范围。')
        self.partial_text = text
        self.reason = reason


def transient(error):
    return isinstance(error, (httpx.TransportError, openai.APIConnectionError, openai.APITimeoutError)) or is_transient_error(error)


def input_too_long(error):
    return any(marker in str(error).lower() for marker in (
        'inputcontenttoolong', 'context_length_exceeded', 'maximum context length', 'input is too long'))


async def recover_response(model, messages, tools, tool_choice, kwargs, *, enabled):
    """最多两次续写；底层瞬时重试另计，不触碰工具执行或会话审批。"""
    def request_details(rows, number):
        prior = context_request_details.get()
        if number:
            from app.infrastructure.context_governance import request_diagnostics
            details = request_diagnostics(rows, tools)
            details['reason'] = 'response_continuation'
        else:
            details = dict(prior) if prior is not None else None
        if details is not None:
            details['recovery_attempt'] = number
        return details

    async def attempt(rows, choice, number):
        observation = ResponseObservation()
        token = response_observation.set(observation)
        kind = context_call_kind.set('continuation' if number else context_call_kind.get())
        details = context_request_details.set(request_details(rows, number))
        try:
            if number:
                from app.infrastructure.model_counting import count_model_input, input_limits
                count = await count_model_input(model, rows, tools)
                output = kwargs.get('max_completion_tokens') or kwargs.get('max_tokens') or model.parameters.max_tokens or 8192
                if count['estimated_input_tokens'] > input_limits(model, output)['input_budget']:
                    raise IncompleteModelResponse('', 'capacity')
            response = await model._call_once(rows, tools, choice, **kwargs)
            return response, observation, None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return None, observation, error
        finally:
            context_request_details.reset(details)
            context_call_kind.reset(kind)
            response_observation.reset(token)

    first = await attempt(messages, tool_choice, 0)
    if first[2] is not None and (not enabled or not isinstance(first[2], ModelProtocolViolation)
                                or first[2].code not in {'incomplete_tool_response', 'incomplete_tool_stream'}):
        raise first[2]

    if not enabled:
        response, observation, _ = first
        if not isinstance(response, AsyncIterable):
            if isinstance(response, ChatResponse):
                response.metadata['raw_finish_reason'] = observation.reason
            return response
        async def passthrough():
            try:
                yield None
                async for part in response:
                    if isinstance(part, ChatResponse) and part.is_last:
                        part.metadata['raw_finish_reason'] = observation.reason
                    yield part
            finally:
                if hasattr(response, 'aclose'):
                    await response.aclose()
        output = passthrough()
        await anext(output)
        return output

    async def generate():
        response, observation, error = first
        rows, choice = list(messages), tool_choice
        previous_text = ''
        usages = []
        try:
            yield None  # 预启动，调用方未读首片段就取消时也能关闭底层流。
            for number in range(3):
                final, text, emitted_tool = None, '', False
                kind = context_call_kind.set('continuation' if number else context_call_kind.get())
                details = context_request_details.set(request_details(rows, number))
                try:
                    if error is None:
                        if isinstance(response, AsyncIterable):
                            async for part in response:
                                if part.is_last:
                                    final = part
                                else:
                                    text += ''.join(b.text for b in part.content if isinstance(b, TextBlock))
                                    emitted_tool |= any(isinstance(b, ToolCallBlock) for b in part.content)
                                    yield part
                        else:
                            final = response
                        if final is not None:
                            text = ''.join(b.text for b in final.content if isinstance(b, TextBlock))
                            final.metadata['raw_finish_reason'] = observation.reason
                    if final is None and error is None:
                        error = IncompleteModelResponse('', 'missing_final')
                except asyncio.CancelledError:
                    raise
                except Exception as failure:
                    error = failure
                finally:
                    if isinstance(response, AsyncIterable) and hasattr(response, 'aclose'):
                        await response.aclose()
                    context_request_details.reset(details)
                    context_call_kind.reset(kind)
                usages.append(final.usage if final is not None else None)
                truncated = observation.reason == 'length' and not observation.saw_tool
                incomplete_tool = isinstance(error, ModelProtocolViolation) and error.code in {
                    'incomplete_tool_response', 'incomplete_tool_stream'}
                disconnected = error is not None and not isinstance(error, ModelProtocolViolation) and transient(error)
                recoverable = (truncated or incomplete_tool or disconnected) and not emitted_tool
                if error is None and not truncated:
                    if number and final.metadata.get('budget_fallback'):
                        raise IncompleteModelResponse(previous_text, 'budget')
                    if previous_text:
                        final.content = [TextBlock(text=previous_text + text),
                            *[deepcopy(b) for b in final.content if not isinstance(b, TextBlock)]]
                    if number:
                        final.usage = (ChatUsage(input_tokens=sum(u.input_tokens for u in usages),
                            output_tokens=sum(u.output_tokens for u in usages),
                            cache_input_tokens=sum(u.cache_input_tokens or 0 for u in usages),
                            cache_creation_input_tokens=sum(u.cache_creation_input_tokens or 0 for u in usages),
                            time=sum(u.time for u in usages)) if all(u is not None for u in usages) else None)
                        final.metadata['continuations'] = number
                    yield final
                    return
                if not recoverable or number == 2:
                    partial = previous_text + text
                    if partial:
                        raise IncompleteModelResponse(partial, 'exhausted' if recoverable else 'nonrecoverable') from error
                    if error is not None:
                        raise error
                    raise IncompleteModelResponse('', 'exhausted')
                # 完整工具结果在 rows 中原样保留。半截工具一直被 HTTP 合同拦住，不能进入此处历史。
                if text:
                    rows = [*rows, AssistantMsg('assistant', text)]
                previous_text += text
                retry_tool = incomplete_tool or (disconnected and observation.saw_tool)
                if retry_tool:
                    hint = '上次工具响应未完成，工具尚未执行。重新生成完整工具调用，不猜补残缺参数，不重复已有正文。'
                elif text or previous_text:
                    hint = '接着已生成正文的中断位置继续，不重复开头和已完成内容。本次禁止调用工具；已有工具结果仍有效，不重新执行。'
                    choice = ToolChoice(mode='none')
                else:
                    hint = '上次响应尚未产生可用内容，请继续完成当前买家请求。'
                rows = [*rows, UserMsg('response_recovery', hint)]
                record_context_diagnostic({'type': 'response_recovery', 'attempt': number + 1,
                    'reason': 'incomplete_tool' if retry_tool else 'length' if truncated else 'connection',
                    'partial_text_preserved': bool(previous_text)})
                response, observation, error = await attempt(rows, choice, number + 1)
        finally:
            if isinstance(response, AsyncIterable) and hasattr(response, 'aclose'):
                await response.aclose()

    output = generate()
    await anext(output)
    if isinstance(first[0], AsyncIterable) or (first[0] is None and model.stream):
        return output
    final = None
    try:
        async for response in output:
            if response.is_last:
                final = response
    finally:
        await output.aclose()
    return final
