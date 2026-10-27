# -*- coding: utf-8 -*-
"""工具活动的展示白名单；禁止用任意输入/输出截断充当安全摘要。"""
from __future__ import annotations

import json
import math
import re
from typing import Any


def _number(value: Any) -> bool:
    return type(value) in {int, float} and 0 <= value <= 1_000_000_000 and math.isfinite(value)


def _code(value: Any, pattern: str) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def parse_object(content: str) -> Any:
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return None


def input_summary(name: str, arguments: str) -> str | None:
    data = parse_object(arguments)
    if not isinstance(data, dict):
        return None
    parts = []
    if name == "product_search_tool":
        # 查询全文可能携带地址、偏好与凭据，只显示固定格式业务条件。
        for key, label, pattern in (
            ("product_id", "商品", r"P\d{4,10}"),
            ("sku_id", "规格", r"P\d{4,10}-S\d{1,6}"),
            ("ship_to", "配送国家", r"[A-Z]{2}"),
            ("target_currency", "币种", r"[A-Z]{3}"),
        ):
            value = _code(data.get(key), pattern)
            if value:
                parts.append(f"{label} {value}")
        if _number(data.get("price_max_major")):
            parts.append(f"预算上限 {data['price_max_major']}")
        if type(data.get("top_k")) is int and 0 < data["top_k"] <= 100:
            parts.append(f"最多 {data['top_k']} 件")
    elif name in {"category_insight_tool", "web_search_tool"}:
        if type(data.get("top_k")) is int and 0 < data["top_k"] <= 100:
            parts.append(f"最多 {data['top_k']} 条资料")
    elif name == "task_dispatch":
        agent = data.get("subagent_type")
        label = {"search_agent": "商品检索专家", "trade_agent": "交易专家"}.get(agent) if isinstance(agent, str) else None
        if label:
            parts.append(label)
    elif name == "load_agent_skill_tool":
        # 不展示任意 title/body；固定格式版本仍可用于核对方案调用。
        version = _code(data.get("version"), r"v?\d{1,4}(?:\.\d{1,4}){0,3}")
        if version:
            parts.append(f"方案版本 {version}")
    elif name in {"remember_preference_tool", "forget_preference_tool", "update_preference_tool"}:
        parts.append("购物偏好操作；具体内容见确认卡")
    elif name == "create_order_tool":
        items = data.get("items")
        if isinstance(items, list):
            parts.append(f"{len(items)} 项商品；具体规格和地址见确认卡")
        elif type(data.get("quantity")) is int and 0 < data["quantity"] <= 1000:
            parts.append(f"{data['quantity']} 件；具体规格和地址见确认卡")
    elif name in {"query_order_tool", "cancel_order_tool"}:
        parts.append("订单操作；详情见订单卡")
    return " · ".join(parts)[:240] or None


def result_summary(name: str, content: str, status: str) -> tuple[str, str]:
    """框架 SUCCESS 之外，识别工具明确返回的失败，绝不透传错误原文。"""
    data = parse_object(content)
    if status == "completed" and (
        content.lstrip().startswith("[error]")
        or isinstance(data, dict) and (
            data.get("success") is False or data.get("ok") is False
            or bool(data.get("error")) or data.get("status") == "error" or data.get("status") == "failed"
        )
    ):
        status = "error"
    if status != "completed":
        return status, {
            "error": "工具执行失败，请查看本轮回复或重试。",
            "cancelled": "此操作已取消或被拒绝。",
            "interrupted": "执行中断，尚未取得完整结果。",
            "running": "工具仍在执行，尚未取得最终结果。",
        }.get(status, "尚未取得最终结果。")
    if name == "product_search_tool" and isinstance(data, dict) and isinstance(data.get("hits"), list):
        return status, f"检索返回 {len(data['hits'])} 件商品。"
    if name == "category_insight_tool" and isinstance(data, dict) and isinstance(data.get("insights"), list):
        return status, f"返回 {len(data['insights'])} 条选购资料。"
    if name == "lookup_strategy_memory_tool" and isinstance(data, list):
        return status, f"找到 {len(data)} 条审核建议。"
    if name in {"create_order_tool", "cancel_order_tool"} and isinstance(data, dict) and data.get("confirmation_required") is True:
        return status, "已准备确认卡，等待你的决定。"
    return status, {
        "load_agent_skill_tool": "选购方案读取结束。",
        "remember_preference_tool": "购物偏好保存操作已完成。",
        "forget_preference_tool": "购物偏好删除操作已完成。",
        "update_preference_tool": "购物偏好修改操作已完成。",
        "query_order_tool": "订单查询已完成。",
        "task_dispatch": "专家任务已返回。",
    }.get(name, "工具执行已完成。")
