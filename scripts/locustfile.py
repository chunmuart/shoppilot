# scripts/locustfile.py
# 依赖：locust、websocket-client
# 运行：locust -f scripts/locustfile.py --host http://localhost:8000
import json
import os
import time
import uuid

import websocket  # websocket-client
from locust import HttpUser, between, task


API_TOKEN = os.getenv("SHOPPILOT_API_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}
WS_PROTOCOLS = ["shoppilot-events", f"shoppilot-auth.{API_TOKEN}"] if API_TOKEN else ["shoppilot-events"]


class SyncIntentUser(HttpUser):
    """压同步接口：端到端时延 + 排队等待。"""
    wait_time = between(1, 3)

    @task
    def submit_intent(self) -> None:
        payload = {
            "buyer_id": os.getenv("SHOPPILOT_BUYER_ID", "locust-buyer"),
            "raw_query": "预算300元，抗造又不塑料的旅行三件套",
            "locale": "zh-CN",
            "currency": "CNY",
        }
        # name 聚合统计，避免不同 query 被拆成多条曲线
        self.client.post("/commerce/intents", json=payload, headers=AUTH_HEADERS, name="POST /commerce/intents")


class AsyncWsUser(HttpUser):
    """压异步链路：入队 + WS 拉事件到 final.result，量真实事件流时延。"""
    wait_time = between(1, 3)

    @task
    def submit_and_stream(self) -> None:
        session_id = f"locust-{uuid.uuid4().hex[:8]}"
        payload = {
            "buyer_id": os.getenv("SHOPPILOT_BUYER_ID", "locust-buyer"),
            "raw_query": "找几个适合长途飞行的颈枕，要小众设计",
            "locale": "zh-CN",
            "currency": "CNY",
            "shopping_session_id": session_id,
        }
        started = time.monotonic()
        with self.client.post("/commerce/intents/async", json=payload, headers=AUTH_HEADERS,
                              name="POST /commerce/intents/async", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"enqueue failed: {resp.status_code}")
                return

        ws_url = self.host.replace("http", "ws", 1) + "/commerce/events"
        ws = websocket.create_connection(ws_url, timeout=120, subprotocols=WS_PROTOCOLS)
        try:
            ws.send(json.dumps({"shopping_session_id": session_id, "buyer_id": payload["buyer_id"]}))  # 订阅协议见 connection.py
            while True:
                event = json.loads(ws.recv())
                if event.get("type") in ("final.result", "error"):
                    break
            # 手动上报一条端到端耗时，locust 默认只统计 HTTP
            self.environment.events.request.fire(
                request_type="WS", name="stream_to_final_result",
                response_time=(time.monotonic() - started) * 1000, response_length=0,
            )
        finally:
            ws.close()
