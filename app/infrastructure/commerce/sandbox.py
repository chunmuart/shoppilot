"""可独立启动的 ACP 契约沙箱商家；合成目录、库存和订单在独立 SQLite 中。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from fastapi import FastAPI, HTTPException, Request, Depends
from app.infrastructure.commerce.acp import VERSION, SANDBOX_TOKEN, digest


def create_sandbox_merchant(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db():
        conn = sqlite3.connect(path, timeout=15)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    with db() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS inventory (sku TEXT PRIMARY KEY,price INTEGER,stock INTEGER)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY,body TEXT)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY,hash TEXT,body TEXT)"
        )
        c.execute(
            "INSERT OR IGNORE INTO inventory VALUES(?,?,?)",
            ("demo-backpack-black", 12900, 10),
        )
        c.execute(
            "INSERT OR IGNORE INTO inventory VALUES(?,?,?)", ("demo-cup", 3900, 10)
        )

    async def authorize(request: Request):
        if request.headers.get("Authorization") != "Bearer " + SANDBOX_TOKEN:
            raise HTTPException(401, "仅供沙箱凭据")
        if request.headers.get("API-Version") != VERSION:
            raise HTTPException(400, "不支持的 ACP 版本")

    api = FastAPI(
        title="ShopPilot ACP sandbox — no real payment", dependencies=[Depends(authorize)]
    )

    def read(c, id):
        row = c.execute("SELECT body FROM sessions WHERE id=?", (id,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到沙箱会话")
        return json.loads(row[0])

    def replay(c, request, payload):
        key = request.headers.get("Idempotency-Key", "")
        if not 1 <= len(key) <= 128:
            raise HTTPException(400, "需要幂等键")
        signature = digest([request.url.path, payload])
        old = c.execute("SELECT hash,body FROM requests WHERE key=?", (key,)).fetchone()
        if old and old[0] != signature:
            raise HTTPException(422, "幂等键对应的请求体不同")
        return key, signature, json.loads(old[1]) if old else None

    def save(c, key, signature, body):
        raw = json.dumps(body, ensure_ascii=False)
        c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?)", (body["id"], raw))
        c.execute("INSERT INTO requests VALUES(?,?,?)", (key, signature, raw))
        return body

    @api.post("/checkout_sessions", status_code=201)
    async def create(request: Request, payload: dict):
        if (
            set(payload) - {"line_items", "currency", "capabilities"}
            or payload.get("currency") != "cny"
            or payload.get("capabilities") != {}
            or not isinstance(payload.get("line_items"), list)
            or not 1 <= len(payload["line_items"]) <= 20
        ):
            raise HTTPException(422, "本沙箱只支持 CNY 简单商品结账")
        items = payload["line_items"]
        if any(
            not isinstance(i, dict) or set(i) != {"id"} or not isinstance(i["id"], str)
            for i in items
        ):
            raise HTTPException(422, "商品格式无效")
        if len({i["id"] for i in items}) != len(items):
            raise HTTPException(422, "本沙箱每个 SKU 限一件")
        with db() as c:
            key, signature, old = replay(c, request, payload)
            if old:
                return old
            lines = []
            for i, item in enumerate(items):
                row = c.execute(
                    "SELECT price,stock FROM inventory WHERE sku=?", (item["id"],)
                ).fetchone()
                if not row or row[1] < 1:
                    raise HTTPException(409, "无商品或库存不足")
                lines.append(
                    {
                        "id": "line-" + str(i),
                        "item": {
                            "id": item["id"],
                            "name": item["id"],
                            "unit_amount": row[0],
                        },
                        "quantity": 1,
                        "totals": [
                            {
                                "type": "total",
                                "display_text": "沙箱商品金额",
                                "amount": row[0],
                            }
                        ],
                    }
                )
            body = {
                "id": "checkout-" + uuid.uuid4().hex,
                "status": "ready_for_payment",
                "currency": "cny",
                "line_items": lines,
                "totals": [
                    {
                        "type": "total",
                        "display_text": "沙箱总金额",
                        "amount": sum(l["item"]["unit_amount"] for l in lines),
                    }
                ],
                "fulfillment_options": [],
                "messages": [],
                "links": [],
                "capabilities": {},
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            }
            return save(c, key, signature, body)

    @api.get("/checkout_sessions/{id}")
    async def get(id: str):
        with db() as c:
            return read(c, id)

    @api.post("/checkout_sessions/{id}/complete")
    async def complete(id: str, request: Request, payload: dict):
        expected = {
            "payment_data": {
                "handler_id": "sandbox",
                "instrument": {
                    "type": "sandbox",
                    "credential": {"type": "sandbox", "token": SANDBOX_TOKEN},
                },
            }
        }
        if payload != expected:
            raise HTTPException(422, "只接受固定沙箱凭据，禁止真实支付资料")
        with db() as c:
            key, signature, old = replay(c, request, payload)
            if old:
                return old
            body = read(c, id)
            if body["status"] != "ready_for_payment":
                raise HTTPException(409, "结账已处理")
            if datetime.fromisoformat(body["expires_at"]) <= datetime.now(timezone.utc):
                raise HTTPException(409, "报价过期")
            for line in body["line_items"]:
                changed = c.execute(
                    "UPDATE inventory SET stock=stock-1 WHERE sku=? AND stock>=1 AND price=?",
                    (line["item"]["id"], line["item"]["unit_amount"]),
                ).rowcount
                if changed != 1:
                    raise HTTPException(409, "商品库存或价格已变化")
            body["status"] = "completed"
            body["order"] = {
                "id": "sandbox-order-" + uuid.uuid4().hex,
                "checkout_session_id": id,
                "permalink_url": "http://127.0.0.1/sandbox-orders/" + id,
            }
            return save(c, key, signature, body)

    def count():
        with db() as c:
            return sum(
                json.loads(r[0])["status"] == "completed"
                for r in c.execute("SELECT body FROM sessions")
            )

    def change_price(id, amount):
        with db() as c:
            body = read(c, id)
            body["totals"][0]["amount"] = amount
            c.execute("UPDATE sessions SET body=? WHERE id=?", (json.dumps(body), id))

    api.state.completed_count = count
    api.state.change_price = change_price
    return api
