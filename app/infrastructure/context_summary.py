"""缓存友好的摘要候选；保留原请求前缀，不执行任何业务工具。

工具选择参数仍可能影响供应商缓存，是否获益必须以真实 usage 为准。
校验、证据归档和事务提交由 LayeredContextMiddleware 统一负责。
"""
from __future__ import annotations

from collections.abc import AsyncIterable
from copy import deepcopy
import json
import re

import jsonschema
from agentscope.message import UserMsg, TextBlock, ThinkingBlock, Usage
from agentscope.tool import ToolChoice


async def summarize_preserving_prefix(agent, config, instructions=None):
    from app.infrastructure.context_governance import compression_parts, share_identical_products, ContextCapacityError, request_diagnostics
    from app.infrastructure.context_usage import record_context_diagnostic, context_request_details

    head, tail = compression_parts(agent)
    if not head:
        return
    prepared = await agent._prepare_model_input()
    messages = share_identical_products(prepared['messages'],
        compact_rules=getattr(agent, '_shoppilot_compact_rules', False))
    instruction = (config.compression_prompt + '\n本次仅输出一个符合下列 JSON Schema 的 JSON 对象，不加 Markdown，不调用工具。'
        '\n阅读完整历史以延续先前有效要求。近期原文仍会保留；摘要重点记录较早进展与最新有效状态。'
        '\nJSON Schema：' + json.dumps(config.summary_schema, ensure_ascii=False, separators=(',', ':')))
    if instructions is not None:
        instruction += '\n' + getattr(instructions, 'text', str(instructions))
    messages.append(UserMsg('context_summary_request', instruction))
    tools = deepcopy(prepared.get('tools') or [])
    estimated = await agent._shoppilot_summary_count(agent, messages, tools)
    if estimated > agent._shoppilot_summary_available:
        raise ContextCapacityError('完整摘要请求超过安全容量，已保留原状态；请缩小本次任务')
    record_context_diagnostic({'type': 'summary_request', 'mode': 'preserve_prefix',
        'original_tool_count': len(tools), 'estimated_tokens': estimated,
        'provider_cache_confirmed': False})
    details = request_diagnostics(messages, tools)
    details['reason'] = 'context_summary'
    token = context_request_details.set(details)
    try:
        response = await agent.model(messages=messages, tools=tools, tool_choice=ToolChoice(mode='none'))
        if isinstance(response, AsyncIterable):
            final = None
            try:
                async for part in response:
                    if part.is_last:
                        final = part
            finally:
                if hasattr(response, 'aclose'):
                    await response.aclose()
            response = final
    finally:
        context_request_details.reset(token)
    if response is None or any(not isinstance(block, (TextBlock, ThinkingBlock)) for block in response.content):
        raise ValueError('摘要必须是完整文本 JSON，禁止工具执行或非文本结果')
    if response.metadata.get('raw_finish_reason') == 'length':
        raise ValueError('摘要达到输出上限，候选不完整')
    text = ''.join(block.text for block in response.content if isinstance(block, TextBlock)).strip()
    text = re.sub(r'^```(?:json)?\s*([\s\S]*?)\s*```$', r'\1', text).strip()
    try:
        content = json.loads(text)
        jsonschema.validate(content, config.summary_schema)
        summary = config.summary_template.format(**content)
    except (ValueError, TypeError, KeyError, jsonschema.ValidationError) as error:
        raise ValueError('摘要未通过五字段格式校验') from error
    if not all(isinstance(value, str) for value in content.values()) or not summary.strip():
        raise ValueError('摘要字段必须是文本')
    # 沿用 SDK 的读取缓存清理与 usage 归属；失败由外层恢复 AgentState。
    reserved = deepcopy(tail)
    await agent._clear_unreserved_read_cache(reserved)
    agent.state.summary = summary
    agent.state.context = reserved
    if response.usage is not None:
        if not agent.state.context:
            agent.state.append_context(agent.name, [])
        agent.state.context[-1].append_usage(Usage(
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
            cache_input_tokens=response.usage.cache_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0))
