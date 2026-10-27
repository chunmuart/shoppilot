"""自定义模型的无隐私连通性探针；固定协议、固定工具和受限网络范围。"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx


class ModelConfigurationError(ValueError):
    """可以安全返回页面的配置错误；不包含上游响应或凭据。"""


async def validate_address(base_url: str, allowed_local_hosts: tuple[str, ...] = ()) -> tuple[str, str]:
    """解析并固定连接地址，避免 DNS 校验后重新解析导致地址变化。"""
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ModelConfigurationError("Base URL 必须是无认证信息、查询参数的 HTTP(S) 地址")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ModelConfigurationError("Base URL 端口无效") from None
    if parsed.scheme == "http" and host not in allowed_local_hosts:
        raise ModelConfigurationError("远程模型连接必须使用 HTTPS")
    try:
        records = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), timeout=5,
        )
        addresses = sorted({record[4][0] for record in records})
    except (OSError, asyncio.TimeoutError):
        raise ModelConfigurationError("无法解析模型服务地址") from None
    if not addresses:
        raise ModelConfigurationError("无法解析模型服务地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        # 云元数据、链路本地、未指定和多播地址即使在放行列表也不可访问。
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) in {"100.100.100.200", "fd00:ec2::254"}:
            raise ModelConfigurationError("模型服务地址不在允许的网络范围")
        if not ip.is_global and host not in allowed_local_hosts:
            raise ModelConfigurationError("本地或私有模型服务需由管理员显式放行域名")
    selected = addresses[0]
    authority = f"[{selected}]" if ":" in selected else selected
    pinned = f"{parsed.scheme}://{authority}:{port}{parsed.path.rstrip('/')}"
    public_host = f"[{host}]" if ":" in host else host
    original_authority = public_host if port == (443 if parsed.scheme == "https" else 80) else f"{public_host}:{port}"
    return pinned, original_authority


async def probe_connection(profile: dict, api_key: str, *, allowed_local_hosts: tuple[str, ...] = (),
                           transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """验证流式工具往返。小探针不能证明用户声明的最大窗口。"""
    pinned, authority = await validate_address(profile["base_url"], allowed_local_hosts)
    host = urlsplit(profile["base_url"]).hostname
    tool = {"type": "function", "function": {"name": "connection_probe", "description": "Return a fixed test value.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string", "enum": ["ok"]}},
                           "required": ["value"], "additionalProperties": False}}}
    messages = [{"role": "user", "content": "Connection test. Call connection_probe with value ok. This has no side effects."}]
    parameters = profile.get("probe_reasoning_parameters", {})
    extra_body = parameters.get("extra_body", {})
    common = {"model": profile["model_id"], "stream": True, "stream_options": {"include_usage": True},
              "max_tokens": 1024, **{k: v for k, v in parameters.items() if k != "extra_body"}, **extra_body}
    usage_seen = False
    returned_models: set[str] = set()
    requests_with_identity = 0
    def upstream_error(event: dict) -> None:
        raw = event.get("error")
        if raw is not None or event.get("type") == "error" or ("code" in event and not event.get("choices")):
            code = raw.get("code", raw.get("type")) if isinstance(raw, dict) else event.get("code")
            safe_code = str(code) if isinstance(code, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(code)) else "unknown"
            # 上游 message 可能包含请求与认证信息，只展示受限错误码。
            raise ModelConfigurationError(f"模型校验收到上游错误（code={safe_code}），请检查模型权限、额度或稍后重试")
    async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=5), follow_redirects=False,
                                transport=transport, trust_env=False) as client:
        async def request(body: dict) -> tuple[dict, str, str]:
            nonlocal usage_seen, requests_with_identity
            calls: dict[int, dict] = {}
            text = ""
            reasoning = ""
            finished = False
            size = 0
            has_identity = False
            try:
                async with client.stream("POST", pinned + "/chat/completions", json=body,
                        headers={"Authorization": f"Bearer {api_key}", "Host": authority},
                        extensions={"sni_hostname": host}) as response:
                    if response.status_code != 200:
                        raise ModelConfigurationError(f"模型校验失败（HTTP {response.status_code}），请检查地址、权限及协议")
                    if "application/json" in response.headers.get("content-type", ""):
                        payload = await response.aread()
                        if len(payload) > 131072:
                            raise ModelConfigurationError("模型校验响应超出允许大小")
                        event = json.loads(payload)
                        if isinstance(event, dict):
                            upstream_error(event)
                        raise ModelConfigurationError("模型未返回请求的 SSE 流，请检查网关流式协议支持")
                    async for line in response.aiter_lines():
                        size += len(line.encode())
                        if size > 131072:
                            raise ModelConfigurationError("模型校验响应超出允许大小")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        event = json.loads(data)
                        if not isinstance(event, dict):
                            raise ModelConfigurationError("模型事件不是有效 JSON 对象")
                        upstream_error(event)
                        reported_model = event.get("model")
                        # 只返回受限的型号标识，不泄露上游任意字段或认证信息。
                        if (isinstance(reported_model, str)
                                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", reported_model)
                                and (not api_key or api_key not in reported_model)):
                            returned_models.add(reported_model)
                            has_identity = True
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            usage_seen = usage_seen or any(type(usage.get(key)) is int and usage[key] >= 0
                                for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"))
                        for choice in event.get("choices", []):
                            finished = finished or bool(choice.get("finish_reason"))
                            delta = choice.get("delta", {})
                            text += delta.get("content") or ""
                            reasoning += delta.get("reasoning_content") or ""
                            for call in delta.get("tool_calls", []):
                                index = call.get("index", 0)
                                target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if call.get("id"):
                                    target["id"] = call["id"]
                                for key in ("name", "arguments"):
                                    target["function"][key] += call.get("function", {}).get(key) or ""
                if not finished:
                    raise ModelConfigurationError("模型流没有返回有效结束标识")
                requests_with_identity += int(has_identity)
                return calls, text, reasoning
            except ModelConfigurationError:
                raise
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, IndexError):
                raise ModelConfigurationError("模型校验失败：连接中断或返回格式不符合 OpenAI Chat 协议") from None
        # Qwen 思考模式不支持强制 tool_choice 对象；自动选择后仍验证唯一工具与精确参数。
        calls, first_text, reasoning = await request({**common, "messages": messages, "tools": [tool],
            "tool_choice": "auto"})
        if len(calls) != 1:
            raise ModelConfigurationError("模型未返回预期的工具调用")
        call = next(iter(calls.values()))
        try:
            valid = (call["id"] and call["function"]["name"] == "connection_probe"
                     and json.loads(call["function"]["arguments"]) == {"value": "ok"})
        except ValueError:
            valid = False
        if not valid:
            raise ModelConfigurationError("模型工具调用参数不符合受控探针要求")
        assistant = {"role": "assistant", "content": first_text or None, "tool_calls": [call]}
        if reasoning:
            # 思考工具往返需保留供应商给出的协议字段，仅用于该固定探针。
            assistant["reasoning_content"] = reasoning
        messages.extend([assistant,
                         {"role": "tool", "tool_call_id": call["id"], "content": '{"value":"ok"}'},
                         {"role": "user", "content": "Reply with a short confirmation. Do not call any tool."}])
        final_calls, text, _ = await request({**common, "messages": messages, "tools": [tool], "tool_choice": "none"})
        if final_calls or not text.strip():
            raise ModelConfigurationError("模型未能完成工具结果后的文本回答")
    expected_models = {profile["model_id"], *profile.get("probe_model_aliases", [])}
    identity_matches = (False if returned_models - expected_models else True if requests_with_identity == 2 else None)
    return {"streaming": True, "tool_calling": True, "tool_result": True, "usage_available": usage_seen,
            "requested_model": profile["model_id"], "returned_models": sorted(returned_models),
            "model_identity_matches": identity_matches, "tested_at": datetime.now(timezone.utc).isoformat(),
            "context_verified": False, "scope": "固定小样例的流式工具往返；未验证完整上下文容量"}


class ValidatedModelTransport(httpx.AsyncBaseTransport):
    """运行时也解析、校验并固定地址，防止保存配置后 DNS 变更绕过校验。"""

    def __init__(self, *, allowed_local_hosts: tuple[str, ...] = (), transport=None):
        self.allowed_local_hosts = allowed_local_hosts
        self.transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pinned, authority = await validate_address(str(request.url), self.allowed_local_hosts)
        request.headers["Host"] = authority
        request.extensions["sni_hostname"] = request.url.host
        request.url = httpx.URL(pinned)
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self.transport.aclose()
