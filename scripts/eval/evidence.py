# -*- coding: utf-8 -*-
"""把 Agent 事件流转成可复现、不可被 Judge 改写的评测证据。"""
from __future__ import annotations

from typing import Any


class AssertionDefinitionError(ValueError):
    """用例里的确定性断言缺字段或使用了未知类型。"""


def _tool_names(events: list[dict[str, Any]], event_type: str = "tool.invoke") -> set[str]:
    return {
        str(event.get("payload", {}).get("tool"))
        for event in events
        if event.get("type") == event_type and event.get("payload", {}).get("tool")
    }


def _events_before_turn(events: list[dict[str, Any]], turn_index: int) -> list[dict[str, Any]] | None:
    for index, event in enumerate(events):
        if event.get("type") == "eval.turn.complete" and (event.get("payload") or {}).get("turn_index") == turn_index:
            return events[:index]
    return None


def _product_hits(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool.result":
            continue
        payload = event.get("payload") or {}
        if payload.get("tool") == "product_search_tool":
            hits.extend(payload.get("hits") or [])
    return hits


def _has_successful_product_result(events: list[dict[str, Any]]) -> bool:
    return any(
        event.get("type") == "tool.result"
        and (event.get("payload") or {}).get("tool") == "product_search_tool"
        and isinstance((event.get("payload") or {}).get("hits"), list)
        for event in events
    )


def _order_statuses(events: list[dict[str, Any]]) -> list[str]:
    statuses = [
        str((event.get("payload") or {}).get("order", {}).get("status"))
        for event in events
        if event.get("type") == "tool.result"
        and (event.get("payload") or {}).get("tool") in {"create_order_tool", "cancel_order_tool", "query_order_tool"}
        and (event.get("payload") or {}).get("order", {}).get("status")
    ]
    statuses.extend(str((event.get("payload") or {}).get("order", {}).get("status"))
                    for event in events if event.get("type") == "eval.order.snapshot"
                    and (event.get("payload") or {}).get("order", {}).get("status"))
    return statuses


def _written_orders(event: dict) -> list[dict]:
    payload = event.get("payload") or {}
    orders = []
    if isinstance(payload.get("order"), dict):
        orders.append(payload["order"])
    confirmation = payload.get("confirmation")
    if isinstance(confirmation, dict) and isinstance(confirmation.get("result"), dict):
        orders.append(confirmation["result"])
    if event.get("type") == "eval.confirmations.snapshot":
        orders.extend(row["result"] for row in payload.get("confirmations", []) if isinstance(row, dict) and isinstance(row.get("result"), dict))
    return [order for order in orders if order.get("status") in {"CONFIRMED", "CANCELLED"}]


def _landed_prices(events: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    prices: list[tuple[str, dict[str, Any]]] = []
    for hit in _product_hits(events):
        landed = hit.get("landed_price")
        if isinstance(landed, dict) and "landed_total_major" in landed:
            prices.append((str(hit.get("product_id", "未知商品")), landed))
    return prices


def _passed(criterion: str, passed: bool, reason: str) -> dict[str, Any]:
    return {"criterion": criterion, "pass": passed, "reason": f"程序证据：{reason}。结论：{'通过' if passed else '不通过'}"}


def evaluate_trace_assertions(assertions: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """运行声明式 trace 断言；用例定义不合法时直接失败，避免假绿。"""
    results: list[dict[str, Any]] = []
    for assertion in assertions:
        criterion = assertion.get("criterion")
        kind = assertion.get("kind")
        if not isinstance(criterion, str) or not criterion or not isinstance(kind, str):
            raise AssertionDefinitionError("确定性断言必须提供非空 criterion 与 kind")

        if kind == "no_orders_before_http_confirmation":
            before = []
            for event in events:
                if event.get("type") == "eval.http_action.invoke" and (event.get("payload") or {}).get("request", {}).get("approved") is True:
                    break
                before.append(event)
            observed = [order for event in before for order in _written_orders(event)]
            snapshots = [event for event in before if event.get("type") == "eval.confirmations.snapshot"]
            passed = bool(snapshots) and not observed
            results.append(_passed(criterion, passed, f"点击前持久确认快照 {len(snapshots)} 次；已执行订单 {len(observed)} 笔"))
            continue

        if kind == "http_confirmation_result":
            action = assertion.get("action", "create")
            expected = assertion.get("expected_status", "CONFIRMED")
            if action not in {"create", "cancel"} or expected not in {"CONFIRMED", "CANCELLED"}:
                raise AssertionDefinitionError("HTTP 确认断言的 action/expected_status 无效")
            confirmed_ids = set()
            for event in events:
                payload = event.get("payload") or {}
                response = payload.get("response") or {}
                confirmation = response.get("confirmation") or {}
                order = response.get("order") or {}
                if (event.get("type") == "eval.http_action.result" and payload.get("http_status") == 200
                    and payload.get("action") == action and payload.get("request", {}).get("approved") is True
                    and confirmation.get("status") == "approved" and order.get("status") == expected
                    and confirmation.get("confirmation_id") == payload.get("confirmation_id")
                    and confirmation.get("snapshot_hash") == payload.get("request", {}).get("snapshot_hash")):
                    confirmed_ids.add((confirmation["confirmation_id"], order.get("order_id")))
            persisted = {(event["payload"].get("confirmation_id"), event["payload"].get("order", {}).get("order_id"))
                         for event in events if event.get("type") == "eval.order.snapshot"
                         and event.get("payload", {}).get("order", {}).get("status") == expected}
            verified = confirmed_ids & persisted
            results.append(_passed(criterion, bool(verified), f"HTTP 用户确认并重新读取到 {expected} 意向单 {len(verified)} 笔"))
            continue

        if kind in {"required_tools", "forbidden_tools"}:
            tools = assertion.get("tools")
            if not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools):
                raise AssertionDefinitionError(f"{kind} 必须提供非空 tools 列表")
            actual = _tool_names(events)
            if kind == "required_tools":
                missing = sorted(set(tools) - actual)
                results.append(_passed(criterion, not missing, f"已调用工具 {sorted(actual)}；缺失 {missing}" if missing else f"已调用 {sorted(tools)}"))
            else:
                invoked = sorted(set(tools) & actual)
                results.append(_passed(criterion, not invoked, f"禁止工具被调用 {invoked}" if invoked else f"未调用禁止工具 {sorted(tools)}"))
            continue

        if kind == "forbidden_tools_before_turn":
            tools = assertion.get("tools")
            before_turn = assertion.get("before_turn")
            if not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools):
                raise AssertionDefinitionError("forbidden_tools_before_turn 必须提供非空 tools 列表")
            if not isinstance(before_turn, int) or before_turn <= 0:
                raise AssertionDefinitionError("forbidden_tools_before_turn 必须提供正整数 before_turn")
            earlier_events = _events_before_turn(events, before_turn)
            if earlier_events is None:
                results.append(_passed(criterion, False, f"缺少第 {before_turn} 轮结束标记，无法证明确认前行为"))
                continue
            invoked = sorted(set(tools) & _tool_names(earlier_events))
            results.append(_passed(
                criterion,
                not invoked,
                f"确认前禁止工具被调用 {invoked}" if invoked else f"确认前未调用禁止工具 {sorted(tools)}",
            ))
            continue

        if kind == "product_hits":
            hits = _product_hits(events)
            if not _has_successful_product_result(events):
                results.append(_passed(criterion, False, "没有 product_search_tool 的候选结果，无法证明硬约束"))
                continue
            violations: list[str] = []
            price_max = assertion.get("price_max_major")
            require_in_stock = assertion.get("require_in_stock", False)
            forbidden_materials = set(assertion.get("excluded_material_tags") or [])
            for hit in hits:
                product_id = str(hit.get("product_id", "未知商品"))
                if price_max is not None and float(hit.get("price_major", float("inf"))) > float(price_max):
                    violations.append(f"{product_id}: 超预算")
                if require_in_stock:
                    # ProductCard 的库存事实在 SKU 层；保留顶层 stock 兼容历史 trace。
                    skus = hit.get("skus") or []
                    has_stock = (
                        any(int(sku.get("stock", 0)) > 0 for sku in skus if isinstance(sku, dict))
                        if skus else int(hit.get("stock", 0)) > 0
                    )
                    if not has_stock:
                        violations.append(f"{product_id}: 无库存")
                matched_materials = forbidden_materials & set(hit.get("material_tags") or [])
                if matched_materials:
                    violations.append(f"{product_id}: 含禁用材质 {sorted(matched_materials)}")
            results.append(_passed(criterion, not violations, f"候选 {len(hits)} 个；" + ("；".join(violations) if violations else "无硬约束泄漏")))
            continue

        if kind == "tool_error":
            tool = assertion.get("tool")
            contains = assertion.get("contains")
            if not isinstance(tool, str) or not tool or not isinstance(contains, str) or not contains:
                raise AssertionDefinitionError("tool_error 必须提供非空 tool 与 contains")
            errors = [
                str((event.get("payload") or {}).get("error"))
                for event in events
                if event.get("type") == "tool.result"
                and (event.get("payload") or {}).get("tool") == tool
                and (event.get("payload") or {}).get("error")
            ]
            passed = any(contains in error for error in errors)
            results.append(_passed(
                criterion,
                passed,
                f"{tool} 错误 {errors}" if errors else f"未观察到 {tool} 的错误结果",
            ))
            continue

        if kind == "order_statuses":
            expected = assertion.get("expected")
            if not isinstance(expected, list) or not all(isinstance(status, str) and status for status in expected):
                raise AssertionDefinitionError("order_statuses 必须提供 expected 状态列表")
            actual = _order_statuses(events)
            missing = [status for status in expected if status not in actual]
            results.append(_passed(criterion, not missing, f"订单状态 {actual}；缺失 {missing}" if missing else f"订单状态按要求出现 {expected}"))
            continue

        if kind == "landed_price_consistency":
            prices = _landed_prices(events)
            if not prices:
                results.append(_passed(criterion, False, "没有包含到手价的商品检索结果"))
                continue
            violations: list[str] = []
            for product_id, landed in prices:
                try:
                    expected_total = sum(
                        float(landed[field])
                        for field in ("subtotal_major", "freight_major", "tariff_major")
                    )
                    actual_total = float(landed["landed_total_major"])
                except (KeyError, TypeError, ValueError):
                    violations.append(f"{product_id}: 到手价字段不完整")
                    continue
                if abs(expected_total - actual_total) > 0.01:
                    violations.append(
                        f"{product_id}: {actual_total} != {expected_total:.2f}",
                    )
            results.append(_passed(
                criterion,
                not violations,
                f"核对 {len(prices)} 个到手价；" + ("；".join(violations) if violations else "小计+运费+关税均等于总价"),
            ))
            continue

        raise AssertionDefinitionError(f"未知确定性断言类型：{kind}")
    return results
