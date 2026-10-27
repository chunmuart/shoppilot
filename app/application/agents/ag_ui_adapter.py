# -*- coding: utf-8 -*-
"""将真实 AgentScope 事件和业务结果投影为 AG-UI，不承担业务决策。"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Callable

from ag_ui.core import (
    AssistantMessage,
    BaseEvent,
    CustomEvent,
    MessagesSnapshotEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from app.infrastructure.eventbus import TradeEvent
from app.application.agents.product_candidate_projection import ProductCandidateProjection
from app.application.agents.activity_summary import input_summary, result_summary

_TOOL_LABELS = {
    "product_search_tool": "检索商品",
    "category_insight_tool": "查询选购知识",
    "web_search_tool": "核实外部资料",
    "task_dispatch": "协调专家任务",
    "remember_preference_tool": "保存购物偏好",
    "forget_preference_tool": "删除购物偏好",
    "update_preference_tool": "修改购物偏好",
    "create_order_tool": "准备订单意向",
    "query_order_tool": "查询订单",
    "cancel_order_tool": "准备取消确认",
    "load_agent_skill_tool": "读取选购方案",
    "lookup_strategy_memory_tool": "查阅审核选购建议",
}


class AGUIRunAdapter:
    def __init__(self, request: RunAgentInput, emit: Callable[[BaseEvent], None], *, authoritative_state: dict | None = None) -> None:
        self.request = request
        self.emit = emit
        last = request.messages[-1] if request.messages else None
        raw_query = last.content if last is not None and last.role == "user" and isinstance(last.content, str) else ""
        self._products = ProductCandidateProjection(raw_query)
        self.state: dict[str, Any] = {
            "products": [],
            "shoppingForm": None,
            "confirmations": [],
            "toolApprovals": [],
            "skillUsages": [],
            "searchCompleted": False,
            "status": "queued",
            "progress": [],
            "activities": [],
        }
        if authoritative_state:
            self.state.update({k:copy.deepcopy(authoritative_state[k]) for k in ("products","searchCompleted","skillUsages","shoppingForm") if k in authoritative_state})
        self.error: str | None = None
        self._text_open: set[str] = set()
        self._tool_open: set[str] = set()
        self._tool_names: dict[str, str] = {}
        self._tool_output: dict[str, list[str]] = {}
        self._skill_args: dict[str, str] = {}
        self._activity_args: dict[str, str] = {}
        self._thinking_published: dict[str, float] = {}
        self._completed = False

    def _id(self, kind: str, original: str) -> str:
        return f"{self.request.run_id}:{kind}:{original}"

    def snapshot(self) -> None:
        # 队列消费者稍后才序列化，必须隔离后续原地状态修改。
        self.emit(StateSnapshotEvent(snapshot=copy.deepcopy(self.state)))

    def start(self) -> None:
        self.emit(RunStartedEvent(thread_id=self.request.thread_id, run_id=self.request.run_id))
        self.snapshot()

    def _progress(self, identifier: str, label: str, status: str) -> None:
        entries = self.state["progress"]
        entry = next((entry for entry in entries if entry["id"] == identifier), None)
        if entry is None:
            entries.append({"id": identifier, "label": label, "status": status})
        else:
            entry.update(label=label, status=status)

    def _activity(self, identifier: str, kind: str, *, name: str = "") -> dict:
        entries = self.state["activities"]
        entry = next((item for item in entries if item["id"] == identifier), None)
        if entry is None:
            entry = {"id": identifier, "runId": self.request.run_id, "kind": kind,
                     "label": "Thinking" if kind == "thinking" else _TOOL_LABELS.get(name, "执行工具"),
                     "status": "running" if kind == "thinking" else "preparing",
                     "sequence": len(entries) + 1,
                     # 记录服务端收到事件的时间，不伪称供应商执行耗时。
                     "startedAt": int(time.time() * 1000), "finishedAt": None,
                     "source": "provider" if kind == "thinking" else "agentscope"}
            if kind == "tool":
                entry.update(toolCallId=identifier, toolName=name)
            else:
                entry["content"] = ""
            entries.append(entry)
        elif kind == "tool" and name:
            entry.update(toolName=name, label=_TOOL_LABELS.get(name, "执行工具"))
        return entry

    def _publish_activity(self, entry: dict) -> None:
        # Thinking 只发布单条活动，不为每个 token 重复复制商品和整轮状态。
        self.emit(CustomEvent(name="activity.upsert", value=copy.deepcopy(entry)))

    def _tool_activity(self, original_id: str, **updates) -> dict:
        entry = self._activity(self._id("tool", original_id), "tool", name=self._tool_names.get(original_id, ""))
        entry.update(updates)
        if entry["status"] in {"completed", "error", "cancelled", "interrupted"}:
            entry["finishedAt"] = entry["finishedAt"] or int(time.time() * 1000)
        self._publish_activity(entry)
        return entry

    def _close_activities(self, status: str, *, keep_waiting: bool = False) -> None:
        for entry in self.state["activities"]:
            if entry["status"] in {"preparing", "running", "waiting"}:
                if keep_waiting and entry["status"] == "waiting":
                    continue
                entry.update(status=status, finishedAt=int(time.time() * 1000))
                self._publish_activity(entry)

    def on_agent_event(self, event: Any) -> None:
        """保留框架原始的开始/增量/结束边界，不从工具名称推测调用关联。"""
        kind = getattr(event, "type", None)
        if kind == "REPLY_START":
            self.state["status"] = "running"
            self.snapshot()
        elif kind == "TEXT_BLOCK_START":
            message_id = self._id("text", event.block_id)
            self._text_open.add(message_id)
            self.emit(TextMessageStartEvent(message_id=message_id, role="assistant"))
        elif kind == "TEXT_BLOCK_DELTA":
            if event.delta:
                self.emit(TextMessageContentEvent(
                    message_id=self._id("text", event.block_id), delta=event.delta,
                ))
        elif kind == "TEXT_BLOCK_END":
            message_id = self._id("text", event.block_id)
            self._text_open.discard(message_id)
            self.emit(TextMessageEndEvent(message_id=message_id))
        elif kind in {"THINKING_BLOCK_START", "THINKING_BLOCK_DELTA", "THINKING_BLOCK_END"}:
            # AgentScope 2.0.8 将供应商公开返回的 thinking/reasoning 映射到这些事件。
            # 不读取 memory、系统提示、隐藏字段或从回答反推 Thinking。
            identifier = self._id("thinking", f"{event.reply_id}:{event.block_id}")
            entry = self._activity(identifier, "thinking")
            if kind == "THINKING_BLOCK_DELTA":
                delta = event.delta if isinstance(event.delta, str) else ""
                remaining = max(0, 8000 - len(entry["content"]))
                entry["content"] += delta[:remaining]
                if len(delta) > remaining:
                    entry["truncated"] = True
                now = time.monotonic()
                if now - self._thinking_published.get(identifier, 0) >= 0.2 and remaining:
                    self._publish_activity(entry)
                    self._thinking_published[identifier] = now
            elif kind == "THINKING_BLOCK_END":
                entry.update(status="completed", finishedAt=int(time.time() * 1000))
                self._publish_activity(entry)
            else:
                self._publish_activity(entry)
                self._thinking_published[identifier] = time.monotonic()
        elif kind == "TOOL_CALL_START":
            call_id = self._id("tool", event.tool_call_id)
            self._tool_names[event.tool_call_id] = event.tool_call_name
            self._tool_open.add(call_id)
            if event.tool_call_name == "load_agent_skill_tool":
                self._skill_args[event.tool_call_id] = ""
            self._activity_args[event.tool_call_id] = ""
            self.emit(ToolCallStartEvent(tool_call_id=call_id, tool_call_name=event.tool_call_name))
            self._tool_activity(event.tool_call_id)
        elif kind == "TOOL_CALL_DELTA":
            if event.delta:
                if event.tool_call_id in self._skill_args:
                    self._skill_args[event.tool_call_id] = (self._skill_args[event.tool_call_id] + event.delta)[:2048]
                self._activity_args[event.tool_call_id] = (self._activity_args.get(event.tool_call_id, "") + event.delta)[:16384]
                self.emit(ToolCallArgsEvent(
                    tool_call_id=self._id("tool", event.tool_call_id), delta=event.delta,
                ))
        elif kind == "TOOL_CALL_END":
            call_id = self._id("tool", event.tool_call_id)
            self._tool_open.discard(call_id)
            # END 仅表示参数流结束；工具是否完成由 RESULT 表达。
            self.emit(ToolCallEndEvent(tool_call_id=call_id))
            summary = input_summary(self._tool_names.get(event.tool_call_id, ""), self._activity_args.pop(event.tool_call_id, ""))
            self._tool_activity(event.tool_call_id, **({"inputSummary": summary} if summary else {}))
        elif kind == "TOOL_RESULT_START":
            name = event.tool_call_name
            self._tool_names[event.tool_call_id] = name
            self._tool_output[event.tool_call_id] = []
            if name == "load_agent_skill_tool":
                self._skill_reading(event.tool_call_id)
            self._progress(self._id("tool", event.tool_call_id), _TOOL_LABELS.get(name, name), "running")
            self._tool_activity(event.tool_call_id, status="running")
            self.snapshot()
        elif kind == "TOOL_RESULT_TEXT_DELTA":
            self._tool_output.setdefault(event.tool_call_id, []).append(event.delta)
        elif kind == "TOOL_RESULT_DATA_DELTA":
            # 媒体结果保留其真实元数据，不在通用日志里重复传播大块 base64。
            self._tool_output.setdefault(event.tool_call_id, []).append(json.dumps({
                "media_type": event.media_type, "url": event.url,
            }, ensure_ascii=False))
        elif kind == "TOOL_RESULT_END":
            call_id = self._id("tool", event.tool_call_id)
            name = self._tool_names.get(event.tool_call_id, "工具")
            success = str(event.state).lower() == "success"
            content = "".join(self._tool_output.pop(event.tool_call_id, []))
            status = {"success": "completed", "error": "error", "denied": "cancelled",
                      "interrupted": "interrupted", "running": "running"}.get(str(event.state).lower(), "error")
            status, summary = result_summary(name, content, status)
            if name == "load_agent_skill_tool":
                self._skill_result(event.tool_call_id, content, success)
                if next(item for item in self.state["skillUsages"] if item["toolCallId"] == call_id)["status"] == "error":
                    status, summary = "error", "方案读取未成功或版本已变化。"
            self.emit(ToolCallResultEvent(
                message_id=self._id("result", event.tool_call_id),
                tool_call_id=call_id,
                content=content,
                role="tool",
            ))
            self._progress(call_id, _TOOL_LABELS.get(name, name), "completed" if success else "error")
            self._tool_activity(event.tool_call_id, status=status, resultSummary=summary)
            self.snapshot()
        elif kind == "REPLY_END":
            reason = str(event.finished_reason).lower()
            if reason in {"error", "interrupted", "exceed_max_iters"}:
                self.error = "本轮执行未正常完成，请重试或缩小问题范围。"
        elif kind == "REQUIRE_USER_CONFIRM":
            self.state["status"]="awaiting_confirmation"
            for call in event.tool_calls:
                item={"id":f"{event.reply_id}:{call.id}","tool":call.name,"label":_TOOL_LABELS.get(call.name,call.name),"arguments":call.input,
                      "toolCallId":self._id("tool", call.id)}
                if not any(p["id"]==item["id"] for p in self.state["toolApprovals"]):self.state["toolApprovals"].append(item)
                self._tool_names[call.id] = call.name
                self._tool_activity(call.id, status="waiting", resultSummary="等待你的批准或拒绝。")
            self.snapshot()
        elif kind == "REQUIRE_EXTERNAL_EXECUTION":
            # 首版不接受 resume，遇到暂停必须如实结束，不能把未执行动作展示成成功。
            self.error = "本次操作需要人工确认或外部执行，当前流式入口尚未接入此续接流程。"

    def _skill_reading(self, original_id: str) -> None:
        call_id = self._id("tool", original_id)
        if any(item["toolCallId"] == call_id for item in self.state["skillUsages"]):
            return
        item = {"toolCallId": call_id, "status": "reading"}
        try:
            arguments = json.loads(self._skill_args.get(original_id, ""))
            for source, target in (("skill_id", "id"), ("version", "version")):
                value = arguments.get(source) if isinstance(arguments, dict) else None
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
                    item[target] = value
        except (ValueError, TypeError):
            pass
        self.state["skillUsages"].append(item)

    def _skill_result(self, original_id: str, content: str, success: bool) -> None:
        self._skill_reading(original_id)
        call_id = self._id("tool", original_id)
        item = next(item for item in self.state["skillUsages"] if item["toolCallId"] == call_id)
        self._skill_args.pop(original_id, None)
        try:
            loaded = json.loads(content)
            if (not success or not isinstance(loaded, dict) or loaded.get("kind") != "skill"
                    or loaded.get("authority") != "reference_only"
                    or not all(isinstance(loaded.get(key), str) and loaded[key] for key in ("id", "version", "title", "body", "content_hash"))
                    or not re.fullmatch(r"[a-f0-9]{64}", loaded["content_hash"])
                    or any(item.get(key) is not None and item[key] != loaded[key] for key in ("id", "version"))):
                raise ValueError("不能确认方案读取成功")
            item.update(id=loaded["id"], version=loaded["version"], title=loaded["title"],
                        contentHash=loaded["content_hash"], status="used")
            item.pop("error", None)
        except (ValueError, TypeError):
            item.update(status="error", error="方案读取未成功或版本已变化，请刷新方案后重试。")

    def _close_skill_reads(self, message: str) -> None:
        for item in self.state["skillUsages"]:
            if item["status"] == "reading":
                item.update(status="error", error=message)

    def on_trade_event(self, event: TradeEvent) -> None:
        if event.shopping_session_id != self.request.thread_id:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "skill.preload":
            selected = self.request.forwarded_props.get("selectedSkill") if isinstance(self.request.forwarded_props, dict) else None
            if (not isinstance(selected, dict) or payload.get("source") != "server_preload"
                    or any(payload.get(key) != selected.get(key) for key in ("id", "version", "contentHash"))
                    or payload.get("status") not in {"reading", "used", "error"}
                    or not isinstance(payload.get("operationId"), str)):
                return
            call_id = self._id("preload", payload["operationId"])
            item = {"toolCallId": call_id, "source": "server_preload", "status": payload["status"],
                    **{key: payload[key] for key in ("id", "version", "contentHash")}}
            if isinstance(payload.get("title"), str):
                item["title"] = payload["title"]
            if payload["status"] == "error":
                item["error"] = "所选方案读取失败，请刷新方案，必要时新建选购后重试。"
                self.error = item["error"]
            self.state["skillUsages"] = [*[
                previous for previous in self.state["skillUsages"] if previous["toolCallId"] != call_id], item]
            self._progress(call_id, "读取所选方案", {"reading": "running", "used": "completed", "error": "error"}[payload["status"]])
            # 明确是服务端确定读取，不发送任何伪造的模型 TOOL_CALL_* 事件。
            self.emit(CustomEvent(name="skill.preload", value=copy.deepcopy(item)))
            self.snapshot()
        elif event.type == "ui.surface":
            form = payload.get("form")
            if (isinstance(form, dict) and form.get("session_id") == self.request.thread_id
                    and isinstance(self.request.forwarded_props, dict)
                    and payload.get("buyer_id") == self.request.forwarded_props.get("buyerId")):
                self.state["shoppingForm"] = copy.deepcopy(form)
                for message in form.get("messages", []):
                    self.emit(CustomEvent(name="a2ui", value=copy.deepcopy(message)))
                self.snapshot()
        elif event.type == "tool.result" and self._products.apply(payload):
            # 明确多 ID 的并发精确检索按本轮原始顺序合并，其余查询保持替换语义。
            self.state["products"] = copy.deepcopy(self._products.result["hits"])
            self.state["searchCompleted"] = True
            self.snapshot()
        elif event.type in {"confirmation.required", "confirmation.resolved"}:
            confirmation = payload.get("confirmation")
            if (isinstance(confirmation, dict) and isinstance(self.request.forwarded_props, dict)
                    and confirmation.get("buyer_id") == self.request.forwarded_props.get("buyerId")
                    and confirmation.get("session_id") == self.request.thread_id):
                previous = self.state["confirmations"]
                self.state["confirmations"] = [copy.deepcopy(confirmation), *[
                    item for item in previous if item["confirmation_id"] != confirmation["confirmation_id"]
                ]][:20]
                self.snapshot()
        elif event.type == "plan.update":
            for task in payload.get("tasks", []):
                task_state = str(task.get("state", "")).lower()
                status = "completed" if task_state in {"completed", "done"} else "running"
                self._progress(f"plan:{task['id']}", task.get("subject", "任务"), status)
            self.snapshot()
        elif event.type in {"agent.dispatch", "cache.hit", "model.fallback", "context.compressed", "error"}:
            self.emit(CustomEvent(name=event.type, value=payload))
            if event.type == "context.compressed":
                self.state["contextStatistics"] = payload
                self.snapshot()

    def _close_streams(self) -> None:
        # 中断/错误时关闭已打开的消息和参数流，客户端不会残留永久 loading。
        for message_id in sorted(self._text_open):
            self.emit(TextMessageEndEvent(message_id=message_id))
        self._text_open.clear()
        for call_id in sorted(self._tool_open):
            self.emit(ToolCallEndEvent(tool_call_id=call_id))
        self._tool_open.clear()

    def finish(self, final_text: str) -> None:
        if self._completed:
            return
        self._completed = True
        self._close_streams()
        self._close_skill_reads("未收到方案读取成功的结果。")
        self._close_activities("interrupted", keep_waiting=bool(self.state["toolApprovals"]))
        # 用审核后的最终回答收口；保留客户端传入的全部历史与本轮用户消息。
        self.emit(MessagesSnapshotEvent(messages=[
            *self.request.messages,
            AssistantMessage(id=self._id("final", "answer"), content=final_text),
        ]))
        self.state["status"] = "awaiting_confirmation" if self.state["toolApprovals"] else "completed"
        self.snapshot()
        outcome={"type":"interrupt","interrupts":[{"id":p["id"],"reason":"tool_confirmation","message":p["label"]} for p in self.state["toolApprovals"]]} if self.state["toolApprovals"] else {"type":"success"}
        self.emit(RunFinishedEvent(thread_id=self.request.thread_id, run_id=self.request.run_id,outcome=outcome))

    def fail(self, message: str, *, cancelled: bool = False, code: str | None = None) -> None:
        if self._completed:
            return
        self._completed = True
        self._close_streams()
        self._close_skill_reads("本轮已中断，方案读取尚未完成。")
        self._close_activities("cancelled" if cancelled else "interrupted" if code == "SERVER_RESTART" else "error")
        self.state["status"] = "cancelled" if cancelled else "error"
        for entry in self.state["progress"]:
            if entry["status"] == "running":
                entry["status"] = "error"
        self.snapshot()
        self.emit(RunErrorEvent(message=message, code="CANCELLED" if cancelled else (code or "AGENT_ERROR")))
