"""校验最终 HTTP 工具合同；不信任兼容网关会遵守 tool_choice。"""
from __future__ import annotations

from app.infrastructure.prompt_cache import value


class ModelProtocolViolation(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('模型服务返回了不符合本次工具权限的响应，已停止执行。')


class ResponseContract:
    def __init__(self, request, observe):
        self.allowed = {t['function']['name'] for t in request.get('tools', [])
                        if t.get('type') == 'function'}
        choice = request.get('tool_choice')
        self.none = choice == 'none' or not self.allowed
        self.forced = choice.get('function', {}).get('name') if isinstance(choice, dict) else None
        self.required = choice == 'required' or bool(self.forced)
        self.observe = observe

    def validate(self, names):
        code = ('tools_forbidden' if self.none and names else
                'undeclared_tool' if any(n not in self.allowed for n in names) else
                'wrong_forced_tool' if self.forced and any(n != self.forced for n in names) else
                'required_tool_missing' if self.required and not names else None)
        self.observe(protocol_status=code or 'valid')
        # 结构化生成仍可由 SDK 回退解析文本；明确的商品重新核验缺少工具时不能视为完成。
        if code and (code != 'required_tool_missing' or self.forced == 'product_search_tool'):
            raise ModelProtocolViolation(code)

    def completion(self, response):
        self.observe(response=response)
        if any(value(choice.message, 'tool_calls') and choice.finish_reason not in ('stop','tool_calls')
               for choice in response.choices):
            self.observe(protocol_status='incomplete_tool_response')
            raise ModelProtocolViolation('incomplete_tool_response')
        names = [c.function.name for choice in response.choices
                 for c in (value(choice.message, 'tool_calls') or [])]
        self.validate(names)
        return response


class ValidatedStream:
    """正文照常流出；工具片段全部校验后才交给 AgentScope，混合批次不能部分执行。"""
    def __init__(self, source, contract):
        self.source, self.contract = source, contract
        self.closed = False

    async def __aiter__(self):
        buffered, names, finished = [], {}, set()
        try:
            async for chunk in self.source:
                self.contract.observe(response=chunk)
                has_tool = False
                for choice in chunk.choices:
                    if choice.finish_reason == 'tool_calls':
                        finished.add(choice.index)
                    for call in value(choice.delta, 'tool_calls') or []:
                        has_tool = True
                        key = (choice.index, call.index)
                        names[key] = names.get(key, '') + (value(call.function, 'name') or '')
                if has_tool or buffered:
                    buffered.append(chunk)
                else:
                    yield chunk
            if any(index not in finished for index, _ in names):
                self.contract.observe(protocol_status='incomplete_tool_stream')
                raise ModelProtocolViolation('incomplete_tool_stream')
            self.contract.validate(list(names.values()))
            emitted = set()
            for chunk in buffered:
                # SDK 2.0.8仅使用首片段的函数名；合并名称分片后再交给原生解析器。
                normalized = chunk.model_copy(deep=True)
                for choice in normalized.choices:
                    for call in value(choice.delta, 'tool_calls') or []:
                        key = (choice.index, call.index)
                        if call.function is not None:
                            call.function.name = names[key] if key not in emitted else None
                            emitted.add(key)
                yield normalized
        finally:
            await self.close()

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.source.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
