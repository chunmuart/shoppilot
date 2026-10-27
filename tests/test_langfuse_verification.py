"""真实本机 HTTP 验证 Langfuse 只读合同与自动 OTLP 配置，不访问远端。"""
from __future__ import annotations

import base64
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.infrastructure import langfuse_config
from app.infrastructure.langfuse_config import LANGFUSE_FIELDS, LangfuseConfig
from app.infrastructure.tracing import _export_settings, create_tracer_provider
from scripts.verify_langfuse import audit_score, audit_trace, main, verify
from tests.test_tracing import collector, settings

TRACE = "a" * 32
PUBLIC, SECRET = "pk-local-test", "sk-local-test-private"
AUTH = "Basic " + base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()


def observations():
    rows = []
    for identifier, parent, kind, name in [
        ("api", None, "SPAN", "POST /commerce/intents"),
        ("worker", "api", "SPAN", "commerce.intent.consume"),
        ("agent", "worker", "AGENT", "invoke_agent main"),
        ("model", "agent", "GENERATION", "chat model"),
        ("tool", "agent", "TOOL", "execute_tool product_search_tool"),
    ]:
        rows.append({"id": identifier, "parentObservationId": parent, "type": kind,
            "name": name, "traceId": TRACE, "projectId": "project-local", "level": "DEFAULT",
            "startTime": "2026-09-09T00:00:00Z", "endTime": "2026-09-09T00:00:01Z",
            "inputUsage": 13 if kind == "GENERATION" else None,
            "outputUsage": 7 if kind == "GENERATION" else None,
            "totalCost": 0.001 if kind == "GENERATION" else None,
            # 即使上游意外多返字段，报告也不能将它们序列化。
            "input": SECRET, "metadata": {"private": SECRET}, "statusMessage": SECRET})
    return rows


@pytest.fixture
def service():
    state = SimpleNamespace(requests=[], rows=observations(), scores=[], status=200,
                            pages=False, project_id="project-local")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            state.requests.append((parsed.path, params, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != AUTH:
                self.send_response(401)
                self.end_headers()
                return
            if state.status != 200:
                self.send_response(state.status)
                self.end_headers()
                self.wfile.write(SECRET.encode())
                return
            if parsed.path == "/api/public/projects":
                payload = {"data": [{"id": state.project_id, "name": SECRET}]}
            elif parsed.path == "/api/public/v2/observations":
                if state.pages and not params.get("cursor"):
                    payload = {"data": state.rows[:2], "meta": {"cursor": "next-page"}}
                else:
                    payload = {"data": state.rows[2:] if state.pages else state.rows, "meta": {}}
            elif parsed.path == "/api/public/v3/scores":
                payload = {"data": state.scores, "meta": {}}
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.config = LangfuseConfig(f"http://127.0.0.1:{server.server_port}", PUBLIC, SECRET)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_config_missing_is_explicit_and_never_requests(monkeypatch):
    monkeypatch.setattr("httpx.Client", lambda *args, **kwargs: pytest.fail("不应发请求"))
    report = verify(LangfuseConfig.from_env(environ={}))
    assert report["missing_fields"] == list(LANGFUSE_FIELDS)
    assert report["remote_trace_verified"] is False


def test_config_env_file_precedence_and_no_unrelated_key(tmp_path):
    path = tmp_path / "local.env"
    path.write_text("LANGFUSE_BASE_URL=https://cloud.langfuse.com\nLANGFUSE_PUBLIC_KEY=pk-file\n"
                    "LANGFUSE_SECRET_KEY=sk-file\nLLM_API_KEY=irrelevant\n")
    config = LangfuseConfig.from_env(path, environ={"LANGFUSE_SECRET_KEY": "sk-env"})
    assert config.secret_key == "sk-file"
    assert "sk-file" not in repr(config)
    assert "pk-file" not in repr(config)
    assert not hasattr(config, "llm_api_key")


def test_default_env_file_fallback_environment_wins_and_injected_env_isolated(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("LANGFUSE_BASE_URL=https://cloud.langfuse.com\nLANGFUSE_PUBLIC_KEY=pk-file\n"
                    "LANGFUSE_SECRET_KEY=sk-file\n")
    monkeypatch.setattr(langfuse_config, "DEFAULT_ENV_FILE", path)
    for name in LANGFUSE_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    config = LangfuseConfig.from_env()
    assert config.public_key == "pk-file" and config.secret_key == "sk-env"
    assert LangfuseConfig.from_env(environ={}).missing_fields() == list(LANGFUSE_FIELDS)


def test_env_file_cannot_expand_other_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OTHER_SECRET", "private-value")
    path = tmp_path / "local.env"
    path.write_text("LANGFUSE_BASE_URL=https://cloud.langfuse.com\nLANGFUSE_PUBLIC_KEY=pk-file\n"
                    "LANGFUSE_SECRET_KEY=${OTHER_SECRET}\n")
    config = LangfuseConfig.from_env(path, environ={})
    assert config.secret_key == "${OTHER_SECRET}"
    with pytest.raises(ValueError, match="credentials_invalid"):
        config.validate()


def test_api_and_cli_reject_same_literal_placeholder_without_changing_other_env_semantics(tmp_path, monkeypatch):
    from app.infrastructure.settings import _load_environment, load_settings
    for name in (*LANGFUSE_FIELDS, "LLM_API_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OTHER_SECRET", "private-unrelated-value")
    monkeypatch.setenv("LOCAL_MODEL_TOKEN", "local-model-test-token")
    path = tmp_path / ".env"
    path.write_text("LANGFUSE_BASE_URL=https://cloud.langfuse.com\nLANGFUSE_PUBLIC_KEY=pk-file\n"
        "LANGFUSE_SECRET_KEY=${OTHER_SECRET}\nLLM_API_KEY=${LOCAL_MODEL_TOKEN}\n")
    _load_environment(path)
    configured = load_settings()
    assert configured.langfuse_secret_key == "${OTHER_SECRET}"
    assert configured.llm_api_key == "local-model-test-token"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "${OTHER_SECRET}"
    with pytest.raises(ValueError, match="langfuse_credentials_invalid"):
        _export_settings(configured)
    report = verify(LangfuseConfig.from_env(path, environ={}))
    assert report["status"] == "CONFIGURATION_INVALID"
    assert "private-unrelated-value" not in json.dumps(report)


def test_settings_langfuse_process_environment_still_wins(tmp_path, monkeypatch):
    from app.infrastructure.settings import _load_environment
    for name in LANGFUSE_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "explicit-process-key")
    path = tmp_path / ".env"
    path.write_text("LANGFUSE_BASE_URL=https://cloud.langfuse.com\nLANGFUSE_PUBLIC_KEY=pk-file\n"
                    "LANGFUSE_SECRET_KEY=${OTHER_SECRET}\n")
    _load_environment(path)
    assert os.environ["LANGFUSE_SECRET_KEY"] == "explicit-process-key"


@pytest.mark.parametrize("url", ["https://secret@host", "http://remote.test", "https://host?secret=x",
    "https://host/#key", "https://host:broken", "https://host /path", "file:///tmp/file"])
def test_invalid_url_is_rejected_without_echo(url):
    with pytest.raises(ValueError, match="langfuse_base_url_invalid") as error:
        LangfuseConfig(url, PUBLIC, SECRET).validate()
    assert SECRET not in str(error.value) and url not in str(error.value)


def test_derived_otlp_configuration_and_settings_repr_hide_secrets():
    config = settings(langfuse_base_url="https://cloud.langfuse.com/", langfuse_public_key=PUBLIC,
                      langfuse_secret_key=SECRET)
    endpoint, headers = _export_settings(config)
    assert endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert headers == {"Authorization": AUTH, "x-langfuse-ingestion-version": "4"}
    assert PUBLIC not in repr(config) and SECRET not in repr(config)


def test_explicit_collector_never_receives_langfuse_credentials():
    config = settings(langfuse_base_url="https://cloud.langfuse.com", langfuse_public_key=PUBLIC,
        langfuse_secret_key=SECRET, otlp_endpoint="http://localhost:4318",
        otlp_headers="Authorization=custom-auth")
    assert _export_settings(config) == ("http://localhost:4318/v1/traces", {"Authorization": "custom-auth"})
    # Langfuse 配错时不能连带禁用已显式配置好的通用接收器。
    assert _export_settings(replace(config, langfuse_base_url="broken")) == _export_settings(config)


def test_explicit_headers_override_derived_auth_without_case_duplicate():
    endpoint, headers = _export_settings(settings(langfuse_base_url="https://cloud.langfuse.com",
        langfuse_public_key=PUBLIC, langfuse_secret_key=SECRET,
        otlp_traces_endpoint="https://cloud.langfuse.com/api/public/otel/v1/traces",
        otlp_headers="Authorization=ignored", otlp_traces_headers="authorization=override,x-langfuse-ingestion-version=3"))
    assert endpoint.endswith("/api/public/otel/v1/traces")
    assert headers == {"authorization": "override", "x-langfuse-ingestion-version": "3"}


def test_derived_config_uses_real_otlp_sanitizer_and_v4_http(collector):
    base_url = collector.endpoint.removesuffix("/api/public/otel")
    provider = create_tracer_provider(settings(langfuse_base_url=base_url,
        langfuse_public_key=PUBLIC, langfuse_secret_key=SECRET))
    with provider.get_tracer("verification").start_as_current_span("local-test") as span:
        span.set_attribute("gen_ai.input.messages", SECRET)
        span.set_attribute("gen_ai.usage.input_tokens", 13)
    assert provider.force_flush()
    provider.shutdown()
    path, headers, _batch, raw = collector.captured[0]
    assert path == "/api/public/otel/v1/traces"
    assert headers["Authorization"] == AUTH
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert SECRET.encode() not in raw


def test_real_read_only_project_access_is_not_trace_pass(service):
    report = verify(service.config)
    assert report["status"] == "PROJECT_ACCESS_VERIFIED"
    assert report["remote_trace_verified"] is False
    assert [path for path, _, _ in service.requests] == ["/api/public/projects"]
    assert SECRET not in json.dumps(report)


def test_real_read_only_trace_pagination_parent_chain_and_usage(service):
    service.pages = True
    report = verify(service.config, trace_id=TRACE, required={"api", "worker", "agent", "model", "tool"})
    assert report["status"] == "VERIFIED"
    assert report["trace"]["parent_link_count"] == 4
    assert report["trace"]["input_tokens"] == 13 and report["trace"]["output_tokens"] == 7
    assert SECRET not in json.dumps(report) and AUTH not in json.dumps(report)
    queries = [params for path, params, _ in service.requests if path.endswith("observations")]
    assert len(queries) == 2 and queries[1]["cursor"] == ["next-page"]
    assert all(params["traceId"] == [TRACE] and "fromStartTime" in params and "toStartTime" in params for params in queries)
    assert all("io" not in params["fields"][0].split(",") and "metadata" not in params["fields"][0].split(",") for params in queries)


@pytest.mark.parametrize("change,issue", [
    (lambda rows: rows.pop(0), "parent_observations_missing"),
    (lambda rows: rows[3].update(outputUsage=None), "model_usage_incomplete"),
    (lambda rows: rows[3].update(inputUsage=True), "model_usage_incomplete"),
    (lambda rows: rows[3].update(inputUsage=-1), "model_usage_incomplete"),
    (lambda rows: rows[1].update(traceId="b" * 32), "observation_trace_mismatch"),
    (lambda rows: rows[1].update(projectId="another-project"), "observation_project_mismatch"),
    (lambda rows: rows[1].update(parentObservationId="agent"), "observation_parent_cycle"),
    (lambda rows: rows[1].update(endTime=None), "observations_unfinished"),
    (lambda rows: rows[1].update(level="ERROR"), "observations_contain_errors"),
])
def test_incomplete_trace_cannot_be_pass(change, issue):
    rows = observations()
    change(rows)
    result = audit_trace(rows, TRACE, "project-local", {"api", "agent", "model", "tool"})
    assert result["status"] == "INCOMPLETE" and issue in result["issues"]


def test_remote_error_body_cannot_leak_and_redirect_is_not_followed(service):
    service.status = 302
    report = verify(service.config)
    assert report["error_code"] == "remote_http_302"
    assert SECRET not in json.dumps(report) and len(service.requests) == 1


def test_target_project_must_match(service):
    report = verify(service.config, project_id="other-project", trace_id=TRACE)
    assert report["error_code"] == "target_project_not_accessible"
    assert len(service.requests) == 1


def score():
    return {"id": "score-local", "projectId": "project-local", "name": "feedback_helpful",
            "dataType": "NUMERIC", "value": 1, "subject": {"kind": "trace", "id": TRACE}}


def test_real_score_v3_requires_exact_trace_name_value(service):
    service.scores = [score()]
    result = verify(service.config, trace_id=TRACE, score_id="score-local", score_name="feedback_helpful", score_value=1)
    assert result["status"] == "VERIFIED" and result["score"]["status"] == "VERIFIED"
    path, params, _ = service.requests[-1]
    assert path == "/api/public/v3/scores" and params["fields"] == ["subject"]
    assert params["id"] == ["score-local"] and params["traceId"] == [TRACE]


@pytest.mark.parametrize("changes,issue", [
    ({"subject": {"kind": "trace", "id": "b" * 32}}, "score_trace_subject_mismatch"),
    ({"subject": {"kind": "session", "id": TRACE}}, "score_trace_subject_mismatch"),
    ({"projectId": "another"}, "score_project_mismatch"),
    ({"name": "other"}, "score_name_mismatch"),
    ({"value": True}, "score_numeric_value_mismatch"),
    ({"value": 0}, "score_numeric_value_mismatch"),
])
def test_wrong_score_is_not_verified(changes, issue):
    result = audit_score([{**score(), **changes}], score_id="score-local", trace_id=TRACE,
        project_id="project-local", name="feedback_helpful", value=1)
    assert result["status"] == "INCOMPLETE" and issue in result["issues"]


def test_cli_missing_values_reports_field_names_only(tmp_path, capsys, monkeypatch):
    for name in LANGFUSE_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(langfuse_config, "DEFAULT_ENV_FILE", tmp_path / "absent.env")
    output = tmp_path / "report.json"
    assert main(["--output", str(output)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["missing_fields"] == list(LANGFUSE_FIELDS)
    assert json.loads(output.read_text()) == report
