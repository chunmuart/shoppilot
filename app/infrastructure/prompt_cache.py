"""供应商显式 Prompt Cache：仅装饰请求副本，不保存买家正文或替代上下文治理。"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import math
import re
from typing import Any, Literal

from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import ToolResultBlock, ToolResultState

cache_disabled: ContextVar[bool] = ContextVar("prompt_cache_disabled", default=False)
cache_retry_target: ContextVar[str | None] = ContextVar("prompt_cache_retry_target", default=None)
cache_attempt: ContextVar["CacheAttempt | None"] = ContextVar("prompt_cache_attempt", default=None)


def value(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return getattr(obj, key, None)
    except (KeyError, AttributeError):
        return None


def count(number):
    return number if type(number) is int and number >= 0 else None


def _consistent(values):
    known = [v for v in values if v is not None]
    return (known[0], False) if known and len(set(known)) == 1 else (None, bool(known))


def read_cache_usage(usage) -> dict:
    """只读取白名单数值，保留缺失与冲突；不保留原始响应正文。"""
    details = value(usage, "prompt_tokens_details")
    read, read_conflict = _consistent([
        count(value(details, "cached_tokens")), count(value(usage, "cache_read_input_tokens")),
    ])
    creation = value(details, "cache_creation")
    ttl_counts = [count(value(creation, k)) for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")]
    ttl_total = sum(v for v in ttl_counts if v is not None) if any(v is not None for v in ttl_counts) else None
    write, write_conflict = _consistent([
        count(value(details, "cache_creation_input_tokens")), count(value(details, "cache_write_tokens")),
        count(value(usage, "cache_creation_input_tokens")), ttl_total,
    ])
    total = count(value(usage, "prompt_tokens"))
    invalid = read_conflict or write_conflict or (total is not None and (
        (read or 0) > total or (write or 0) > total or (read or 0) + (write or 0) > total))
    if invalid:
        read = write = None
    cost = value(usage, "model_cost")
    if type(cost) not in (float, int) or not math.isfinite(cost) or cost < 0:
        cost = None
    return {"cache_read_tokens": read, "cache_write_tokens": write,
            "cache_usage_invalid": bool(invalid), "reported_cost": cost}


@dataclass
class CacheAttempt:
    model: str
    mode: str
    policy: str
    retry_without_cache: bool = False
    prefix: dict = field(default_factory=dict)
    marker_count: int = 0
    boundary_blocks: list[int] = field(default_factory=list)
    reason: str = "passthrough"
    usage: dict = field(default_factory=lambda: read_cache_usage(None))
    response_models: set[str] = field(default_factory=set)
    protocol_status: str = 'unknown'

    def metrics(self):
        response_model = next(iter(self.response_models)) if len(self.response_models) == 1 else None
        return {"model": self.model, "response_model": response_model,
                "response_model_matches": response_model == self.model if response_model else None,
                "response_model_conflict": len(self.response_models) > 1,
                "protocol_status": self.protocol_status,
                "cache_mode": self.mode, "cache_policy": self.policy,
                "cache_marker_count": self.marker_count, "cache_reason": self.reason,
                "cache_retry_without_markers": self.retry_without_cache, **self.usage, **self.prefix}


def mark_messages(messages: list[dict], policy: str, incomplete_ids=frozenset()) -> tuple[list[dict], list[int]]:
    """静态边界 + 最近两个完整历史边界；不合并消息、不改工具参数或结果。"""
    result = deepcopy(messages)
    candidates = []
    pending: set[str] = set()
    malformed = False
    for i, msg in enumerate(result):
        role = msg.get("role")
        calls = msg.get("tool_calls") or []
        if calls:
            ids = [call.get("id") for call in calls]
            if pending or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
                malformed = True
            pending.update(x for x in ids if isinstance(x, str))
        elif role == "tool":
            tool_id = msg.get("tool_call_id")
            if tool_id not in pending or tool_id in incomplete_ids:
                malformed = True
            pending.discard(tool_id)
        elif pending:
            # 未闭合调用不能因后面出现普通消息而假装完成。
            malformed = True
        if not pending and not malformed and role in ("assistant", "tool") and msg.get("content") and not calls:
            candidates.append(i)

    selected = []
    # 只标记第一个系统消息，避免把随后动态注入的偏好/Skill 误叫作静态前缀。
    if result and result[0].get("role") == "system" and result[0].get("content"):
        selected.append(0)
    if policy == "static_history":
        selected.extend(candidates[-2:])
    positions = []
    block_number = 0
    for i, msg in enumerate(result):
        content = msg.get("content")
        if i in selected and isinstance(content, str):
            content = msg["content"] = [{"type": "text", "text": content}]
        if isinstance(content, list):
            # 按最终序列化后的 content 块计算，不把消息条数当块数。
            eligible = [j for j, block in enumerate(content) if block.get("type") == "text" and block.get("text")]
            if i in selected and eligible:
                j = eligible[-1]
                content[j]["cache_control"] = {"type": "ephemeral"}
                positions.append(block_number + j)
            block_number += len(content)
        elif content:
            block_number += 1
    return result, positions


def normalized_cache_messages(messages):
    """三组统一文本块包装，只移除请求层标记，不修改业务正文。"""
    result = deepcopy(messages)
    for msg in result:
        content = msg.get('content')
        if isinstance(content, str):
            msg['content'] = [{'type': 'text', 'text': content}]
        for block in msg.get('content') or []:
            if isinstance(block, dict):
                block.pop('cache_control', None)
    return result


def closed_cache_boundaries(messages, incomplete_ids=frozenset()):
    """只允许完整工具批次之后；任何非法配对之后都不猜测边界。"""
    pending, used, boundaries = set(), set(), []
    for i, msg in enumerate(messages):
        calls = msg.get('tool_calls') or []
        if calls:
            ids = [call.get('id') for call in calls]
            if pending or any(not isinstance(x, str) or not x or x in used for x in ids) or len(set(ids)) != len(ids):
                break
            pending.update(ids); used.update(ids)
        elif msg.get('role') == 'tool':
            tool_id = msg.get('tool_call_id')
            if tool_id not in pending or tool_id in incomplete_ids:
                break
            pending.remove(tool_id)
        elif pending:
            break
        if not pending and not calls and msg.get('role') in ('assistant', 'tool') and msg.get('content'):
            boundaries.append(i)
    return boundaries


@dataclass
class PruneAwareBoundaries:
    """实验候选：仅观察当前已读可裁剪 ID，不接收未来轨迹；默认运行链不启用。"""
    sent: dict = field(default_factory=dict)
    ttl_seconds: float | None = None
    previous_messages: list = field(default_factory=list)
    previous_envelope: str | None = None

    @staticmethod
    def fingerprint(value):
        import hashlib
        import json
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest(), len(raw)

    def select(self, request, mutable_ids, *, now, incomplete_ids=frozenset()):
        messages = normalized_cache_messages(request['messages'])
        legal = closed_cache_boundaries(messages, incomplete_ids)
        first = next((i for i, m in enumerate(messages) if m.get('role') == 'tool' and m.get('tool_call_id') in mutable_ids), None)
        preceding = [i for i in legal if first is not None and i < first]
        envelope = {k: v for k, v in request.items() if k != 'messages'}
        def prefix_at(index):
            return self.fingerprint({'request': envelope, 'messages': normalized_cache_messages(messages[:index + 1])})
        current_hashes = [self.fingerprint(m)[0] for m in messages]
        rewritten = bool(self.previous_messages) and (
            current_hashes[:len(self.previous_messages)] != self.previous_messages or
            self.previous_envelope != self.fingerprint(envelope)[0])
        protection = preceding[-1] if preceding else None
        protection_reason = 'predict_current_eligible_result'
        # 实際改写后先保留此前发送且仍相同的最长合法前缀，不能只在新位置补BP。
        if rewritten:
            surviving = []
            for i in legal:
                signature, _ = prefix_at(i)
                previous = self.sent.get(signature)
                if previous is not None and (self.ttl_seconds is None or now - previous < self.ttl_seconds):
                    surviving.append(i)
            if surviving:
                protection = surviving[-1]
                protection_reason = 'surviving_previously_sent_prefix'
        choices = [('S', 0)] if messages and messages[0].get('role') == 'system' else []
        if protection is not None:
            choices.append(('P', protection))
        if legal:
            choices.append(('R', legal[-1]))
        records, selected = [], set()
        # 全部请求条件都进入指纹；工具/schema/参数变化不会误报旧前缀。
        for role, index in choices:
            if index in selected:
                continue
            content = messages[index].get('content') or []
            text_indexes = [j for j, b in enumerate(content) if b.get('type') == 'text' and b.get('text')]
            if not text_indexes:
                continue
            signature, size = prefix_at(index)
            previous = self.sent.get(signature)
            status = ('new' if previous is None else 'previously_sent_ttl_unknown' if self.ttl_seconds is None
                      else 'previously_sent_expired' if now - previous >= self.ttl_seconds else 'previously_sent_within_ttl')
            records.append({'role': role, 'message_index': index, 'prefix_sha256': signature,
                            'prefix_bytes': size, 'prior_status': status,
                            'selection_reason': protection_reason if role == 'P' else role,
                            'provider_cache_confirmed': False})
            content[text_indexes[-1]]['cache_control'] = {'type': 'ephemeral'}
            selected.add(index)
        return messages, records

    def record_sent(self, records, *, now, request=None):
        # 必须由真正发起请求的位置调用；离线选择不产生已发送记录。
        for item in records:
            self.sent[item['prefix_sha256']] = now
        if request is not None:
            self.previous_messages = [self.fingerprint(m)[0] for m in normalized_cache_messages(request['messages'])]
            self.previous_envelope = self.fingerprint({k: v for k, v in request.items() if k != 'messages'})[0]
        if len(self.sent) > 128:
            self.sent = dict(sorted(self.sent.items(), key=lambda item: item[1])[-128:])


class PromptCacheFormatter(OpenAIChatFormatter):
    mode: Literal["passthrough", "explicit"] = "passthrough"
    policy: Literal["static", "static_history"] = "static"

    async def format(self, msgs):
        messages = await super().format(msgs)
        attempt = cache_attempt.get()
        if self.mode != "explicit" or cache_disabled.get():
            return messages
        incomplete = {b.id for msg in msgs for b in msg.get_content_blocks()
                      if isinstance(b, ToolResultBlock) and b.state in (ToolResultState.RUNNING, ToolResultState.INTERRUPTED)}
        marked, positions = mark_messages(messages, self.policy, incomplete)
        if attempt is not None:
            attempt.marker_count = len(positions)
            attempt.boundary_blocks = positions
            attempt.reason = "explicit_markers" if positions else "no_complete_boundary"
        return marked


class CacheRejected(RuntimeError):
    """上游明确拒绝 cache_control 且尚未返回流；不携带原始异常正文。"""
    def __init__(self, model):
        super().__init__("上游不支持显式缓存参数")
        self.model = model


def is_cache_rejection(error) -> bool:
    # 只识别确定的参数拒绝，普通 400、额度、鉴权、网络失败不能走此回退。
    if value(error, "status_code") not in (400, 422):
        return False
    body = value(error, "body")
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    if not isinstance(body, dict):
        return False
    text = " ".join(str(body.get(k) or "") for k in ("param", "code", "message")).lower()
    return "cache_control" in text and bool(re.search(
        r"unsupported|not supported|not support|unknown (?:parameter|field)|unrecognized|extra inputs are not permitted|不支持", text))


def attach_usage(response, attempt: CacheAttempt | None):
    usage = value(response, "usage")
    if usage is not None and attempt is not None:
        # ChatUsage 的零默认值会掩盖缺失；Python dataclass 允许 nullable，SDK tracing 也按 None 跳过。
        usage.cache_input_tokens = attempt.usage["cache_read_tokens"]
        usage.cache_creation_input_tokens = attempt.usage["cache_write_tokens"]
        usage.metadata = {**(usage.metadata or {}), "prompt_cache": attempt.metrics()}
    return response


class UsageCapturingStream:
    """保留 OpenAI stream 的生命周期，在 SDK 丢弃字段之前读取供应商 usage。"""
    def __init__(self, stream, attempt):
        self.stream = stream
        self.attempt = attempt

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    async def __aiter__(self):
        async for chunk in self.stream:
            usage = value(chunk, "usage")
            if usage is not None:
                self.attempt.usage = read_cache_usage(usage)
            yield chunk

    async def close(self):
        await self.stream.close()


class PrefixTracker:
    """比较同模型、同买家会话、同调用类型的请求；只持有带随机密钥的摘要。

    字节是规范 JSON 的完整消息前缀，不是供应商 tokenizer 的缓存 token。
    缓存标记单独散列，避免把移动 breakpoint 误判成业务正文变化。
    """
    def __init__(self, capacity=128):
        from collections import OrderedDict
        import secrets
        self.previous = OrderedDict()
        self.secret = secrets.token_bytes(32)
        self.capacity = capacity

    def observe(self, request, *, scope, kind):
        import hashlib
        import hmac
        import json
        def encode(data):
            return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        def hashed(data):
            return hmac.new(self.secret, encode(data), hashlib.sha256).hexdigest()
        def clean(data):
            if isinstance(data, dict):return {k:clean(v) for k,v in data.items() if k != 'cache_control'}
            if isinstance(data, list):return [clean(v) for v in data]
            return data
        original = request.get('messages') or []
        messages = clean(original)
        # 单个文本块和纯文本只因显式 marker 包装而不同，诊断时按同一业务正文比较。
        for message in messages:
            content = message.get('content')
            if isinstance(content, list) and len(content) == 1 and set(content[0]) == {'type','text'} and content[0]['type'] == 'text':
                message['content'] = content[0]['text']
        tools = clean(request.get('tools') or [])
        snapshot = {'tools':hashed(tools), 'system':hashed([m for m in messages if m.get('role') == 'system']),
                    'messages':[hashed(m) for m in messages], 'sizes':[len(encode(m)) for m in messages],
                    'parameters':hashed({k:v for k,v in request.items() if k not in {'messages','tools','stream','stream_options'}})}
        key = (*scope, request.get('model'), kind) if scope else None
        previous = self.previous.get(key) if key else None
        common = 0
        if previous:
            for left,right in zip(previous['messages'], snapshot['messages']):
                if left != right:break
                common += 1
        length = len(snapshot['messages'])
        changed = previous is not None and previous['messages'] != snapshot['messages']
        mode = 'first_request' if not previous else 'identical' if not changed else 'append' if common == len(previous['messages']) else 'rewrite'
        result = {'prefix_comparison':mode, 'prefix_scope_known':scope is not None,
                  'prefix_system_hash':snapshot['system'], 'prefix_tools_hash':snapshot['tools'],
                  'prefix_message_count':length, 'prefix_common_messages':common if previous else None,
                  'prefix_common_message_bytes':sum(snapshot['sizes'][:common]) if previous else None,
                  'prefix_first_changed_message':common if changed else None,
                  'prefix_system_changed':snapshot['system'] != previous['system'] if previous else None,
                  'prefix_tools_changed':snapshot['tools'] != previous['tools'] if previous else None,
                  'prefix_parameters_changed':snapshot['parameters'] != previous['parameters'] if previous else None}
        if key:
            self.previous[key] = snapshot
            self.previous.move_to_end(key)
            while len(self.previous) > self.capacity:self.previous.popitem(last=False)
        return result
