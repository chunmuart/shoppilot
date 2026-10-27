"""按调用读取商品小卡：真实 SQLite、身份检查与展示白名单，不调用模型。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI

from app.domain.session.ports.session_store import SessionOwnerMismatch
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalForbidden, JournalNotFound
from app.infrastructure.context_products import product_page
from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.tool_product_view import tool_product_view
from app.presentation.ag_ui import register_ag_ui_routes
from tests.test_ag_ui_journal import body, controlled_journal_time


def product(identifier="P1001", **changes):
    return {"product_id": identifier, "title": "测试商品", "price_major": 129.5, "currency": "CNY",
            "default_sku_id": identifier + "-S2", "skus": [
                {"sku_id": identifier + "-S1", "spec": "小号", "price_major": 99, "currency": "CNY"},
                {"sku_id": identifier + "-S2", "spec": "大号", "price_major": 129.5, "currency": "CNY"},
            ], **changes}


def start(identifier, name="product_search_tool"):
    return {"type": "TOOL_CALL_START", "toolCallId": identifier, "toolCallName": name}


def result(identifier, payload):
    return {"type": "TOOL_CALL_RESULT", "toolCallId": identifier, "messageId": identifier + ":result",
            "content": json.dumps(payload, ensure_ascii=False), "role": "tool"}


async def saved_run(tmp_path, events):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "owner")
    await journal.append("r1", "owner", events)
    return journal


async def test_parallel_tool_products_are_owned_by_call_and_do_not_read_latest_state(tmp_path):
    journal = await saved_run(tmp_path, [
        start("r1:tool:a"), start("r1:tool:b"),
        result("r1:tool:b", {"hits": [product("P1002")]}),
        result("r1:tool:a", {"hits": [product("P1001")], "total_candidates": 999}),
        {"type": "STATE_SNAPSHOT", "snapshot": {"products": [product("P9999")]}},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "s1"},
    ])
    # 没有新活动事件的旧历史也能读取；重启后不需要商品状态和工具重执行。
    restored = AGUIJournal(journal.path)
    a = await restored.tool_products("r1", "r1:tool:a", "b1")
    b = await restored.tool_products("r1", "r1:tool:b", "b1")
    assert a["runId"] == b["runId"] == "r1"
    assert a["toolCallId"] == "r1:tool:a" and b["toolCallId"] == "r1:tool:b"
    assert a["products"][0]["product_id"] == "P1001"
    assert b["products"][0]["product_id"] == "P1002"
    assert a["products"][0]["spec"] == "大号"
    assert a["totalCount"] == a["count"] == 1 and a["truncated"] is False


async def test_products_require_matching_run_buyer_and_product_tool_start(tmp_path):
    journal = await saved_run(tmp_path, [
        start("a"), result("a", {"hits": [product()]}),
        start("private", "remember_preference_tool"), result("private", {"hits": [product()]}),
        result("orphan", {"hits": [product()]}),
    ])
    with pytest.raises(JournalForbidden):
        await journal.tool_products("r1", "a", "b2")
    for identifier in ("private", "missing", "orphan"):
        with pytest.raises(JournalNotFound):
            await journal.tool_products("r1", identifier, "b1")
    with pytest.raises(JournalNotFound):
        await journal.tool_products("missing", "a", "b1")
    await journal.reserve(body("r2", "s2"), "b1", "owner2")
    await journal.append("r2", "owner2", [start("a"), result("a", {"hits": [product("P2001")]})])
    assert (await journal.tool_products("r1", "a", "b1"))["products"][0]["product_id"] == "P1001"
    assert (await journal.tool_products("r2", "a", "b1"))["products"][0]["product_id"] == "P2001"


async def test_missing_result_pending_and_terminal_are_distinct(tmp_path):
    journal = await saved_run(tmp_path, [start("a")])
    pending = await journal.tool_products("r1", "a", "b1")
    assert pending["status"] == "pending" and pending["totalCount"] is None and not pending["products"]
    await journal.append("r1", "owner", [{"type": "RUN_ERROR", "message": "测试中断"}])
    assert (await journal.tool_products("r1", "a", "b1"))["status"] == "unavailable"


async def test_reading_expired_run_does_not_recover_or_mutate_events(tmp_path, monkeypatch):
    clock = controlled_journal_time(monkeypatch)
    journal = await saved_run(tmp_path, [start("a")])
    clock.now += 300
    assert await journal.run_session_id("r1", "b1") == "s1"
    assert (await journal.tool_products("r1", "a", "b1"))["status"] == "unavailable"
    async with journal._db() as db:
        row = await journal._one(db, "SELECT status,last_seq FROM agui_runs WHERE run_id='r1'")
        assert row["status"] == "running" and row["last_seq"] == 1


async def test_ambiguous_reused_call_id_cannot_expose_another_tool_result(tmp_path):
    journal = await saved_run(tmp_path, [start("same"), start("same", "remember_preference_tool"),
        result("same", {"hits": [product()]})])
    with pytest.raises(JournalNotFound):
        await journal.tool_products("r1", "same", "b1")


def test_product_projection_removes_private_fields_and_does_not_make_up_images():
    secret = "fixture-private-content"
    projection = tool_product_view(json.dumps({"hits": [product(
        description=secret, shipping_address={"phone": secret}, api_key=secret,
        highlights=[secret], trace=secret, landed_price={"private": secret},
    )], "query_conditions": secret, "fragment_product_id": secret, "trace": secret}))
    assert secret not in json.dumps(projection)
    assert set(projection["products"][0]) == {"product_id", "title", "price_major", "currency", "default_sku_id", "spec"}
    assert projection["status"] == "ready" and projection["truncated"] is False


@pytest.mark.parametrize("amount", [True, False, -1, float("nan"), float("inf"), float("-inf"), 10 ** 400, "129", None, []])
def test_invalid_amount_never_becomes_a_quote(amount):
    view = tool_product_view(json.dumps({"hits": [product(price_major=amount)]}))
    assert view["products"] == [] and view["count"] == 0 and view["truncated"] is True
    json.dumps(view, allow_nan=False)


@pytest.mark.parametrize("change", [{"currency": "usd"}, {"currency": []}, {"title": ""}, {"title": {}}, {"product_id": "../other"}, {"product_id": []}])
def test_invalid_required_product_fields_are_omitted(change):
    assert not tool_product_view(json.dumps({"hits": [product(**change)]}))["products"]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:image/svg+xml,<svg/>", "//outside.example/item.png", "/products/../private",
    "/products/%2e%2e/private", "https://user:pass@example.test/item.png", "https://example.test:bad/img",
    "https://example.test/%0aevil", "https://example.test\\@other.test/img", [], None,
])
def test_unsafe_images_are_not_returned(url):
    view = tool_product_view(json.dumps({"hits": [product(image_url=url)]}))
    assert "image_url" not in view["products"][0]


@pytest.mark.parametrize("url", ["/products/backpack.webp", "https://images.example.test/backpack.png", "http://images.example.test/backpack.png"])
def test_safe_source_images_are_optional_and_preserved(url):
    view = tool_product_view(json.dumps({"hits": [product(image_url=url, image_kind="illustration")]}))
    assert view["products"][0]["image_url"] == url
    assert view["products"][0]["image_kind"] == "illustration"


@pytest.mark.parametrize("payload", [None, [], "text", {"hits": {}}, {"hits": None}, {"error": "private"},
    {"hits": [product()], "ok": False}, {"hits": [product()], "status": "failed"}])
def test_invalid_or_failed_tool_payload_does_not_leak_raw_body(payload):
    view = tool_product_view(json.dumps(payload))
    assert view["status"] == "unavailable" and not view["products"] and view["totalCount"] is None
    assert "private" not in json.dumps(view)


def test_malformed_json_and_unknown_field_shapes_do_not_raise():
    assert tool_product_view("[error] private")["status"] == "unavailable"
    assert tool_product_view("{" * 2000)["status"] == "unavailable"
    view = tool_product_view(json.dumps({"hits": [product(image_url="/products/a.png", image_kind=[])], "status": {}}))
    assert view["count"] == 1 and "image_kind" not in view["products"][0]


def test_invalid_unicode_cannot_break_json_response_encoding():
    view = tool_product_view(json.dumps({"hits": [product(title="\ud800"), product(
        image_url="https://images.example.test/\ud800", spec="\ud800", default_sku_id=None)]}))
    assert view["count"] == 1 and view["truncated"] is True
    assert "spec" not in view["products"][0] and "image_url" not in view["products"][0]
    json.dumps(view, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_fifty_item_limit_uses_saved_result_count_not_candidate_pool():
    view = tool_product_view(json.dumps({"hits": [product(f"P{i:04}") for i in range(60)], "total_candidates": 999}))
    assert view["count"] == len(view["products"]) == 50
    assert view["totalCount"] == 60 and view["truncated"] is True


def test_actual_context_pagination_does_not_claim_a_full_result():
    payload = product_page({"hits": [product(f"P{i:04}") for i in range(8)], "result_ref": "ctx-test"}, token_limit=12000)
    view = tool_product_view(json.dumps(payload))
    assert view["count"] == 5 and view["totalCount"] == 8 and view["truncated"] is True
    fragment = product_page({"hits": [product(description="很长的商品说明" * 2000)]}, token_limit=100)
    view = tool_product_view(json.dumps(fragment))
    assert view["products"] == [] and view["totalCount"] == 1 and view["truncated"] is True
    assert "fragment" not in view and "很长的商品说明" not in json.dumps(view, ensure_ascii=False)


def test_selected_sku_page_keeps_spec_and_price_together():
    page = product_page({"hits": [product()]}, sku_id="P1001-S1", token_limit=12000)
    card = tool_product_view(json.dumps(page))["products"][0]
    assert (card["default_sku_id"], card["spec"], card["price_major"]) == ("P1001-S1", "小号", 99)


@pytest.mark.parametrize("metadata", [{"total": True}, {"total": -1}, {"total": "3"}, {"total": 0},
    {"total": 2, "offset": []}, {"total": 1, "offset": 1}, {"next_offset": 5}])
def test_invalid_pagination_metadata_is_not_presented_as_complete(metadata):
    view = tool_product_view(json.dumps({"hits": [product()], **metadata}))
    assert view["count"] == 1 and view["totalCount"] is None and view["truncated"] is True


def test_empty_search_is_distinct_from_unreadable_result():
    view = tool_product_view('{"hits": [], "total_candidates": 100}')
    assert view["status"] == "ready" and view["count"] == view["totalCount"] == 0
    assert view["truncated"] is False


async def test_http_products_route_checks_signature_run_and_session_ownership(tmp_path):
    identifier = "r1:tool:a"
    journal = await saved_run(tmp_path, [start(identifier), result(identifier, {"hits": [product()]})])
    api = FastAPI()
    store = SimpleNamespace(assert_owner=AsyncMock())
    policy = IdentityPolicy(mode="hmac", secret="fixture-product-view-signing-key-1234567890")
    api.state.identity_policy, api.state.session_store = policy, store
    register_ag_ui_routes(api, lambda: None, get_runtime=lambda: SimpleNamespace(journal=journal))
    path = f"/commerce/ag-ui/runs/r1/tools/{quote(identifier, safe='')}/products"
    auth = {"Authorization": "Bearer " + policy.issue("b1")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://local") as client:
        assert (await client.get(path, params={"buyer_id": "b1"})).status_code == 401
        assert (await client.get(path, params={"buyer_id": "b2"}, headers=auth)).status_code == 403
        other_auth = {"Authorization": "Bearer " + policy.issue("b2")}
        assert (await client.get(path, params={"buyer_id": "b2"}, headers=other_auth)).status_code == 403
        response = await client.get(path, params={"buyer_id": "b1"}, headers=auth)
        assert response.status_code == 200
        assert response.json()["runId"] == "r1" and response.json()["toolCallId"] == identifier
        assert response.json()["products"][0]["product_id"] == "P1001"
        store.assert_owner.assert_awaited_once_with("s1", "b1", create=False, enforce_owner=True)
        store.assert_owner.side_effect = SessionOwnerMismatch("s1")
        assert (await client.get(path, params={"buyer_id": "b1"}, headers=auth)).status_code == 403
