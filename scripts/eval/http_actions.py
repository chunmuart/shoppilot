# -*- coding: utf-8 -*-
"""固定评测脚本中的用户 HTTP 动作；绝不从对话文本推断 approved。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx


class BuyerAPIClient:
    """令牌只用于当前评测服务；Judge 继续使用独立的网关凭据。"""
    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str | None) -> None:
        self._client, self._base_url, self._token = client, base_url, token

    async def _call(self, method: str, url: str, **kwargs):
        expected, actual = urlsplit(self._base_url), urlsplit(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise ValueError("评测买家令牌不能发送到其他服务")
        headers = dict(kwargs.pop("headers", {}))
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return await getattr(self._client, method)(url, headers=headers, **kwargs)

    async def get(self, url: str, **kwargs):
        return await self._call("get", url, **kwargs)

    async def post(self, url: str, **kwargs):
        return await self._call("post", url, **kwargs)


def validate_http_actions(case: dict) -> list[dict]:
    actions = case.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("case.actions 必须是显式动作列表")
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"type", "after_turn", "action", "approved", "expected_payload"}:
            raise ValueError("确认动作必须且只能声明 type/after_turn/action/approved/expected_payload")
        if action["type"] != "resolve_confirmation" or action["action"] not in {"create", "cancel"}:
            raise ValueError("未知 HTTP 确认动作")
        if type(action["approved"]) is not bool:
            raise ValueError("HTTP 动作 approved 必须为字面布尔值，不能来自聊天字符串")
        if type(action["after_turn"]) is not int or not 1 <= action["after_turn"] <= len(case.get("queries", [])):
            raise ValueError("HTTP 动作 after_turn 必须指向真实对话轮次")
        expected = action["expected_payload"]
        if not isinstance(expected, dict) or not {"items", "shipping_address", "currency", "total_amount_minor", "amount_scope", "order_kind"} <= set(expected):
            raise ValueError("HTTP 确认动作必须绑定商品、数量、价格币种、地址与意向范围")
        if expected["amount_scope"] != "merchandise_only" or expected["order_kind"] != "purchase_intent":
            raise ValueError("评测动作只支持商品金额意向单")
        items = expected["items"]
        if not isinstance(items, list) or not items:
            raise ValueError("动作 items 不得为空")
        for item in items:
            if not isinstance(item, dict) or not {"product_id", "sku_id", "quantity", "unit_price_minor", "currency"} <= set(item):
                raise ValueError("每个确认 SKU 必须绑定完整价格与数量")
            if type(item["quantity"]) is not int or item["quantity"] <= 0:
                raise ValueError("动作数量必须是正整数")
            if type(item["unit_price_minor"]) is not int or item["unit_price_minor"] < 0:
                raise ValueError("动作价格必须是非负整数分")
        if not isinstance(expected["shipping_address"], dict) or not {"recipient_name", "country", "state", "city", "address_line", "postal_code", "phone"} <= set(expected["shipping_address"]):
            raise ValueError("动作必须声明完整收货地址")
    return actions


def matches_expected(actual: Any, expected: Any) -> bool:
    """忽略服务器补充的标题等展示字段，但所有声明的交易字段严格比较。"""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and matches_expected(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches_expected(item, value) for item, value in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


async def capture_confirmations(client: httpx.AsyncClient, base_url: str, buyer_id: str, session_id: str,
                                events: list[dict], after_turn: int) -> list[dict]:
    response = await client.get(f"{base_url}/commerce/confirmations", params={"buyer_id": buyer_id, "session_id": session_id}, timeout=30)
    response.raise_for_status()
    rows = response.json().get("confirmations")
    if not isinstance(rows, list):
        raise ValueError("持久确认列表响应不合法")
    events.append({"type": "eval.confirmations.snapshot", "payload": {"after_turn": after_turn, "buyer_id": buyer_id, "session_id": session_id, "confirmations": rows}})
    return rows


async def execute_confirmation_action(client: httpx.AsyncClient, base_url: str, buyer_id: str, session_id: str,
                                      action: dict, confirmations: list[dict], events: list[dict]) -> dict:
    if type(action.get("approved")) is not bool:
        raise ValueError("approved 只能来自显式布尔动作")
    matches = [row for row in confirmations if isinstance(row, dict)
               and row.get("buyer_id") == buyer_id and row.get("session_id") == session_id
               and row.get("action") == action["action"] and row.get("status") == "pending" and not row.get("expired")
               and matches_expected(row.get("payload"), action["expected_payload"])]
    if not matches:
        raise ValueError("没有与显式用户动作的 SKU/数量/价格币种/地址匹配的待确认凭证；不会自动构造下单请求")
    # 服务端列表按新到旧排列；仅对完整交易字段一致的最新凭证模拟点击。
    selected = matches[0]
    confirmation_id = selected["confirmation_id"]
    response = await client.get(f"{base_url}/commerce/confirmations/{confirmation_id}", params={"buyer_id": buyer_id, "session_id": session_id}, timeout=30)
    response.raise_for_status()
    current = response.json().get("confirmation", {})
    if (current.get("confirmation_id") != confirmation_id or current.get("buyer_id") != buyer_id
        or current.get("session_id") != session_id or current.get("action") != action["action"]
        or current.get("status") != "pending" or current.get("expired")
        or current.get("snapshot_hash") != selected.get("snapshot_hash")
        or not matches_expected(current.get("payload"), action["expected_payload"])):
        raise ValueError("点击前权威确认快照已变化或失效，拒绝执行")
    request = {"buyer_id": buyer_id, "session_id": session_id, "snapshot_hash": current["snapshot_hash"], "approved": action["approved"]}
    invocation = {"confirmation_id": confirmation_id, "action": action["action"], "after_turn": action["after_turn"], "request": request, "expected_payload": action["expected_payload"]}
    events.append({"type": "eval.http_action.invoke", "payload": invocation})
    response = await client.post(f"{base_url}/commerce/confirmations/{confirmation_id}/resolve", json=request, timeout=30)
    body = response.json()
    events.append({"type": "eval.http_action.result", "payload": {**invocation, "http_status": response.status_code, "response": body}})
    response.raise_for_status()
    resolved = body.get("confirmation", {})
    decision = "approved" if action["approved"] else "rejected"
    if (resolved.get("confirmation_id") != confirmation_id or resolved.get("snapshot_hash") != current["snapshot_hash"]
        or resolved.get("buyer_id") != buyer_id or resolved.get("session_id") != session_id
        or resolved.get("status") != decision or resolved.get("operation_id") != current.get("operation_id")):
        raise ValueError("HTTP 决议返回的身份/快照/状态不匹配，不能算作成功")
    if not action["approved"]:
        if resolved.get("result") is not None or body.get("order") is not None:
            raise ValueError("拒绝确认后不应生成订单结果")
        return body
    order = body.get("order")
    expected_status = "CONFIRMED" if action["action"] == "create" else "CANCELLED"
    if (not isinstance(order, dict) or order != resolved.get("result") or order.get("status") != expected_status
        or order.get("currency") != action["expected_payload"]["currency"]
        or order.get("total_amount_minor") != action["expected_payload"]["total_amount_minor"]):
        raise ValueError("确认后的订单状态或金额与权威快照不符")
    persisted = await client.get(f"{base_url}/commerce/orders/{order['order_id']}", params={"buyer_id": buyer_id}, timeout=30)
    persisted.raise_for_status()
    snapshot = persisted.json()
    events.append({"type": "eval.order.snapshot", "payload": {"confirmation_id": confirmation_id, "order": snapshot, "buyer_id": buyer_id}})
    if not matches_expected(snapshot, {"order_id": order["order_id"], "buyer_id": buyer_id, "status": expected_status,
                                       "currency": order["currency"], "total_amount_minor": order["total_amount_minor"]}):
        raise ValueError("重新读取的持久订单与确认结果不一致")
    return body
