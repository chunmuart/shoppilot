"""预算、持久证据和压缩的行为回归。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState, UserMsg
from agentscope.model import ChatResponse
from app.infrastructure.budget import init_budget, remember_verified_result, rule_fallback_text
from app.infrastructure.llm import ThrottledChatModel
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore, product_decision_view
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.context_compaction import EvidenceCompactionMiddleware
from app.application.tools.conversation_fact_lookup import build_conversation_fact_lookup


class BudgetSpy(ThrottledChatModel):
    def __init__(self):
        super().__init__(credential=OpenAICredential(api_key="test", base_url="http://test/v1"),
                         model="budget-spy", throttle=GatewayThrottle(3, 0), max_transient_retries=0)
        self.calls = 0
        self.wait = asyncio.Event()
        self.started = asyncio.Event()
    async def _invoke_upstream(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        await self.wait.wait()
        return ChatResponse(content=[TextBlock(text="已完成")], is_last=True, usage={"total_tokens": 80})


async def test_exhaustion_never_enters_upstream_or_throttle():
    budget = init_budget(1000)
    budget.charge("previous", 999)
    model = BudgetSpy()
    try:
        result = await asyncio.wait_for(model([]), .5)
        assert result.metadata["budget_fallback"] is True
        assert model.calls == 0
        assert budget.reserved == 0
    finally:
        init_budget(0)
        await model.client.close()


async def test_parallel_reservations_do_not_spend_same_remainder():
    budget = init_budget(1100)
    model = BudgetSpy()
    try:
        first = asyncio.create_task(model([]))
        await model.started.wait()
        assert budget.reserved == 1100
        second = await model([])
        assert second.metadata["budget_fallback"]
        assert model.calls == 1
        model.wait.set()
        await first
        assert budget.used == 80 and budget.reserved == 0
        assert not (await model([])).metadata.get("budget_fallback")
        assert model.calls == 2
    finally:
        model.wait.set()
        init_budget(0)
        await model.client.close()


def test_rule_fallback_uses_verified_result_only_and_resets_each_turn():
    init_budget(0)
    remember_verified_result("products", {"hits": [{"title": "真实目录商品", "product_id": "P1", "price_major": 19, "currency": "CNY"}]})
    assert "P1" in rule_fallback_text()
    init_budget(0)
    assert "P1" not in rule_fallback_text()


async def test_evidence_survives_reopen_and_rejects_cross_scope(tmp_path):
    store = ContextEvidenceStore(tmp_path / "evidence.db")
    ref = await store.save("buyer", "session", "products", {"hits": [{"product_id": "P1"}]})
    reopened = ContextEvidenceStore(store.path)
    assert (await reopened.get("buyer", "session", ref))["data"]["hits"][0]["product_id"] == "P1"
    assert await reopened.get("other", "session", ref) is None
    assert await reopened.get("buyer", "other", ref) is None


async def test_lookup_second_item_from_latest_candidates_after_restart(tmp_path):
    store = ContextEvidenceStore(tmp_path / "evidence.db")
    await store.save("b", "s", "products", {"hits": [{"product_id": "OLD"}]})
    await store.save("b", "s", "products", {"hits": [{"product_id": "P1"}, {"product_id": "P2"}]})
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    try:
        result = await build_conversation_fact_lookup(ContextEvidenceStore(store.path))(position=2)
        assert json.loads(result.content[0].text)["records"][0]["data"]["hits"] == [{"product_id": "P2"}]
        await store.save("b", "s", "products", {"hits": []})
        result = await build_conversation_fact_lookup(store)(position=2)
        assert json.loads(result.content[0].text)["records"][0]["data"]["hits"] == []
    finally:
        ShoppingContext.reset(token)


def test_decision_projection_keeps_exact_prices_skus_but_removes_media():
    full = {"hits": [{"product_id": "P1", "price_major": 19.98, "currency": "CNY", "skus": [{"sku_id": "S1", "stock": 3}], "description": "x"*1000, "image_url": "https://image"}], "result_ref": "ctx_1"}
    view = product_decision_view(full)
    assert view["hits"][0]["skus"] == full["hits"][0]["skus"]
    assert view["hits"][0]["price_major"] == 19.98
    assert "description" not in view["hits"][0] and "image_url" not in view["hits"][0]
    assert "description" in full["hits"][0]


async def test_old_tool_archive_runs_before_summary_with_tool_call_pair_intact(tmp_path):
    init_budget(0)
    block = ToolResultBlock(id="call-1", name="large_tool", output="证据"*1000, state=ToolResultState.SUCCESS)
    context = [Msg(name="tool", content=[block], role="assistant"), *[UserMsg("buyer", "近轮") for _ in range(6)]]
    model = SimpleNamespace(context_size=1000, count_tokens=AsyncMock(side_effect=[900, 100]))
    agent = SimpleNamespace(state=SimpleNamespace(context=context, summary=""), model=model,
                            context_config=SimpleNamespace(trigger_ratio=.75), _prepare_model_input=AsyncMock(return_value={}))
    store = ContextEvidenceStore(tmp_path / "evidence.db")
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    async def next_handler():
        assert context[0].content[0].id == "call-1"
        assert "result_ref" in context[0].content[0].output[0].text
    try:
        await EvidenceCompactionMiddleware(store).on_compress_context(agent, {}, next_handler)
        report = (await store.search("b", "s", kind="compression"))[0]["data"]
        assert report["before_tokens"] == 900 and report["after_tool_cleanup_tokens"] == 100
        assert report["summary_changed"] is False
    finally:
        ShoppingContext.reset(token)


async def test_delayed_old_fence_cannot_replace_latest_candidate(tmp_path):
    store = ContextEvidenceStore(tmp_path / "evidence.db")
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY", session_fence=2))
    try:
        await store.save("b", "s", "products", {"hits": [{"product_id": "NEW"}]})
        ShoppingContext.set_session_fence(1)
        await store.save("b", "s", "products", {"hits": [{"product_id": "OLD"}]})
        latest = (await store.search("b", "s", kind="products", limit=1))[0]
        assert latest["data"]["hits"][0]["product_id"] == "NEW"
    finally:
        ShoppingContext.reset(token)


async def test_retry_cannot_bypass_exhausted_budget(monkeypatch):
    budget = init_budget(1100)
    model = BudgetSpy()
    model._max_transient_retries = 2
    model._retry_base_seconds = 0
    upstream = AsyncMock(side_effect=RuntimeError("429 rate limit exceeded"))
    monkeypatch.setattr(model, "_invoke_upstream", upstream)
    try:
        response = await model([])
        assert response.metadata["budget_fallback"]
        assert upstream.await_count == 1
        assert budget.used == 1100 and budget.reserved == 0
    finally:
        init_budget(0)
        await model.client.close()


async def test_preference_withdrawal_is_authoritative_after_restart(tmp_path):
    from tests.test_ag_ui import make_orchestrator
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore
    from app.domain.buyer.preference import BuyerPreference
    preferences = JsonFilePreferenceStore(tmp_path / "preferences")
    evidence = ContextEvidenceStore(tmp_path / "evidence.db")
    intent = SubmitIntentInput("session-test", "buyer-test", "zh-CN", "CNY", "推荐背包")
    orchestrator, _, _ = make_orchestrator()
    orchestrator._preference_store, orchestrator._evidence_store = preferences, evidence
    await preferences.append(BuyerPreference("buyer-test", "dislike", "不要塑料材质"))
    first = await orchestrator._build_inputs(intent, "session-test")
    assert "不要塑料材质" in first[0].get_text_content()
    await preferences.delete("buyer-test", "不要塑料材质")
    restarted, _, _ = make_orchestrator()
    restarted._preference_store, restarted._evidence_store = JsonFilePreferenceStore(tmp_path / "preferences"), ContextEvidenceStore(evidence.path)
    second = await restarted._build_inputs(intent, "session-test")
    assert "历史已撤回偏好不得恢复" in second[0].get_text_content()
    assert "不要塑料材质" not in second[0].get_text_content()
    records = await evidence.search("buyer-test", "session-test", kind="preferences")
    assert records[0]["data"]["revision"] != records[1]["data"]["revision"]


async def test_preference_read_failure_never_uses_or_populates_empty_scope_cache():
    from app.application.agents.orchestrator import MainAgentOrchestrator
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    orchestrator = object.__new__(MainAgentOrchestrator)
    orchestrator._preference_store = SimpleNamespace(list_by_buyer=AsyncMock(side_effect=RuntimeError('temporary database error')))
    orchestrator._semantic_cache = SimpleNamespace(lookup=AsyncMock(), remember=AsyncMock())
    intent = SimpleNamespace(buyer_id='buyer',raw_query='给我推荐耳机')
    assert await orchestrator._lookup_cache(intent, False) is None
    await orchestrator._remember_cache(intent, 'cached text', False)
    orchestrator._semantic_cache.lookup.assert_not_awaited()
    orchestrator._semantic_cache.remember.assert_not_awaited()


async def test_parallel_exact_candidate_order_survives_persistence_and_restart(tmp_path):
    from tests.test_ag_ui import make_orchestrator
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    orchestrator, agent, _ = make_orchestrator()
    store = ContextEvidenceStore(tmp_path/'candidate.db')
    orchestrator._evidence_store = store
    async def reply(session, current_agent, inputs):
        # 后声明的商品先完成，顺序仍以用户要求和前端投影为准。
        for identifier in ['P1003-S1','P1001-S2']:
            result = await agent.usecase.execute(ProductSearchSpec(normalized_query=identifier))
            orchestrator._bus.publish(session, 'tool.result', {'tool':'product_search_tool', **result})
        return '已完成对比'
    orchestrator._reply_with_retry = reply
    await orchestrator.handle_intent(SubmitIntentInput('session-test','buyer-test','zh-CN','CNY','对比P1001-S2和P1003-S1'))
    restarted = ContextEvidenceStore(store.path)
    latest = (await restarted.search('buyer-test','session-test',kind='products',limit=1))[0]
    assert [h['product_id'] for h in latest['data']['hits']] == ['P1001','P1003']
    token = ShoppingContext.set(ShoppingContextSnapshot('session-test','buyer-test','zh-CN','CNY'))
    try:
        chunk = await build_conversation_fact_lookup(restarted)(position=2)
        payload = json.loads(chunk.content[0].text)
        assert 'P1003-S1' in json.dumps(payload)
    finally:
        ShoppingContext.reset(token)
