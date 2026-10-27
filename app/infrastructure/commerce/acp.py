"""ACP 2026-04-17 沙箱适配器与持久审批；本版只允许回环商家，不能用于真实支付。"""

from __future__ import annotations
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, quote
import uuid
import httpx

VERSION = "2026-04-17"
SANDBOX_TOKEN = "shoppilot-sandbox-only"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def quote_hash(checkout):
    # status、消息和订单回执不参与报价；SKU、数量、费用、地址、有效期必须绑定审批。
    return digest(
        {
            k: checkout.get(k)
            for k in (
                "id",
                "currency",
                "line_items",
                "totals",
                "fulfillment_details",
                "selected_fulfillment_options",
                "fulfillment_groups",
                "expires_at",
                "quote_id",
                "quote_expires_at",
            )
        }
    )


def validate_checkout(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value["id"])
    ):
        raise ValueError("商家返回无效会话")
    if value.get("status") not in {
        "ready_for_payment",
        "completed",
        "canceled",
        "expired",
        "complete_in_progress",
    }:
        raise ValueError("沙箱暂不支持商家的当前结账状态")
    if not isinstance(value.get("currency"), str) or not re.fullmatch(
        r"[A-Za-z]{3}", value["currency"]
    ):
        raise ValueError("商家返回无效币种")
    for key in ("line_items", "totals", "fulfillment_options", "messages", "links"):
        if not isinstance(value.get(key), list):
            raise ValueError("商家缺少 " + key)
    if not value["line_items"] or not isinstance(value.get("capabilities"), dict):
        raise ValueError("缺少商品或协商能力")
    totals = value["totals"]
    if sum(t.get("type") == "total" for t in totals) != 1:
        raise ValueError("报价需要唯一总金额")
    for total in totals:
        if type(total.get("amount")) is not int or total["amount"] < 0:
            raise ValueError("金额必须是非负整数最小币种单位")
    seen = set()
    for line in value["line_items"]:
        if (
            not isinstance(line.get("item"), dict)
            or not isinstance(line["item"].get("id"), str)
            or type(line.get("quantity")) is not int
            or line["quantity"] <= 0
            or line.get("id") in seen
        ):
            raise ValueError("无效或重复商品明细")
        seen.add(line.get("id"))
    if value["status"] == "completed" and (
        not isinstance(value.get("order"), dict)
        or not value["order"].get("id")
        or value["order"].get("checkout_session_id") != value["id"]
    ):
        raise ValueError("完成状态缺少匹配订单回执")
    return value


class ACPClient:
    def __init__(self, base_url, *, transport=None, timeout=10):
        url = urlsplit(base_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("本版 ACP 仅支持无凭据的本机 HTTP 沙箱地址")
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout

    async def request(self, method, path, *, payload=None, key=None):
        headers = {"Authorization": "Bearer " + SANDBOX_TOKEN, "API-Version": VERSION}
        if key:
            headers["Idempotency-Key"] = key
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, path, headers=headers, json=payload)
            response.raise_for_status()
            if len(response.content) > 262144:
                raise ValueError("商家响应过大")
            return validate_checkout(response.json())

    async def create(self, items, key):
        return await self.request(
            "POST",
            "/checkout_sessions",
            key=key,
            payload={
                "currency": "cny",
                "line_items": [{"id": item} for item in items],
                "capabilities": {},
            },
        )

    async def get(self, session):
        return await self.request(
            "GET", "/checkout_sessions/" + quote(session, safe="")
        )

    async def complete(self, session, key):
        return await self.request(
            "POST",
            "/checkout_sessions/" + quote(session, safe="") + "/complete",
            key=key,
            payload={
                "payment_data": {
                    "handler_id": "sandbox",
                    "instrument": {
                        "type": "sandbox",
                        "credential": {"type": "sandbox", "token": SANDBOX_TOKEN},
                    },
                }
            },
        )


class CheckoutApprovals:
    """提交后不确定时仅查询回执；从不自动重放支付请求。"""

    def __init__(self, path: Path, client: ACPClient):
        self.path, self.client = path, client
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS acp_approvals (id TEXT PRIMARY KEY,buyer TEXT,session TEXT,request_hash TEXT,body TEXT)"
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def read(self, buyer, session, id):
        with self.db() as db:
            row = db.execute(
                "SELECT body FROM acp_approvals WHERE id=? AND buyer=? AND session=?",
                (id, buyer, session),
            ).fetchone()
        if not row:
            raise LookupError("确认不存在或不属于当前买家会话")
        body = json.loads(row[0])
        if body.get("merchant") != self.client.base_url:
            raise ValueError("确认所属商家与当前配置不同")
        return body

    def save_status(self, buyer, session, id, status, checkout=None):
        with self.db() as db:
            row = db.execute(
                "SELECT body FROM acp_approvals WHERE id=? AND buyer=? AND session=?",
                (id, buyer, session),
            ).fetchone()
            if not row:
                raise LookupError("确认不存在")
            body = json.loads(row[0])
            body["status"] = status
            if checkout is not None:
                body["checkout"] = checkout
            db.execute(
                "UPDATE acp_approvals SET body=? WHERE id=?",
                (json.dumps(body, ensure_ascii=False), id),
            )
        return body

    async def prepare(self, buyer, session, request_id, items):
        if (
            not all(
                isinstance(x, str) and 0 < len(x) <= 128
                for x in (buyer, session, request_id)
            )
            or not isinstance(items, list)
            or not 1 <= len(items) <= 20
            or len(set(items)) != len(items)
            or not all(
                isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", x)
                for x in items
            )
        ):
            raise ValueError("沙箱每个 SKU 限一件，需要有效买家、会话及请求 ID")
        id = (
            "approval-"
            + digest([self.client.base_url, buyer, session, request_id])[:32]
        )
        request_hash = digest(items)
        with self.db() as db:
            row = db.execute(
                "SELECT request_hash,body FROM acp_approvals WHERE id=?", (id,)
            ).fetchone()
        if row:
            if row[0] != request_hash:
                raise ValueError("请求 ID 已用于不同商品")
            return json.loads(row[1])
        checkout = await self.client.create(items, id)
        if checkout["currency"].lower() != "cny":
            raise ValueError("商家返回币种与请求不一致")
        if sorted(line["item"]["id"] for line in checkout["line_items"]) != sorted(
            items
        ) or any(line["quantity"] != 1 for line in checkout["line_items"]):
            raise ValueError("商家返回的商品与请求不一致")
        body = {
            "id": id,
            "status": "pending",
            "snapshot_hash": quote_hash(checkout),
            "checkout": checkout,
            "sandbox": True,
            "merchant": self.client.base_url,
        }
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO acp_approvals VALUES(?,?,?,?,?)",
                (
                    id,
                    buyer,
                    session,
                    request_hash,
                    json.dumps(body, ensure_ascii=False),
                ),
            )
            row = db.execute(
                "SELECT request_hash,body FROM acp_approvals WHERE id=?", (id,)
            ).fetchone()
            if row[0] != request_hash:
                raise ValueError("并发请求参数冲突")
        return json.loads(row[1])

    async def resolve(self, buyer, session, id, snapshot_hash, approved):
        if type(approved) is not bool:
            raise ValueError("需要明确的批准或拒绝动作")
        with self.db() as db:
            row = db.execute(
                "SELECT body FROM acp_approvals WHERE id=? AND buyer=? AND session=?",
                (id, buyer, session),
            ).fetchone()
            if not row:
                raise LookupError("确认不存在或不属于当前买家会话")
            body = json.loads(row[0])
            if body.get("merchant") != self.client.base_url:
                raise ValueError("确认所属商家与当前配置不同")
            if body["snapshot_hash"] != snapshot_hash:
                raise ValueError("审批内容已改变")
            if (body["status"] == "completed" and approved) or (
                body["status"] == "rejected" and not approved
            ):
                return body
            if body["status"] != "pending":
                raise ValueError("确认已处理；不确定状态请查询回执")
            body["status"] = "executing" if approved else "rejected"
            db.execute(
                "UPDATE acp_approvals SET body=? WHERE id=?",
                (json.dumps(body, ensure_ascii=False), id),
            )
        if not approved:
            return body
        try:
            current = await self.client.get(body["checkout"]["id"])
            if (
                quote_hash(current) != snapshot_hash
                or current["status"] != "ready_for_payment"
            ):
                self.save_status(buyer, session, id, "stale")
                raise ValueError("报价或状态已变化，须重新准备和确认")
            from datetime import datetime, timezone

            expires = current.get("expires_at") or current.get("quote_expires_at")
            if expires and datetime.fromisoformat(
                expires.replace("Z", "+00:00")
            ) <= datetime.now(timezone.utc):
                self.save_status(buyer, session, id, "stale")
                raise ValueError("报价已过期")
            result = await self.client.complete(current["id"], id + "-complete")
            if result["status"] != "completed" or quote_hash(result) != snapshot_hash:
                raise ValueError("提交回执与确认内容不一致，须核对商家状态")
            return self.save_status(buyer, session, id, "completed", result)
        except BaseException:
            if self.read(buyer, session, id)["status"] == "executing":
                self.save_status(buyer, session, id, "uncertain")
            raise

    async def reconcile(self, buyer, session, id):
        body = self.read(buyer, session, id)
        if body["status"] not in {"uncertain", "executing"}:
            return body
        current = await self.client.get(body["checkout"]["id"])
        if (
            current["status"] == "completed"
            and quote_hash(current) == body["snapshot_hash"]
        ):
            return self.save_status(buyer, session, id, "completed", current)
        # 即使 GET 仍显示未支付，也不能推断上次请求未在商家队列中执行。
        return self.save_status(buyer, session, id, "uncertain")
