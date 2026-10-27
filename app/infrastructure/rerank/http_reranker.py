"""专用 reranker HTTP 客户端；失败显式降级，不调用聊天模型。"""

from __future__ import annotations

import math
import time
import httpx
from app.domain.catalog.ports.retrieval_ports import Reranker
from app.infrastructure.settings import Settings
from app.infrastructure.context_usage import context_call_kind, record_context_usage


class RerankerServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class HttpReranker(Reranker):
    def __init__(self, settings: Settings, timeout_seconds: float | None = None):
        endpoint = settings.reranker_base_url.rstrip("/")
        self._url = (
            endpoint
            if endpoint.endswith(("/rerank", "/reranker", "/reranks", "/text-rerank"))
            else f"{endpoint}/rerank"
        )
        self._api_key = settings.reranker_api_key or settings.llm_api_key
        self._model = settings.reranker_model
        self._protocol = settings.reranker_protocol
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.reranker_timeout_seconds
        )
        if self._protocol not in ("flat", "dashscope"):
            raise ValueError("RERANKER_PROTOCOL 只支持 flat / dashscope")
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            raise ValueError("reranker 超时须为正有限数")

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        # 只复用完全相同文本的相关性分数，不合并商品/SKU/报价或返回位置。
        unique_documents = list(dict.fromkeys(documents))
        inputs = {"query": query, "documents": unique_documents}
        # 要求返回全部候选；不能把服务默认的 top_n 缺项当成零分。
        body = (
            {
                "model": self._model,
                "input": inputs,
                "parameters": {
                    "top_n": len(unique_documents),
                    "return_documents": False,
                },
            }
            if self._protocol == "dashscope"
            else {"model": self._model, **inputs, "top_n": len(unique_documents)}
        )
        started, usage = time.monotonic(), {}
        token = context_call_kind.set("rerank")
        try:
            # 不跟随重定向，防止认证头转发至未知主机。
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False
            ) as client:
                response = await client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError as error:
                    raise RerankerServiceError(
                        "invalid_json",
                        "reranker 返回空响应或无效 JSON；请核对服务地址和协议",
                    ) from error
            if not isinstance(result, dict):
                raise RerankerServiceError(
                    "invalid_response", "reranker 响应必须是对象"
                )
            if (
                result.get("success") is False
                or result.get("code")
                or result.get("error")
            ):
                # 不将服务原文（可能含请求数据）写入日志。
                missing = "未找到模型" in str(result.get("message", ""))
                raise RerankerServiceError(
                    "model_unavailable" if missing else "service_error",
                    "reranker 模型未注册或无访问权限；请核对网关开通的模型与独立密钥"
                    if missing
                    else "reranker 业务错误；请核对服务协议和配置",
                )
            usage = result.get("usage") or {}
            container = (
                result.get("output", {}) if self._protocol == "dashscope" else result
            )
            scores = validate_scores(container.get("results"), len(unique_documents))
            by_text = dict(zip(unique_documents, scores))
            return [by_text[document] for document in documents]
        finally:
            input_tokens = (
                usage.get("input_tokens", usage.get("prompt_tokens"))
                if isinstance(usage, dict)
                else None
            )
            if type(input_tokens) is not int or input_tokens < 0:
                input_tokens = None
            record_context_usage(
                input_tokens, None, (time.monotonic() - started) * 1000
            )
            context_call_kind.reset(token)


def validate_scores(results, count: int) -> list[float]:
    """分数必须与全部候选一一对应，缺项不能悄悄补零。"""
    if not isinstance(results, list) or len(results) != count:
        raise ValueError("精排结果数量不匹配")
    scores, seen = [0.0] * count, set()
    for item in results:
        index = item.get("index") if isinstance(item, dict) else None
        if type(index) is not int or not 0 <= index < count or index in seen:
            raise ValueError("精排候选索引不合法或重复")
        score = item.get("relevance_score", item.get("score"))
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("精排分数必须为有限数值")
        scores[index] = float(score)
        seen.add(index)
    return scores
