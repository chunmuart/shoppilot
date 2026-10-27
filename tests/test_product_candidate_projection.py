# -*- coding: utf-8 -*-
"""精确多 ID 并发查询使用真实商品 DTO，验证展示和持久证据的共同顺序。"""
import asyncio
import copy

import pytest
from ag_ui.core import RunAgentInput

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.agents.product_candidate_projection import ProductCandidateProjection
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEvent, TradeEventBus, observe_run_events
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository


@pytest.fixture
async def results():
    usecase = CatalogSearchUseCase(InMemoryProductRepository())
    output = {}
    for index, identifier in enumerate(("P1001-S2", "P1003-S1", "P1001-S1")):
        found = await usecase.execute(ProductSearchSpec(normalized_query=identifier, ship_to="CN"))
        assert found["hits"]
        output[identifier] = {"tool": "product_search_tool", **found, "result_ref": f"ctx_{index}"}
    return output


def request(query, *, run="run-1", state=None):
    return RunAgentInput.model_validate({"threadId": "s", "runId": run, "state": state or {},
        "messages": [{"id": "old", "role": "user", "content": "比较 P1049 和 P1018"},
                     {"id": "current", "role": "user", "content": query}],
        "tools": [], "context": [], "forwardedProps": {"buyerId": "b"}})


async def test_parallel_real_tool_completions_reverse_order_keep_both_exact_skus():
    completed_second = asyncio.Event()
    actual = CatalogSearchUseCase(InMemoryProductRepository())
    class DelayedUseCase:
        async def execute(self, spec):
            if "P1001" in spec.normalized_query:
                await completed_second.wait()
            found = await actual.execute(spec)
            if "P1003" in spec.normalized_query:
                completed_second.set()
            return found
    events = []
    adapter = AGUIRunAdapter(request("比较 P1001-S2 / P1003-S1，寄到中国。"), events.append)
    adapter.start()
    tool = build_product_search_tool(DelayedUseCase(), TradeEventBus())
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    try:
        with observe_run_events(adapter.on_trade_event):
            await asyncio.gather(tool(normalized_query="P1001-S2", ship_to="CN"),
                                 tool(normalized_query="P1003-S1", ship_to="CN"))
    finally:
        ShoppingContext.reset(token)
    snapshots = [event.snapshot for event in events if event.type == "STATE_SNAPSHOT"]
    assert [card["product_id"] for card in snapshots[-2]["products"]] == ["P1003"]
    assert [card["product_id"] for card in snapshots[-1]["products"]] == ["P1001", "P1003"]
    assert [card["default_sku_id"] for card in snapshots[-1]["products"]] == ["P1001-S2", "P1003-S1"]
    for card in snapshots[-1]["products"]:
        expected = await actual.execute(ProductSearchSpec(normalized_query=card["default_sku_id"], ship_to="CN"))
        assert card == expected["hits"][0]  # 价格、运费、图片、SKU 均来自完整原 DTO。


async def test_duplicate_refs_and_source_mutation_do_not_duplicate_or_corrupt_cards(results):
    projection = ProductCandidateProjection("比较 p1001-s2 和 P1003-S1，再看 P1001-S2")
    projection.apply(results["P1003-S1"])
    projection.apply(results["P1001-S2"])
    projection.apply(results["P1003-S1"])
    assert projection.has_result and len(projection.result["hits"]) == 2
    assert projection.result["result_refs"] == ["ctx_0", "ctx_1"]
    assert "result_ref" not in projection.result
    results["P1001-S2"]["hits"][0]["price_major"] = -99
    assert projection.result["hits"][0]["price_major"] > 0
    assert projection.result["requested_identifiers"] == ["P1001-S2", "P1003-S1"]


async def test_empty_and_ordinary_changed_search_replace_and_clear_merge_cache(results):
    projection = ProductCandidateProjection("比较 P1001-S2 和 P1003-S1")
    projection.apply(results["P1001-S2"])
    projection.apply(results["P1003-S1"])
    projection.apply({"tool": "product_search_tool", "hits": [], "recall_strategy": "exact_id_lookup"})
    assert projection.result["hits"] == []
    projection.apply(results["P1003-S1"])
    assert [card["product_id"] for card in projection.result["hits"]] == ["P1003"]
    ordinary = {**results["P1001-S1"], "recall_strategy": "keyword_2gram"}
    projection.apply(ordinary)
    assert projection.result["hits"] == ordinary["hits"]
    projection.apply(results["P1003-S1"])
    assert [card["product_id"] for card in projection.result["hits"]] == ["P1003"]


async def test_original_sku_filter_and_filtered_out_do_not_resurrect_old_candidate(results):
    projection = ProductCandidateProjection("P1001-S2 与 P1003-S1对比")
    projection.apply(results["P1001-S1"])
    assert projection.result["hits"] == []
    projection.apply(results["P1001-S2"])
    filtered = {**results["P1003-S1"], "filtered_out": [{"product_id": "P1001", "reason": "超预算"}]}
    projection.apply(filtered)
    assert [card["product_id"] for card in projection.result["hits"]] == ["P1003"]


async def test_new_run_last_user_only_and_ordinary_query_keep_replace_semantics(results):
    first = AGUIRunAdapter(request("P1001-S2 和 P1003-S1"), lambda _: None)
    for key in ("P1001-S2", "P1003-S1"):
        first.on_trade_event(TradeEvent("s", "tool.result", results[key], "now"))
    assert len(first.state["products"]) == 2
    emitted = []
    second = AGUIRunAdapter(request("换成轻便背包", run="run-2", state=copy.deepcopy(first.state)), emitted.append)
    second.start()
    assert second.state["products"] == []
    for key in ("P1001-S2", "P1003-S1"):
        second.on_trade_event(TradeEvent("s", "tool.result", results[key], "now"))
    assert [card["product_id"] for card in second.state["products"]] == ["P1003"]
    second.on_trade_event(TradeEvent("other-session", "tool.result", results["P1001-S2"], "now"))
    assert [card["product_id"] for card in second.state["products"]] == ["P1003"]


def test_errors_partial_payloads_and_other_tools_do_not_fake_search_completion():
    projection = ProductCandidateProjection("P1001 和 P1003")
    for payload in ({"tool": "product_search_tool", "error": "failed"},
                    {"tool": "product_search_tool", "hits": "partial"},
                    {"tool": "category_insight_tool", "hits": []}):
        assert projection.apply(payload) is False
    assert projection.has_result is False and projection.result is None
