"""真实本地 OTLP/HTTP 接收器验证；不读取真实追踪凭据，不调用付费模型。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from agentscope.message import AssistantMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.middleware import TracingMiddleware
from agentscope.model import ChatResponse, ChatUsage
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import Status, StatusCode

from app.application.agents.orchestrator import SubmitIntentInput
from app.domain.queue.ports.task_queue import IntentTask
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEvent, TradeEventBus
from app.infrastructure import tracing
from app.infrastructure.tracing import (
    TracingASGIMiddleware, _export_settings, correlate, create_tracer_provider,
    current_correlation, inject_task_context, setup_tracing,
    trace_worker_task,
)
from tests.test_phase4_model import _build
from tests.test_retrieval import _settings

SECRET = "buyer@example.com 收货地址测试路1号 电话13800000000 私密对话"


@pytest.fixture
def collector():
    captured = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            decoded = ExportTraceServiceRequest.FromString(body)
            captured.append((self.path, dict(self.headers), decoded, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(endpoint=f"http://127.0.0.1:{server.server_port}/api/public/otel", captured=captured)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def settings(**changes):
    return replace(_settings(Path(".")), **{"otlp_endpoint": "", **changes})


@pytest.fixture
def traced(collector, monkeypatch):
    authentication = "Basic " + base64.b64encode(b"pk-local-test:sk-local-test").decode()
    configured = settings(otlp_traces_endpoint=collector.endpoint + "/v1/traces",
        otlp_traces_headers=f"Authorization={authentication},x-langfuse-ingestion-version=4")
    provider = create_tracer_provider(configured)
    # 真实 SDK provider，只隔离全局安装，避免改变其他测试的 AgentScope 模式。
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    yield SimpleNamespace(provider=provider, collector=collector, authentication=authentication)
    provider.shutdown()


def spans(captured):
    return [span for _, _, batch, _ in captured for resource in batch.resource_spans
            for scope in resource.scope_spans for span in scope.spans]


def attributes(span):
    return {attribute.key: getattr(attribute.value, attribute.value.WhichOneof("value")) for attribute in span.attributes}


def test_specific_endpoint_and_header_configuration_wins_without_double_suffix():
    base = settings(otlp_endpoint="http://localhost:4318/api/public/otel")
    endpoint, headers = _export_settings(replace(base,
        otlp_traces_endpoint="http://localhost:4320/custom/traces", otlp_headers="Authorization=generic",
        otlp_traces_headers="Authorization=Basic%20local,x-langfuse-ingestion-version=4"))
    assert endpoint == "http://localhost:4320/custom/traces"
    assert headers == {"Authorization": "Basic local", "x-langfuse-ingestion-version": "4"}
    assert _export_settings(base)[0].endswith("/api/public/otel/v1/traces")
    assert _export_settings(replace(base, otlp_endpoint=base.otlp_endpoint + "/v1/traces"))[0].count("/v1/traces") == 1


@pytest.mark.parametrize("endpoint", ["ftp://host/otel", "https://secret@host/otel", "https://host/otel?key=secret"])
def test_invalid_endpoint_cannot_embed_secrets(endpoint):
    with pytest.raises(ValueError):
        _export_settings(settings(otlp_endpoint=endpoint))


def test_settings_repr_hides_otlp_credentials():
    configured = settings(otlp_headers="private-header", otlp_traces_headers="private-trace-header")
    assert "private-header" not in repr(configured) and "private-trace-header" not in repr(configured)


def test_selected_skill_trace_keeps_version_evidence_but_never_reference_body():
    clean = tracing._sanitize_attributes({
        'shoppilot.skill.source': 'server_preload',
        'shoppilot.skill.id': 'shopping-needs-clarification',
        'shoppilot.skill.version': '1.0',
        'shoppilot.skill.content_hash': 'a' * 64,
        'shoppilot.skill.body': SECRET,
        'gen_ai.input.messages': SECRET,
    })
    assert clean['shoppilot.skill.source'] == 'server_preload'
    assert clean['shoppilot.skill.id'] == 'shopping-needs-clarification'
    assert clean['shoppilot.skill.version'] == '1.0'
    assert clean['shoppilot.skill.content_hash'] == 'a' * 64
    assert 'shoppilot.skill.body' not in clean
    assert SECRET not in json.dumps(clean, ensure_ascii=False)
    assert clean['shoppilot.content.redacted'] is True


def test_unconfigured_tracing_never_installs_exporter(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(tracing, "create_tracer_provider", create)
    monkeypatch.setattr(tracing, "_initialized", False)
    setup_tracing(settings())
    create.assert_not_called()


def test_invalid_optional_export_configuration_isolated_from_business(monkeypatch, caplog):
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.ProxyTracerProvider())
    monkeypatch.setattr(tracing, "_initialized", False)
    monkeypatch.setattr(tracing, "create_tracer_provider", Mock(side_effect=ValueError(SECRET)))
    setup_tracing(settings(otlp_endpoint="http://localhost/otel"))
    assert "初始化失败" in caplog.text
    assert SECRET not in caplog.text


async def test_otlp_real_http_path_auth_and_native_agentscope_parent_chain(traced, monkeypatch):
    from app.presentation.server import _enqueue
    from app import worker

    bus = TradeEventBus()
    queue = []
    observed = bus.subscribe("session-private")

    async def enqueue(task):
        # 与 Redis 一样通过 JSON 边界，确认标准 carrier 字段不会在进程边界丢失。
        queue.append(IntentTask.from_dict(json.loads(json.dumps(task.to_dict()))))

    container = SimpleNamespace(task_queue=SimpleNamespace(enqueue=enqueue, get_status=AsyncMock(return_value=SimpleNamespace(state="queued"))), bus=bus,
        settings=SimpleNamespace(queue_priority_enabled=False), cache=SimpleNamespace(enabled=False))
    api = FastAPI()
    api.add_middleware(TracingASGIMiddleware)

    @api.post("/commerce/intents/async")
    async def submit():
        task_id = await _enqueue(container, SubmitIntentInput("session-private", "buyer-private", "zh-CN", "CNY", SECRET), request_id="client-submit-1")
        return {"task_id": task_id}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/commerce/intents/async", headers={"tracestate": "vendor=state"})
    assert response.status_code == 200 and len(response.headers["x-trace-id"]) == 32
    task = queue[0]
    assert task.request_id == response.headers["x-request-id"]
    assert task.traceparent.split("-")[1] == response.headers["x-trace-id"]
    assert task.raw_query == SECRET

    middleware = TracingMiddleware()
    agent = SimpleNamespace(name="TraceTestAgent", state=SimpleNamespace(session_id="native-session", reply_id="reply-test"), toolkit=SimpleNamespace(tools={}))
    model = _build([])

    class NativeOrchestrator:
        async def handle_intent(self, intent, **options):
            assert options["fresh_session"] and options["persistence_guard"]()
            token = ShoppingContext.set(ShoppingContextSnapshot(intent.shopping_session_id, intent.buyer_id, intent.locale, intent.currency))

            async def next_model(**_kwargs):
                return ChatResponse(content=[TextBlock(text=SECRET)], is_last=True,
                    usage=ChatUsage(input_tokens=13, output_tokens=7, time=0.1))

            async def next_tool(**_kwargs):
                yield {"result": SECRET}

            async def next_reply(**_kwargs):
                await middleware.on_model_call(agent, {"current_model": model, "messages": [{"role": "user", "content": SECRET}]}, next_model)
                async for _result in middleware.on_acting(agent,
                    {"tool_call": ToolCallBlock(id="tool-test", name="product_search_tool", input=json.dumps({"query": SECRET}))}, next_tool):
                    pass
                yield AssistantMsg("TraceTestAgent", SECRET)

            try:
                async for _message in middleware.on_reply(agent, {"inputs": UserMsg("buyer", SECRET)}, next_reply):
                    pass
                bus.publish(intent.shopping_session_id, "final.result", {"final_text": SECRET})
                return SimpleNamespace(error=None, final_text=SECRET)
            finally:
                ShoppingContext.reset(token)

    monkeypatch.setattr(worker, "current_execution_lease", lambda: SimpleNamespace(is_valid=lambda: True))
    assert await worker.execute_intent_task(task, NativeOrchestrator(), bus) == SECRET
    assert await asyncio.to_thread(traced.provider.force_flush, 5000)
    exported = spans(traced.collector.captured)
    assert len(exported) == 5
    root = next(span for span in exported if span.name == "POST /commerce/intents/async")
    consume = next(span for span in exported if span.name == "commerce.intent.consume")
    agent_span = next(span for span in exported if span.name == "invoke_agent TraceTestAgent")
    model_span = next(span for span in exported if span.name == "chat primary-model")
    tool_span = next(span for span in exported if span.name == "execute_tool product_search_tool")
    assert consume.parent_span_id == root.span_id
    assert agent_span.parent_span_id == consume.span_id
    assert model_span.parent_span_id == tool_span.parent_span_id == agent_span.span_id
    assert {span.trace_id for span in exported} == {root.trace_id}
    assert attributes(model_span)["gen_ai.usage.input_tokens"] == 13
    assert attributes(model_span)["gen_ai.usage.output_tokens"] == 7
    assert attributes(model_span)["langfuse.observation.type"] == "generation"
    for span in exported:
        attrs = attributes(span)
        assert attrs["shoppilot.request_id"] == task.request_id
        assert attrs["shoppilot.task_id"] == task.task_id
        assert attrs["langfuse.session.id"].startswith("sha256:")
    for path, headers, _batch, wire in traced.collector.captured:
        assert path == "/api/public/otel/v1/traces"
        assert headers["Authorization"] == traced.authentication
        assert headers["x-langfuse-ingestion-version"] == "4"
        assert "application/x-protobuf" in headers["Content-Type"]
        for secret in (SECRET, "13800000000", "buyer-private", "session-private", "native-session"):
            assert secret.encode() not in wire
    events = []
    while not observed.empty():
        events.append(observed.get_nowait())
    assert all(event.correlation["trace_id"] == response.headers["x-trace-id"] for event in events)
    assert all(event.correlation["request_id"] == task.request_id for event in events)
    assert TradeEvent.from_dict(events[-1].to_dict()) == events[-1]


async def test_w3c_remote_parent_and_tracestate_survive_http_and_queue(traced):
    api = FastAPI()
    api.add_middleware(TracingASGIMiddleware)
    captured = {}

    @api.get("/test")
    async def handler():
        captured.update(inject_task_context(session_id="session", task_id="task"))
        return {"ok": True}

    parent = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/test", headers={"traceparent": parent, "tracestate": "vendor=opaque"})
    assert response.headers["x-trace-id"] == parent.split("-")[1]
    assert captured["tracestate"] == "vendor=opaque"
    assert captured["traceparent"].split("-")[2] != parent.split("-")[2]
    assert await asyncio.to_thread(traced.provider.force_flush, 5000)
    root = spans(traced.collector.captured)[0]
    assert root.parent_span_id.hex() == "1234567890abcdef"


async def test_exception_text_and_traceback_never_reach_otlp(traced):
    with trace.get_tracer("test", attributes={"internal.secret": SECRET}).start_as_current_span("failure") as span:
        span.set_attribute("gen_ai.tool.call.arguments", SECRET)
        span.set_attribute("authorization", "private-auth")
        span.set_status(Status(StatusCode.ERROR, SECRET))
        span.record_exception(ValueError(SECRET))
    assert await asyncio.to_thread(traced.provider.force_flush, 5000)
    exported = spans(traced.collector.captured)[0]
    assert exported.status.code == 2 and exported.status.message == ""
    assert [item.key for item in exported.events[0].attributes] == ["exception.type"]
    assert attributes(exported)["shoppilot.content.redacted"] is True
    assert attributes(exported)["shoppilot.input.characters"] == len(SECRET)
    wire = b"".join(row[3] for row in traced.collector.captured)
    assert SECRET.encode() not in wire and b"private-auth" not in wire


async def test_parallel_worker_contexts_do_not_mix_request_task_or_session(traced):
    async def run(index):
        task = IntentTask(task_id=f"task-{index}", shopping_session_id=f"session-{index}", buyer_id="buyer",
            locale="zh-CN", currency="CNY", raw_query=SECRET, request_id=f"request-{index}")
        with trace_worker_task(task):
            before = current_correlation()
            await asyncio.sleep(0)
            assert current_correlation() == before
            return before

    one, two = await asyncio.gather(run(1), run(2))
    assert one["request_id"] == "request-1" and two["request_id"] == "request-2"
    assert one["task_id"] == "task-1" and two["task_id"] == "task-2"
    assert one["session_id"] != two["session_id"]
    assert one["trace_id"] != two["trace_id"]


async def test_worker_cancellation_is_error_span_without_exception_body(traced):
    task = IntentTask(task_id="cancel-task", shopping_session_id="session", buyer_id="buyer", locale="zh-CN", currency="CNY", raw_query=SECRET)
    with pytest.raises(asyncio.CancelledError):
        with trace_worker_task(task):
            raise asyncio.CancelledError(SECRET)
    assert await asyncio.to_thread(traced.provider.force_flush, 5000)
    exported = spans(traced.collector.captured)[0]
    assert exported.status.code == 2
    assert attributes(exported)["shoppilot.cancelled"] is True
    assert attributes(exported)["error.type"] == "CancelledError"
    assert SECRET.encode() not in b"".join(row[3] for row in traced.collector.captured)


async def test_sse_span_stays_open_until_last_chunk_and_logs_have_correlation(traced, caplog):
    from fastapi.responses import StreamingResponse

    api = FastAPI()
    api.add_middleware(TracingASGIMiddleware)
    current = []

    async def stream():
        current.append(current_correlation())
        logging.getLogger("tracing-test").warning("安全的测试日志")
        yield "data: first\n\n"
        await asyncio.sleep(0)
        current.append(current_correlation())
        yield "data: last\n\n"

    @api.get("/stream")
    async def handler():
        return StreamingResponse(stream(), media_type="text/event-stream")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/stream")
    assert current[0]["trace_id"] == current[1]["trace_id"] == response.headers["x-trace-id"]
    assert current[0]["span_id"] == current[1]["span_id"]
    record = next(record for record in caplog.records if record.name == "tracing-test")
    assert record.trace_id == response.headers["x-trace-id"]
    assert record.request_id == response.headers["x-request-id"]
    assert await asyncio.to_thread(traced.provider.force_flush, 5000)
    assert len(spans(traced.collector.captured)) == 1


def test_legacy_queue_payload_without_trace_remains_readable():
    task = IntentTask.from_dict({"task_id": "old", "shopping_session_id": "session"})
    assert task.traceparent == task.tracestate == task.request_id == ""
    assert IntentTask.from_dict(task.to_dict()) == task


async def test_unconfigured_api_retains_business_and_request_id_without_fake_trace(monkeypatch):
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.NoOpTracerProvider())
    api = FastAPI()
    api.add_middleware(TracingASGIMiddleware)

    @api.get("/health")
    async def handler():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert len(response.headers["x-request-id"]) == 32
    assert "x-trace-id" not in response.headers


def test_export_failure_returns_failure_without_recording_sensitive_exception(caplog):
    class BrokenExporter:
        def export(self, _spans):
            raise RuntimeError(SECRET)

    result = tracing.SanitizingSpanExporter(BrokenExporter()).export([])
    assert result == SpanExportResult.FAILURE
    assert SECRET not in caplog.text
