import asyncio
import httpx
import pytest


async def make(tmp_path):
    from app.infrastructure.commerce.acp import ACPClient, CheckoutApprovals
    from app.infrastructure.commerce.sandbox import create_sandbox_merchant

    app = create_sandbox_merchant(tmp_path / "merchant.db")
    client = ACPClient("http://127.0.0.1:8099", transport=httpx.ASGITransport(app=app))
    return app, client, CheckoutApprovals(tmp_path / "approvals.db", client)


async def test_confirm_restart_replay_and_ownership(tmp_path):
    app, client, service = await make(tmp_path)
    approval = await service.prepare(
        "buyer-a", "session-a", "request-a", ["demo-backpack-black"]
    )
    assert approval["status"] == "pending"
    from app.infrastructure.commerce.acp import CheckoutApprovals

    service = CheckoutApprovals(tmp_path / "approvals.db", client)
    with pytest.raises(LookupError):
        await service.resolve(
            "buyer-b", "session-a", approval["id"], approval["snapshot_hash"], True
        )
    with pytest.raises(ValueError):
        await service.resolve("buyer-a", "session-a", approval["id"], "tampered", True)
    done = await service.resolve(
        "buyer-a", "session-a", approval["id"], approval["snapshot_hash"], True
    )
    assert done["status"] == "completed" and done["checkout"]["order"]["id"].startswith(
        "sandbox-order-"
    )
    assert (
        await service.resolve(
            "buyer-a", "session-a", approval["id"], approval["snapshot_hash"], True
        )
        == done
    )
    assert app.state.completed_count() == 1
    assert (
        await service.prepare(
            "buyer-a", "session-a", "request-a", ["demo-backpack-black"]
        )
        == done
    )
    with pytest.raises(ValueError):
        await service.prepare("buyer-a", "session-a", "request-a", ["demo-cup"])


async def test_rejected_and_changed_price_never_complete(tmp_path):
    app, client, service = await make(tmp_path)
    a = await service.prepare("b", "s", "r", ["demo-backpack-black"])
    assert (await service.resolve("b", "s", a["id"], a["snapshot_hash"], False))[
        "status"
    ] == "rejected"
    with pytest.raises(ValueError):
        await service.resolve("b", "s", a["id"], a["snapshot_hash"], True)
    b = await service.prepare("b", "s", "r2", ["demo-backpack-black"])
    app.state.change_price(b["checkout"]["id"], 99900)
    with pytest.raises(ValueError, match="报价"):
        await service.resolve("b", "s", b["id"], b["snapshot_hash"], True)
    assert app.state.completed_count() == 0


async def test_official_schema_and_sandbox_auth(tmp_path):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    app, client, service = await make(tmp_path)
    a = await service.prepare("b", "s", "r", ["demo-backpack-black"])
    schema = json.loads(
        (Path(__file__).parent / "contracts/acp-2026-04-17/checkout.json").read_text()
    )
    for name, payload in [
        ("CheckoutSession", a["checkout"]),
        (
            "CheckoutSessionWithOrder",
            (await service.resolve("b", "s", a["id"], a["snapshot_hash"], True))[
                "checkout"
            ],
        ),
    ]:
        Draft202012Validator({**schema, "$ref": "#/$defs/" + name}).validate(payload)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sandbox"
    ) as c:
        assert (
            await c.get("/checkout_sessions/" + a["checkout"]["id"])
        ).status_code == 401


async def test_timeout_after_commit_is_reconciled_without_replaying_write(tmp_path):
    app, client, service = await make(tmp_path)
    a = await service.prepare("b", "s", "r", ["demo-backpack-black"])
    complete = client.complete

    async def uncertain(*args, **kwargs):
        await complete(*args, **kwargs)
        raise httpx.ReadTimeout("模拟提交后断线")

    client.complete = uncertain
    with pytest.raises(httpx.ReadTimeout):
        await service.resolve("b", "s", a["id"], a["snapshot_hash"], True)
    recovered = await service.reconcile("b", "s", a["id"])
    assert recovered["status"] == "completed"
    assert app.state.completed_count() == 1


def test_sandbox_client_rejects_non_loopback_and_url_credentials():
    from app.infrastructure.commerce.acp import ACPClient

    for url in [
        "https://merchant.example",
        "http://127.0.0.1.evil.test",
        "http://secret@127.0.0.1",
        "http://localhost:8000/?token=x",
    ]:
        with pytest.raises(ValueError):
            ACPClient(url)


async def test_concurrent_approval_submits_only_once(tmp_path):
    app, client, service = await make(tmp_path)
    a = await service.prepare("b", "s", "race", ["demo-backpack-black"])
    results = await asyncio.gather(
        *[
            service.resolve("b", "s", a["id"], a["snapshot_hash"], True)
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert any(isinstance(r, dict) and r["status"] == "completed" for r in results)
    assert all(isinstance(r, (dict, ValueError)) for r in results)
    assert app.state.completed_count() == 1


async def test_approval_is_bound_to_merchant_endpoint(tmp_path):
    _, client, service = await make(tmp_path)
    a = await service.prepare("b", "s", "origin", ["demo-backpack-black"])
    from app.infrastructure.commerce.acp import ACPClient, CheckoutApprovals

    other = CheckoutApprovals(
        tmp_path / "approvals.db", ACPClient("http://127.0.0.1:8100")
    )
    with pytest.raises(ValueError, match="商家"):
        await other.resolve("b", "s", a["id"], a["snapshot_hash"], True)
