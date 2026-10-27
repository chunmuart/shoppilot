# -*- coding: utf-8 -*-
"""Reranker HTTP 契约回归。"""

from __future__ import annotations

import pytest

from app.infrastructure.rerank import http_reranker
from app.infrastructure.rerank.http_reranker import HttpReranker
from app.infrastructure.settings import load_settings


@pytest.mark.asyncio
async def test_full_reranker_endpoint_is_used_with_gateway_authorization(
    monkeypatch, tmp_path
) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.1},
                ],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setenv("LLM_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "RERANKER_BASE_URL",
        "https://1688openai.alibaba-inc.com/v1/services/reranker",
    )
    monkeypatch.setenv("RERANKER_MODEL", "qwen-text-rerank")
    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: FakeClient())

    scores = await HttpReranker(load_settings()).rerank(
        "轻便旅行背包",
        ["20L 轻量旅行背包", "陶瓷咖啡杯"],
    )

    assert scores == [0.9, 0.1]
    assert captured["url"] == "https://1688openai.alibaba-inc.com/v1/services/reranker"
    assert captured["headers"] == {"Authorization": "Bearer test-gateway-key"}
    assert captured["json"] == {
        "model": "qwen-text-rerank",
        "query": "轻便旅行背包",
        "documents": ["20L 轻量旅行背包", "陶瓷咖啡杯"],
        "top_n": 2,
    }


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "score": 0.8}, {"index": 0, "score": 0.1}],
        [{"index": -1, "score": 0.8}, {"index": 0, "score": 0.1}],
        [{"index": 0, "score": float("nan")}, {"index": 1, "score": 0.1}],
        [{"index": 0}, {"index": 1, "score": 0.1}],
    ],
)
async def test_invalid_rankings_fail_closed(monkeypatch, results):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": results}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: Client())
    with pytest.raises((ValueError, RuntimeError)):
        await HttpReranker(load_settings()).rerank("背包", ["背包", "耳机"])


def test_llm_reranker_mode_is_rejected():
    from dataclasses import replace
    from app.infrastructure.rerank.factory import create_reranker

    with pytest.raises(ValueError, match="http / disabled"):
        create_reranker(replace(load_settings(), reranker_mode="llm"))


@pytest.mark.parametrize("protocol", ["flat", "dashscope"])
async def test_dedicated_key_and_protocol(monkeypatch, protocol):
    from dataclasses import replace
    import httpx

    captured = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            results = [
                {"index": 1, "relevance_score": 0.1},
                {"index": 0, "relevance_score": 0.9},
            ]
            body = (
                {"output": {"results": results}}
                if protocol == "dashscope"
                else {"results": results}
            )
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: Client())
    s = replace(
        load_settings(),
        reranker_protocol=protocol,
        reranker_api_key="dedicated",
        reranker_base_url="https://rerank.test/v1/reranks",
    )
    assert await HttpReranker(s).rerank("背包", ["背包", "水杯"]) == [0.9, 0.1]
    assert captured["url"] == s.reranker_base_url
    assert captured["headers"]["Authorization"] == "Bearer dedicated"
    body = captured["json"]
    assert (body["input"] if protocol == "dashscope" else body)["documents"] == [
        "背包",
        "水杯",
    ]
    assert body.get("parameters", body)["top_n"] == 2


async def test_http_200_service_error_is_not_success(monkeypatch):
    import httpx

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            return httpx.Response(
                200,
                json={"success": False, "message": "未找到模型对应的服务"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: Client())
    with pytest.raises(RuntimeError, match="模型未注册"):
        await HttpReranker(load_settings()).rerank("背包", ["背包"])


async def test_identical_documents_are_scored_once_but_all_positions_restored(
    monkeypatch,
):
    import httpx

    captured = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return httpx.Response(
                200,
                json={
                    "results": [{"index": 1, "score": 0.1}, {"index": 0, "score": 0.9}]
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: Client())
    scores = await HttpReranker(load_settings()).rerank(
        "背包", ["黑色背包", "水杯", "黑色背包"]
    )
    assert captured["documents"] == ["黑色背包", "水杯"]
    assert captured["top_n"] == 2
    assert scores == [0.9, 0.1, 0.9]
