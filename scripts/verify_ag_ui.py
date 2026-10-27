# -*- coding: utf-8 -*-
"""连接真实 AG-UI SSE，校验协议并输出不含消息正文的 JSON 报告。

不会自行启动服务，也不会在 import 时运行。例：
    uv run python scripts/verify_ag_ui.py --output eval/ag-ui-verification.json
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
import uuid

import httpx
from ag_ui.core import Event
from pydantic import TypeAdapter

EVENT_ADAPTER = TypeAdapter(Event)
_FORBIDDEN_TOOLS = {"create_order_tool", "query_order_tool", "cancel_order_tool", "remember_preference_tool", "forget_preference_tool"}


class ContractViolation(ValueError):
    """不包含原始工具参数或模型正文的合同错误。"""


class StreamAudit:
    def __init__(self, thread_id: str, run_id: str, minimum_products: int = 1) -> None:
        self.thread_id, self.run_id = thread_id, run_id
        self.minimum_products = minimum_products
        self.counts: Counter[str] = Counter()
        self.messages: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.retrieval_evidence: list[dict[str, Any]] = []
        self.unparsed_product_search_results = 0
        self._args: dict[str, str] = {}
        self._result_ids: set[str] = set()
        self.first_event_ms: float | None = None
        self.first_text_ms: float | None = None
        self.final_state: dict[str, Any] = {}
        self.final_message_characters = 0
        self.terminal: str | None = None

    def accept(self, data: str, elapsed_ms: float) -> None:
        # 使用官方联合事件模型，而非只用 BaseEvent 放过各事件的必需字段。
        try:
            event = EVENT_ADAPTER.validate_json(data).model_dump(mode="json", by_alias=True, exclude_none=True)
        except Exception as err:
            # Pydantic 错误默认会回显输入，报告中只保留错误类型。
            raise ContractViolation("official_event_schema_invalid") from err
        kind = event["type"]
        if self.terminal is not None:
            raise ContractViolation("event_after_terminal")
        if not self.counts and kind != "RUN_STARTED":
            raise ContractViolation("first_event_is_not_run_started")
        self.counts[kind] += 1
        if self.first_event_ms is None:
            self.first_event_ms = round(elapsed_ms, 2)

        if kind == "RUN_STARTED":
            if self.counts[kind] != 1 or event["threadId"] != self.thread_id or event["runId"] != self.run_id:
                raise ContractViolation("run_started_identity_or_count_mismatch")
        elif kind == "TEXT_MESSAGE_START":
            identifier = event["messageId"]
            if identifier in self.messages:
                raise ContractViolation("duplicate_text_message_start")
            self.messages[identifier] = {"id": identifier, "characters": 0, "ended": False}
        elif kind in {"TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"}:
            message = self.messages.get(event["messageId"])
            if message is None or message["ended"]:
                raise ContractViolation("text_event_without_open_message")
            if kind == "TEXT_MESSAGE_CONTENT":
                message["characters"] += len(event["delta"])
                if event["delta"] and self.first_text_ms is None:
                    self.first_text_ms = round(elapsed_ms, 2)
            else:
                message["ended"] = True
        elif kind == "TOOL_CALL_START":
            identifier = event["toolCallId"]
            if event["toolCallName"] in _FORBIDDEN_TOOLS:
                raise ContractViolation("out_of_scope_tool_requested_in_search_verification")
            if identifier in self.tools:
                raise ContractViolation("duplicate_tool_call_start")
            self.tools[identifier] = {
                "id": identifier, "name": event["toolCallName"], "argumentsCharacters": 0,
                "argumentsEnded": False, "argumentsJsonValid": False, "resultReceived": False,
            }
            self._args[identifier] = ""
        elif kind in {"TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT"}:
            identifier = event["toolCallId"]
            tool = self.tools.get(identifier)
            if tool is None:
                raise ContractViolation("tool_event_without_call_start")
            if kind == "TOOL_CALL_ARGS":
                if tool["argumentsEnded"]:
                    raise ContractViolation("tool_arguments_after_end")
                self._args[identifier] += event["delta"]
                tool["argumentsCharacters"] += len(event["delta"])
            elif kind == "TOOL_CALL_END":
                if tool["argumentsEnded"]:
                    raise ContractViolation("duplicate_tool_call_end")
                tool["argumentsEnded"] = True
                try:
                    arguments = json.loads(self._args.pop(identifier))
                except ValueError:
                    arguments = None
                # END 只保证参数齐备；JSON 有效性是质量观察项，不能误报成协议违规。
                tool["argumentsJsonValid"] = isinstance(arguments, dict)
                if tool["name"] == "task_dispatch" and isinstance(arguments, dict) and arguments.get("subagent_type") == "trade_agent":
                    raise ContractViolation("trade_agent_requested_in_read_only_verification")
            else:
                if not tool["argumentsEnded"] or tool["resultReceived"]:
                    raise ContractViolation("tool_result_before_args_end_or_duplicate")
                if event["messageId"] in self._result_ids or event["messageId"] in self.messages:
                    raise ContractViolation("duplicate_tool_result_message_id")
                self._result_ids.add(event["messageId"])
                tool.update(resultReceived=True, resultMessageId=event["messageId"], resultCharacters=len(event["content"]))
                if tool["name"] == "product_search_tool":
                    self._record_retrieval(identifier, event["content"])
        elif kind == "STATE_SNAPSHOT":
            snapshot = event["snapshot"]
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("products"), list):
                raise ContractViolation("invalid_products_snapshot")
            self.final_state = {
                "status": snapshot.get("status"), "searchCompleted": snapshot.get("searchCompleted"),
                "productCount": len(snapshot["products"]),
                "productIds": [product.get("product_id") for product in snapshot["products"] if isinstance(product, dict)],
                "progressCount": len(snapshot.get("progress", [])),
            }
        elif kind == "MESSAGES_SNAPSHOT":
            assistants = [message for message in event["messages"] if message["role"] == "assistant"]
            if assistants:
                self.final_message_characters = len(str(assistants[-1].get("content", "")))
        elif kind == "RUN_FINISHED":
            if event["threadId"] != self.thread_id or event["runId"] != self.run_id:
                raise ContractViolation("run_finished_identity_mismatch")
            self.terminal = kind
        elif kind == "RUN_ERROR":
            self.terminal = kind

    def finish(self) -> None:
        if self.terminal != "RUN_FINISHED":
            raise ContractViolation("run_failed_or_terminal_missing")
        if any(not message["ended"] for message in self.messages.values()):
            raise ContractViolation("text_message_left_open")
        if not self.tools or any(not tool["argumentsEnded"] or not tool["resultReceived"] for tool in self.tools.values()):
            raise ContractViolation("tool_result_missing")
        if self.final_state.get("status") != "completed" or self.final_state.get("searchCompleted") is not True:
            raise ContractViolation("search_not_completed")
        if self.final_state.get("productCount", 0) < self.minimum_products:
            raise ContractViolation("insufficient_products")
        if self.first_text_ms is None or self.final_message_characters <= 0:
            raise ContractViolation("streamed_or_final_text_missing")

    def _record_retrieval(self, tool_call_id: str, content: str) -> None:
        """只保留工具真实 JSON 的检索模式证据，绝不据商品出现推断重排成功。"""
        try:
            result = json.loads(content)
        except ValueError:
            self.unparsed_product_search_results += 1
            return
        if not isinstance(result, dict):
            self.unparsed_product_search_results += 1
            return
        strategy = result.get("recall_strategy")
        reranked = result.get("rerank_applied")
        total = result.get("total_candidates")
        hits = result.get("hits")
        self.retrieval_evidence.append({
            "toolCallId": tool_call_id,
            "recall_strategy": strategy if isinstance(strategy, str) else None,
            "rerank_applied": reranked if isinstance(reranked, bool) else None,
            "hit_count": len(hits) if isinstance(hits, list) else None,
            "total_candidates": total if isinstance(total, int) and not isinstance(total, bool) else None,
        })

    def report(self, total_ms: float) -> dict:
        strategies = sorted({item["recall_strategy"] for item in self.retrieval_evidence if item["recall_strategy"]})
        full_main_chain = bool(self.retrieval_evidence) and not self.unparsed_product_search_results and all(
            item["recall_strategy"] == "embedding_rerank" and item["rerank_applied"] is True
            for item in self.retrieval_evidence
        )
        return {
            "threadId": self.thread_id, "runId": self.run_id,
            "timingMs": {"firstEvent": self.first_event_ms, "firstText": self.first_text_ms, "total": round(total_ms, 2)},
            "eventCounts": dict(self.counts), "terminal": self.terminal,
            "messages": list(self.messages.values()), "tools": list(self.tools.values()),
            "finalState": self.final_state, "finalAnswerCharacters": self.final_message_characters,
            "retrievalEvidence": self.retrieval_evidence,
            "retrievalSummary": {
                "observedStrategies": strategies,
                "allObservedSearchesUseEmbeddingAndRerank": full_main_chain,
                "unparsedProductSearchResults": self.unparsed_product_search_results,
                # 本脚本校验协议和商品展示，不计算正式检索集的 Recall/MRR/NDCG 门槛。
                "onlineMainQualityGates": "not_evaluated",
            },
        }


async def verify(base_url: str, query: str, timeout: float, minimum_products: int) -> dict:
    thread_id, run_id = f"verify-{uuid.uuid4().hex}", f"run-{uuid.uuid4().hex}"
    audit = StreamAudit(thread_id, run_id, minimum_products)
    request = {
        "threadId": thread_id, "runId": run_id, "state": {}, "tools": [], "context": [],
        "forwardedProps": {"buyerId": "ag-ui-verifier", "locale": "zh-CN", "currency": "CNY"},
        "messages": [{
            "id": f"user-{uuid.uuid4().hex}", "role": "user",
            "content": f"仅做商品检索并展示候选，不执行任何交易或偏好写入。请调用商品检索工具：{query}",
        }],
    }
    started = time.perf_counter()
    error = None
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as client:
                async with client.stream("POST", f"{base_url.rstrip('/')}/commerce/ag-ui/run", json=request) as response:
                    response.raise_for_status()
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ContractViolation("response_is_not_sse")
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if not line:
                            if data_lines:
                                audit.accept("\n".join(data_lines), (time.perf_counter() - started) * 1000)
                                data_lines.clear()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip(" "))
                        # SSE 注释/心跳及传输字段不作为协议事件。
                    if data_lines:
                        raise ContractViolation("incomplete_sse_frame_at_eof")
        audit.finish()
    except ContractViolation as err:
        error = str(err)
    except httpx.HTTPStatusError as err:
        error = f"http_status_{err.response.status_code}"
    except (httpx.HTTPError, TimeoutError) as err:
        error = type(err).__name__
    return {
        "ok": error is None, "error": error,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "sdkVersion": importlib.metadata.version("ag-ui-protocol"),
        "mode": "read_only_product_search",
        **audit.report((time.perf_counter() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--query", default="推荐三款 300 元以内的旅行三件套")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--min-products", type=int, default=1)
    parser.add_argument("--output", type=Path, help="可选 JSON 报告路径；不记录工具参数和回复正文")
    args = parser.parse_args()
    if not args.query.strip() or re.search(r"下单|支付|取消|订单|记住|忘记|删除|购买它", args.query):
        parser.error("--query 必须是只读商品检索，不能包含交易或偏好修改指令")
    if args.timeout <= 0 or args.min_products < 0:
        parser.error("timeout 必须为正数，min-products 不能为负数")
    result = asyncio.run(verify(args.base_url, args.query, args.timeout, args.min_products))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
