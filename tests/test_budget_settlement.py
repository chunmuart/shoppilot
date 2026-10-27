"""异常/缺失用量与延迟流结算，验证未知成本不免费、计费异常不泄漏闸门。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.budget import init_budget
from app.infrastructure.llm import BudgetCall, _usage_tokens
from app.infrastructure.model_stream import StreamFinalizer
from app.infrastructure.operational_metrics import begin_request, finish_request


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 10}, None), ({"output_tokens": 10}, None),
    ({"total_tokens": -1}, None), ({"total_tokens": True}, None),
    ({"total_tokens": "10"}, None), ({"total_tokens": float("inf")}, None),
    ({"total_tokens": float("nan")}, None),
    ({"input_tokens": True, "output_tokens": 5}, None),
    ({"input_tokens": 0, "output_tokens": 0}, 0),
    ({"input_tokens": 10, "output_tokens": 5}, 15),
    ({"prompt_tokens": 10, "completion_tokens": 5}, 15),
    ({"total_tokens": 20}, 20),
    ({"total_tokens": 0, "input_tokens": 10}, None),
    ({"total_tokens": 10, "input_tokens": 10, "output_tokens": 5}, 15),
])
def test_usage_only_accepts_complete_nonnegative_integer_evidence(usage, expected):
    assert _usage_tokens({"usage": usage}) == expected


async def test_partial_usage_is_charged_conservatively_and_metrics_remain_unknown():
    budget = init_budget(10000)
    observation = begin_request()
    try:
        call = BudgetCall([], None, {})
        assert call.acquire()
        reserved = budget.reserved
        call.mark_started()
        call.settle({"usage": {"input_tokens": 10}})
        assert budget.used == reserved and budget.reserved == 0
        summary = finish_request(observation)
        assert summary["usage_complete"] is False
        assert summary["input_tokens"] is None and summary["model_calls"] == 1
    finally:
        if not observation.finished:
            finish_request(observation, "error")
        init_budget(0)


async def test_unsettled_attempt_is_not_reported_as_zero_cost_before_late_cleanup():
    init_budget(0)
    observation = begin_request()
    call = BudgetCall([], None, {})
    call.mark_started()
    summary = finish_request(observation, "cancelled")
    assert summary["usage_complete"] is False and summary["model_calls"] == 1
    assert summary["input_tokens"] is None
    call.settle({"usage": {"input_tokens": 10, "output_tokens": 2}})
    assert summary["usage_complete"] is False and summary["input_tokens"] is None


@pytest.mark.parametrize("original_error", [False, True])
async def test_finalizer_releases_slot_even_if_usage_settlement_raises(original_error):
    slot = SimpleNamespace(__aexit__=AsyncMock())
    def failing_charge(_):
        raise OverflowError("测试结算异常")
    finalizer = StreamFinalizer(slot, failing_charge)
    if original_error:
        await finalizer.close((asyncio.CancelledError, asyncio.CancelledError(), None))
    else:
        with pytest.raises(OverflowError):
            await finalizer.close()
    slot.__aexit__.assert_awaited_once()
    await finalizer.close()
    slot.__aexit__.assert_awaited_once()


async def test_minimal_hint_uses_pre_reservation_tier_and_is_included_in_budget():
    from agentscope.credential import OpenAICredential
    from app.infrastructure.llm import ThrottledChatModel
    from app.infrastructure.throttle import GatewayThrottle
    from app.infrastructure.budget import MINIMAL_MODE_HINT
    class Fallback:
        model = "lite-test"
        messages = None
        async def __call__(self, messages, *_args, **_kwargs):
            self.messages = messages
            return {"usage": {"input_tokens": 50, "output_tokens": 10}}
    budget = init_budget(20000)
    budget.charge("prior", 17000)
    fallback = Fallback()
    model = ThrottledChatModel(credential=OpenAICredential(api_key="local-test", base_url="http://127.0.0.1:9/v1"),
        model="main-test", fallback=fallback, throttle=GatewayThrottle(max_concurrency=1, min_interval_seconds=0))
    try:
        call = BudgetCall([], None, {})
        assert call.acquire() and call.tier == "minimal"
        assert call.input_tokens > call.base_input_tokens
        call.reservation.settle(0)
        await model([])
        assert fallback.messages is not None
        assert fallback.messages[-1].get_text_content() == MINIMAL_MODE_HINT
        assert budget.reserved == 0
    finally:
        init_budget(0)
