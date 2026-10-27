# -*- coding: utf-8 -*-
"""Thinking 开关为三态；在真实 SDK 的 HTTP 请求边界验证主模型和回退模型。"""
from dataclasses import replace
import json

import httpx
import pytest
from agentscope.message import UserMsg

from app.infrastructure.llm import create_chat_model
from app.infrastructure.settings import load_settings
from app.infrastructure.throttle import GatewayThrottle
from tests.test_retrieval import _settings


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("  ", None), ("1", True), ("true", True),
    (" TRUE ", True), ("0", False), ("false", False), ("False", False),
])
def test_thinking_configuration_is_explicit_and_optional(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if value is None:
        monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    else:
        monkeypatch.setenv("LLM_ENABLE_THINKING", value)
    assert load_settings(require_llm_key=False).llm_enable_thinking is expected


@pytest.mark.parametrize("value", ["yes", "2", "invalid-sensitive-value"])
def test_invalid_thinking_configuration_is_rejected_without_echo(monkeypatch, value):
    monkeypatch.setenv("LLM_ENABLE_THINKING", value)
    with pytest.raises(ValueError, match="LLM_ENABLE_THINKING") as error:
        load_settings(require_llm_key=False)
    assert value not in str(error.value)


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("fallback", [False, True])
async def test_factory_sends_only_explicit_thinking_choice_to_primary_and_fallback(tmp_path, enabled, fallback):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if fallback and payload["model"] == "fixture-primary":
            return httpx.Response(429, json={"error": {"message": "rate limit", "type": "rate_limit"}})
        return httpx.Response(200, json={
            "id": "response", "object": "chat.completion", "created": 1, "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })
    config = replace(_settings(tmp_path), llm_model="fixture-primary", llm_api_key="fixture-key",
                     llm_base_url="https://fixture.invalid/v1", llm_fallback_model="fixture-backup" if fallback else "",
                     llm_max_retries=0, llm_enable_thinking=enabled)
    model = create_chat_model(config, stream=False, throttle=GatewayThrottle(1, 0))
    clients = [model, *([model._fallback] if model._fallback else [])]
    try:
        for candidate in clients:
            await candidate.client.close()
            candidate.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await model([UserMsg("buyer", "挑选通勤背包")])
        assert [request["model"] for request in requests] == ["fixture-primary", *(["fixture-backup"] if fallback else [])]
        for request in requests:
            assert "enable_thinking" in request if enabled is not None else "enable_thinking" not in request
            if enabled is not None:
                assert request["enable_thinking"] is enabled
            assert "reasoning_effort" not in request
    finally:
        for candidate in clients:
            await candidate.client.close()
