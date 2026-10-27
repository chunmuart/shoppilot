"""定时合同、持久领取、跨重启和两种输出：隔离 SQLite，无真实订单。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import httpx
import pytest
from ag_ui.core import RunAgentInput
from fastapi import FastAPI
from app.domain.automation.models import Schedule
from app.infrastructure.automation_store import (
    AutomationStore,
    AutomationConflict,
    AutomationNotFound,
)
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalConflict
from app.application.agent_runtime import AGUIRuntime
from app.application.automation import AutomationService, AutomationWorker
from app.presentation.automations import register_automation_routes


def stamp(value, zone="Asia/Shanghai"):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp()


def definition(mode="merged", **changes):
    return {
        "name": "每周选购",
        "prompt": "比较通勤背包，预算400元",
        "schedule": {"kind": "daily", "time": "10:00"},
        "output_mode": mode,
        "auto_start": True,
        "model": {
            "profile_id": "test",
            "profile_revision": 1,
            "window_id": "200k",
            "thinking": "off",
        },
        **changes,
    }


@pytest.fixture
async def stores(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.initialize()
    clock = [stamp("2026-10-09T09:00:00")]
    store = AutomationStore(journal.path, clock=lambda: clock[0])
    await store.initialize()
    return store, journal, clock


@pytest.mark.parametrize(
    "zone", ["Asia/Shanghai", "America/New_York", "Europe/Berlin", "Pacific/Auckland"]
)
def test_daily_preview_wall_time(zone):
    schedule = Schedule(kind="daily", time="10:30", timezone=zone)
    times = schedule.preview(stamp("2026-10-09T11:00:00", zone))
    assert len(times) == 5
    assert all("T10:30:00" in t for t in times)


def test_dst_gap_skips_and_fold_uses_earlier():
    spring = Schedule(kind="daily", time="02:30", timezone="America/New_York")
    times = spring.preview(stamp("2026-03-07T03:00:00", "America/New_York"), 2)
    assert times[0].startswith("2026-03-09T02:30")
    fall = Schedule(kind="daily", time="01:30", timezone="America/New_York")
    times = fall.preview(stamp("2026-10-31T02:00:00", "America/New_York"), 2)
    assert times == ["2026-11-01T01:30:00-04:00", "2026-11-02T01:30:00-05:00"]


@pytest.mark.parametrize(
    "rule", ["* * * * * *", "0 0 31 2 *", "@reboot", "H * * * *", "*/0 * * * *"]
)
def test_cron_rejects_invalid(rule):
    with pytest.raises(ValueError):
        Schedule(kind="cron", cron=rule)


def test_cron_or_and_expiration():
    rule = Schedule(kind="cron", cron="0 10 1 * 1", end_date="2026-10-12")
    assert rule.preview(stamp("2026-10-09T00:00:00")) == ["2026-10-12T10:00:00+08:00"]
    assert Schedule(kind="once", at="2026-10-10T09:00:00").preview(
        stamp("2026-10-09T00:00:00")
    ) == ["2026-10-10T09:00:00+08:00"]


async def test_save_cas_ownership_and_idempotency(stores):
    store, _, _ = stores
    a = await store.save("buyer", definition(), "request")
    assert (await store.save("buyer", definition(), "request"))["id"] == a["id"]
    with pytest.raises(AutomationConflict):
        await store.save("buyer", definition(prompt="changed"), "request")
    with pytest.raises(AutomationNotFound):
        await store.get(a["id"], "someone")
    with pytest.raises(AutomationConflict):
        await store.save(
            "buyer", definition(), "edit", identity=a["id"], expected_revision=0
        )
    b = await store.save(
        "buyer",
        definition("independent"),
        "edit",
        identity=a["id"],
        expected_revision=1,
    )
    assert b["revision"] == 2 and b["session_id"] != a["session_id"]


async def test_concurrent_tick_and_claim_once(stores):
    store, journal, clock = stores
    await store.save("b", definition(), "a")
    clock[0] = stamp("2026-10-09T10:00:01")
    other = AutomationStore(journal.path, clock=lambda: clock[0])
    await asyncio.gather(store.tick("w1"), other.tick("w2"))
    runs = await store.runs("b")
    assert runs["total"] == 1
    claims = await asyncio.gather(store.claim("w1"), other.claim("w2"))
    assert sum(x is not None for x in claims) == 1


@pytest.mark.parametrize("mode", ["independent", "merged"])
async def test_three_occurrences_restore_identity_and_overlap(stores, mode):
    store, _, clock = stores
    await store.save("b", definition(mode), "save")
    sessions = []
    for day in (9, 10, 11):
        clock[0] = stamp(f"2026-10-{day:02}T10:00:01")
        await store.tick("worker")
        claim = await store.claim("worker")
        sessions.append(claim["run"]["session_id"])
        # 用确定性的完成记录隔离模型调用。
        async with store.tx() as db:
            await store.finish(db, claim["run"], "succeeded")
    assert len(set(sessions)) == (1 if mode == "merged" else 3)
    assert (await store.runs("b"))["total"] == 3


async def test_missed_latest_only_and_pending_overlap(stores):
    store, _, clock = stores
    await store.save("b", definition(auto_start=False), "save")
    clock[0] = stamp("2026-10-12T10:30:00")
    await store.tick("worker")
    row = (await store.runs("b"))["items"][0]
    assert row["status"] == "awaiting_start" and row["catchup_from"] == stamp(
        "2026-10-09T10:00:00"
    )
    clock[0] = stamp("2026-10-13T10:00:00")
    await store.tick("worker")
    assert (await store.runs("b"))["items"][0]["status"] == "skipped_overlap"


async def test_manual_idempotent_pause_edit_cancel_and_expiration(stores):
    store, _, clock = stores
    a = await store.save("b", definition(), "save")
    run = await store.manual(a["id"], "b", "run", 1)
    assert (await store.manual(a["id"], "b", "run", 1))["id"] == run["id"]
    assert (await store.get(a["id"], "b"))["next_run_at"] == a["next_run_at"]
    await store.action(a["id"], "b", "pause", 1, "pause")
    assert (await store.run(run["id"], "b"))["status"] == "cancelled"
    with pytest.raises(AutomationConflict):
        await store.manual(a["id"], "b", "r2", 1)


async def test_expired_claim_is_fenced(stores):
    store, _, clock = stores
    a = await store.save("b", definition(), "save")
    await store.manual(a["id"], "b", "run", 1)
    old = await store.claim("old")
    clock[0] += 61
    new = await store.claim("new")
    assert old["attempt"]["agent_run_id"] == new["attempt"]["agent_run_id"]
    with pytest.raises(AutomationConflict):
        await store.prepared(old["attempt"], {})
    await store.fail_start(old, "old failure")
    assert (await store.run(old["run"]["id"], "b"))["status"] == "starting"


async def test_retry_uses_new_attempt(stores):
    store, _, _ = stores
    a = await store.save("b", definition(), "save")
    run = await store.manual(a["id"], "b", "run", 1)
    old = await store.claim("w")
    await store.fail_start(old, "failure")
    row = await store.run(run["id"], "b")
    await store.run_action(row["id"], "b", "retry", row["revision"], "retry")
    new = await store.claim("w")
    assert new["attempt"]["attempt_no"] == 2
    assert new["attempt"]["agent_run_id"] != old["attempt"]["agent_run_id"]


async def test_automatic_three_failures_pause(stores):
    store, _, clock = stores
    a = await store.save("b", definition(), "save")
    for day in (9, 10, 11):
        clock[0] = stamp(f"2026-10-{day:02}T10:00:01")
        await store.tick("w")
        await store.fail_start(await store.claim("w"), "failure")
    assert (await store.get(a["id"], "b"))["status"] == "paused"


@pytest.fixture
async def real_time_service(tmp_path):
    # 真正的持久执行入口配假模型；不替换 journal/事务/事件终态。
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.initialize()
    store = AutomationStore(journal.path)
    await store.initialize()
    histories = {}

    async def reply(intent, **kwargs):
        histories.setdefault(intent.shopping_session_id, []).append(intent.raw_query)
        return SimpleNamespace(
            error=None,
            final_text=f"历史轮次 {len(histories[intent.shopping_session_id])}",
        )

    orchestrator = SimpleNamespace(
        handle_intent=reply, available_skills=AsyncMock(return_value={"skills": []})
    )
    runtime = AGUIRuntime(journal, orchestrator)
    registry = SimpleNamespace(
        resolve=AsyncMock(
            return_value={
                "profile_id": "test",
                "profile_revision": 1,
                "model_id": "fixture",
            }
        )
    )
    sessions = SimpleNamespace(assert_owner=AsyncMock())
    service = AutomationService(store, runtime, registry, sessions)
    yield service, histories
    await runtime.shutdown()


@pytest.mark.parametrize("mode", ["merged", "independent"])
async def test_runtime_history_and_restart_reconcile(real_time_service, mode):
    service, histories = real_time_service
    store = service.store
    a = await service.save("b", definition(mode), "save")
    for index in range(3):
        row = await store.manual(a["id"], "b", f"run-{index}", 1)
        claim = await store.claim("w")
        await service.execute(claim)
        await asyncio.gather(
            *(entry.task for entry in service.runtime.running.values())
        )
        # 模拟执行完成后、任务结算前重启；对账不重新请求 Agent。
        restored = AutomationStore(store.path)
        await restored.reconcile()
        result = await restored.run(row["id"], "b")
        assert result["status"] == "succeeded"
        assert result["attempts"][0]["model"]["model_id"] == "fixture"
        assert result["attempts"][0]["usage"] is None
    assert sorted(map(len, histories.values())) == (
        [3] if mode == "merged" else [1, 1, 1]
    )


async def test_edit_between_claim_and_reserve_cannot_start(real_time_service):
    s, histories = real_time_service
    a = await s.save("b", definition(), "save")
    await s.store.manual(a["id"], "b", "run", 1)
    claim = await s.store.claim("w")
    await s.save(
        "b",
        definition(prompt="新的任务"),
        "edit",
        identity=a["id"],
        expected_revision=1,
    )
    await s.execute(claim)
    assert not histories
    assert (await s.store.run(claim["run"]["id"], "b"))["status"] == "cancelled"


async def test_stale_model_blocks_execution(real_time_service):
    s, histories = real_time_service
    a = await s.save("b", definition(), "save")
    await s.store.manual(a["id"], "b", "run", 1)
    s.registry.resolve.return_value = {"profile_revision": 2}
    await s.execute(await s.store.claim("w"))
    assert not histories
    assert (await s.store.runs("b"))["items"][0]["status"] == "failed"


async def test_api_ownership_and_schema(real_time_service):
    s, _ = real_time_service
    api = FastAPI()
    register_automation_routes(api, lambda: s)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        a = (
            await client.post(
                "/commerce/automations",
                json={
                    "buyer_id": "b",
                    "request_id": "create",
                    "definition": definition(),
                },
            )
        ).json()
        response = await client.patch(
            "/commerce/automations/" + a["id"],
            json={
                "buyer_id": "other",
                "request_id": "edit",
                "definition": definition(),
                "expected_revision": 1,
            },
        )
        assert response.status_code == 404
        assert (await client.get("/commerce/automations?buyer_id=other")).json()[
            "items"
        ] == []
        bad = await client.post(
            "/commerce/automations/preview",
            json={"buyer_id": "b", "schedule": {"kind": "cron", "cron": "@reboot"}},
        )
        assert bad.status_code == 422


async def test_completed_but_waiting_confirmation_does_not_succeed(real_time_service):
    s, _ = real_time_service
    a = await s.save("b", definition(), "save")
    run = await s.store.manual(a["id"], "b", "run", 1)
    claim = await s.store.claim("w")
    await s.execute(claim)
    await asyncio.gather(*(x.task for x in s.runtime.running.values()))
    async with s.store.tx() as db:
        await db.execute(
            "UPDATE agui_sessions SET state_json=? WHERE session_id=?",
            ('{"toolApprovals":[{"id":"pending"}]}', run["session_id"]),
        )
    await s.store.reconcile()
    assert (await s.store.run(run["id"], "b"))["status"] == "awaiting_confirmation"
    with pytest.raises(AutomationConflict):
        await s.store.manual(a["id"], "b", "other", 1)
    async with s.store.tx() as db:
        await db.execute(
            "UPDATE agui_sessions SET state_json=? WHERE session_id=?",
            ('{"toolApprovals":[]}', run["session_id"]),
        )
    await s.store.reconcile()
    assert (await s.store.run(run["id"], "b"))["status"] == "succeeded"


async def test_pending_order_prevents_automatic_next_turn(real_time_service):
    s, histories = real_time_service
    a = await s.save("b", definition(), "save")
    await s.store.manual(a["id"], "b", "run", 1)
    s.runtime.confirmations = SimpleNamespace(
        list=AsyncMock(
            return_value={"confirmations": [{"status": "pending", "expired": False}]}
        )
    )
    await s.execute(await s.store.claim("w"))
    assert not histories
    assert (await s.store.runs("b"))["items"][0]["status"] == "skipped_overlap"


async def test_stop_before_start_and_expired_approval(stores):
    store, _, clock = stores
    task = await store.save(
        "b",
        definition(
            auto_start=False,
            schedule={"kind": "daily", "time": "10:00", "end_date": "2026-10-09"},
        ),
        "save",
    )
    clock[0] = stamp("2026-10-09T10:00:01")
    await store.tick("w")
    row = (await store.runs("b"))["items"][0]
    clock[0] = stamp("2026-10-10T00:00:00")
    with pytest.raises(AutomationConflict):
        await store.run_action(row["id"], "b", "start", 1, "start")
    await store.run_action(row["id"], "b", "stop", 1, "stop")
    assert (await store.run(row["id"], "b"))["status"] == "cancelled"


async def test_merged_agentstate_restored_after_process_recreation(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from agentscope.state import AgentState
    from agentscope.message import UserMsg, AssistantMsg
    from app.application.agents.main_agent import SessionRegistry
    from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot

    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "states.db"))
    store = SqlFencedSessionStore(engine)
    factory = SimpleNamespace(
        build=lambda state: SimpleNamespace(state=state or AgentState())
    )
    token = ShoppingContext.set(
        ShoppingContextSnapshot("merged-auto", "b", "zh-CN", "CNY")
    )
    try:
        first = SessionRegistry(factory, store)
        agent = await first.get_or_create("merged-auto")
        agent.state.context.extend(
            [
                UserMsg("buyer", "预算400元，保留SKU-B"),
                AssistantMsg("agent", "已保留SKU-B"),
            ]
        )
        await first.persist("merged-auto")
        await first.invalidate("merged-auto")
        # 新建注册表模拟内存完全丢失；读取真正的 SqlFencedSessionStore。
        second = SessionRegistry(factory, store)
        restored = await second.get_or_create("merged-auto")
        assert "预算400元" in restored.state.model_dump_json()
        assert len(restored.state.context) == 2
        restored.state.context.append(UserMsg("buyer", "第二周继续比较"))
        await second.persist("merged-auto")
        assert (
            len(AgentState.model_validate_json(await store.load("merged-auto")).context)
            == 3
        )
        await second.invalidate("merged-auto")
    finally:
        ShoppingContext.reset(token)
        await engine.dispose()


async def test_pause_cancels_queued_retry_after_prior_agui_execution(real_time_service):
    service, _ = real_time_service
    store = service.store
    task = await service.save("b", definition(), "save")
    run = await store.manual(task["id"], "b", "run", 1)
    claim = await store.claim("worker")
    await service.execute(claim)
    await asyncio.gather(*(entry.task for entry in service.runtime.running.values()))
    # 上次已进入 AG-UI，终态留在日志中；下一次重试仍应能被暂停取消。
    async with store.tx() as db:
        row = await store.owned(db, "automation_runs", run["id"], "b")
        await store.finish(db, row, "interrupted", "模拟中断")
    row = await store.run(run["id"], "b")
    await store.run_action(row["id"], "b", "retry", row["revision"], "retry")
    await store.action(task["id"], "b", "pause", 1, "pause")
    assert (await store.run(row["id"], "b"))["status"] == "cancelled"
    assert await store.claim("worker") is None


@pytest.mark.parametrize("mode", ["merged", "independent"])
async def test_session_origin_restores_after_archive_and_respects_owner(real_time_service, mode):
    service, _ = real_time_service
    store = service.store
    task = await service.save("b", definition(mode), "origin-save")
    pending = await store.manual(task["id"], "b", "origin-run-1", 1)
    assert await store.session_origins("b") == []  # 仅排队不能冒充已经产生的会话。
    sessions = []
    for index in range(2):
        row = pending if index == 0 else await store.manual(task["id"], "b", "origin-run-2", 1)
        await service.execute(await store.claim("w"))
        await asyncio.gather(*(entry.task for entry in service.runtime.running.values()))
        await store.reconcile()
        sessions.append(row["session_id"])
    await store.action(task["id"], "b", "archive", 1, "archive-origin")
    restored = AutomationStore(store.path)
    assert set(await restored.session_origins("b")) == set(sessions)
    assert len(await restored.session_origins("b")) == (1 if mode == "merged" else 2)
    assert await restored.session_origins("other") == []
    # 同一来源查询接口只返回当前买家的轻量 ID，不暴露任务内容。
    api = FastAPI()
    register_automation_routes(api, lambda: service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.get("/commerce/automations/session-origins?buyer_id=b")).json() == {"session_ids": list(await restored.session_origins("b"))}
        assert (await client.get("/commerce/automations/session-origins?buyer_id=other")).json() == {"session_ids": []}
