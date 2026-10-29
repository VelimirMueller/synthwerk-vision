"""Platform conventions: health endpoints, JSON logs and OpenTelemetry (blueprint
docs/conventions/python-service.md §2, §4, §5, §7)."""

import asyncio
import io
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from starlette.requests import Request

from synthwerk_vision.main import create_app
from synthwerk_vision.platform.config import Settings
from synthwerk_vision.platform.health import CheckResult, ReadinessCheck, _run
from synthwerk_vision.platform.log import FORBIDDEN_KEYS, configure_logging
from synthwerk_vision.platform.otel import Telemetry, build_resource, setup_telemetry
from tests.conftest import FakeClassifier, make_image, widget_headers

CONTRACT = yaml.safe_load((Path(__file__).parent / "contracts" / "health.yaml").read_text())
REQUIRED_LOG_FIELDS = {
    "timestamp",
    "level",
    "msg",
    "event.name",
    "service.name",
    "service.version",
    "deployment.environment.name",
}


def contract_errors(schema: str, body: object) -> list[str]:
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{schema}", "components": CONTRACT["components"]}
    )
    return [error.message for error in validator.iter_errors(body)]


def our_stdout_handler() -> logging.StreamHandler[Any]:
    return next(
        h
        for h in logging.getLogger().handlers
        if getattr(h, "synthwerk", False) and isinstance(h, logging.StreamHandler)
    )


def log_lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


class Captured:
    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(self.logs))
        self.telemetry = Telemetry(tracer_provider, MeterProvider(), logger_provider)
        self.stdout = io.StringIO()


@pytest.fixture
def captured(settings: Settings) -> Iterator[tuple[TestClient, Captured]]:
    sink = Captured()
    app = create_app(settings, classifier=FakeClassifier(), telemetry=sink.telemetry)
    our_stdout_handler().setStream(sink.stdout)
    with TestClient(app, headers=widget_headers()) as client:
        yield client, sink


def classify(client: TestClient) -> None:
    response = client.post(
        "/v1/classifications", files={"image": ("a.png", make_image(), "image/png")}
    )
    assert response.status_code == 200


# --- health (§2) -------------------------------------------------------------------------


def test_healthz_process_runs_200_ok(client_without_model: TestClient) -> None:
    response = client_without_model.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert contract_errors("Liveness", response.json()) == []


def test_readyz_startup_not_complete_503_down(settings: Settings) -> None:
    client = TestClient(create_app(settings, classifier=FakeClassifier()))  # no lifespan yet

    response = client.get("/readyz")

    assert (response.status_code, response.json()["status"]) == (503, "down")


def test_readyz_model_loaded_200_ok(client: TestClient) -> None:
    body = client.get("/readyz").json()

    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert body["commit"] == "0000000"


def test_readyz_optional_dependency_down_200_degraded(settings: Settings, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write to every directory")
    data_dir = tmp_path / "data"
    admin = settings.model_copy(update={"admin_token": SecretStr("t"), "data_dir": data_dir})
    with TestClient(create_app(admin, classifier=FakeClassifier())) as client:
        data_dir.chmod(0o500)  # the volume turns read-only after startup
        response = client.get("/readyz")
        data_dir.chmod(0o700)

    assert (response.status_code, response.json()["status"]) == (200, "degraded")
    assert contract_errors("Readiness", response.json()) == []
    assert response.json()["checks"][1] == {
        "name": "data_dir",
        "status": "degraded",
        "latency_ms": response.json()["checks"][1]["latency_ms"],
        "detail": "data dir not writable",
    }


def test_readyz_admin_on_writable_data_dir_200_ok(settings: Settings, tmp_path: Path) -> None:
    admin = settings.model_copy(
        update={"admin_token": SecretStr("t"), "data_dir": tmp_path / "new"}
    )
    with TestClient(create_app(admin, classifier=FakeClassifier())) as client:
        body = client.get("/readyz").json()

    assert [(c["name"], c["status"]) for c in body["checks"]] == [
        ("model", "ok"),
        ("data_dir", "ok"),
    ]


@pytest.mark.parametrize(("client_name", "code"), [("client", 200), ("client_without_model", 503)])
def test_readyz_any_state_body_matches_health_schema(
    request: pytest.FixtureRequest, client_name: str, code: int
) -> None:
    client: TestClient = request.getfixturevalue(client_name)

    response = client.get("/readyz")

    assert response.status_code == code
    assert contract_errors("Readiness", response.json()) == []


@pytest.mark.parametrize(("behaviour", "detail"), [("slow", "timeout"), ("raises", "check failed")])
def test_readiness_check_slow_or_failing_down(behaviour: str, detail: str) -> None:
    async def run(_: Request) -> CheckResult:
        if behaviour == "raises":
            raise OSError("disk gone")
        await asyncio.sleep(1)
        return CheckResult("ok")

    check = asyncio.run(
        _run(ReadinessCheck("disk", run, timeout_s=0.01), Request({"type": "http"}))
    )

    assert (check.status, check.detail) == ("down", detail)


def test_openapi_health_routes_exist_and_old_path_is_gone(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert {"/healthz", "/readyz"} <= paths.keys()
    assert "/health" not in paths
    assert client.get("/health").status_code == 404


# --- logs (§5) ---------------------------------------------------------------------------


def test_log_line_request_completed_required_fields_no_forbidden_keys(
    captured: tuple[TestClient, Captured],
) -> None:
    client, sink = captured

    classify(client)

    line = next(entry for entry in log_lines(sink.stdout) if entry["msg"] == "request completed")
    assert line.keys() >= REQUIRED_LOG_FIELDS
    assert line["event.name"] == "http.server.request"
    assert line["level"] == "INFO"
    assert line["timestamp"].endswith("Z")
    assert line["service.name"] == "synthwerk-vision"
    assert line["http.route"] == "/v1/classifications"
    assert line["http.response.status_code"] == 200
    assert len(line["trace_id"]) == 32
    assert len(line["synthwerk.request_id"]) == 32
    assert not FORBIDDEN_KEYS & line.keys()


def test_log_line_path_with_id_route_template_not_raw_path(
    captured: tuple[TestClient, Captured],
) -> None:
    client, sink = captured

    client.delete("/v1/labels/private-label-id")
    client.get("/no/such/path?token=abc")

    lines = [e for e in log_lines(sink.stdout) if e["msg"] == "request completed"]
    assert lines[0]["http.route"] == "/v1/labels/{label_id}"
    assert "http.route" not in lines[1]
    assert "private-label-id" not in json.dumps(lines)
    assert "token=abc" not in json.dumps(lines)


def test_log_line_unhandled_error_500_with_error_field_and_request_id(
    captured: tuple[TestClient, Captured],
) -> None:
    client, sink = captured

    async def broken() -> None:
        raise RuntimeError("boom")

    client.app.add_api_route("/broken", broken)  # type: ignore[attr-defined]

    response = client.get("/broken", headers={"X-Request-ID": "req-500"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req-500"
    lines = [e for e in log_lines(sink.stdout) if e["msg"] == "request completed"]
    assert len(lines) == 1
    line = lines[0]
    assert line.keys() >= REQUIRED_LOG_FIELDS
    assert line["level"] == "ERROR"
    assert line["event.name"] == "http.server.request"
    assert line["http.request.method"] == "GET"
    assert line["http.route"] == "/broken"
    assert line["http.response.status_code"] == 500
    assert line["error.type"] == "RuntimeError"
    assert line["synthwerk.request_id"] == "req-500"
    assert "synthwerk.duration_ms" in line
    assert "RuntimeError: boom" in line["exception"]


def test_log_line_forbidden_key_passed_dropped(captured: tuple[TestClient, Captured]) -> None:
    _, sink = captured
    forbidden = {key: "leak" for key in FORBIDDEN_KEYS}

    logging.getLogger("synthwerk_vision").info("probe", extra={**forbidden, "kept": 1})

    line = log_lines(sink.stdout)[-1]
    assert line["kept"] == 1
    assert not FORBIDDEN_KEYS & line.keys()
    assert "leak" not in sink.stdout.getvalue()


def test_health_endpoints_called_no_info_lines_and_no_spans(
    captured: tuple[TestClient, Captured],
) -> None:
    client, sink = captured

    client.get("/healthz")
    client.get("/readyz")

    assert [e for e in log_lines(sink.stdout) if e["msg"] == "request completed"] == []
    assert sink.spans.get_finished_spans() == ()


def test_request_traced_one_trace_id_in_span_stdout_and_otel_log(
    captured: tuple[TestClient, Captured],
) -> None:
    client, sink = captured

    classify(client)

    (span,) = sink.spans.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/v1/classifications"
    trace_id = format(span.context.trace_id, "032x")
    line = next(e for e in log_lines(sink.stdout) if e["msg"] == "request completed")
    assert line["trace_id"] == trace_id
    otel = next(
        r.log_record
        for r in sink.logs.get_finished_logs()
        if r.log_record.body == "request completed"
    )
    assert otel.event_name == "http.server.request"
    assert format(otel.trace_id or 0, "032x") == trace_id
    assert otel.attributes is not None
    assert otel.attributes["http.route"] == "/v1/classifications"


def test_uvicorn_access_logger_disabled(client: TestClient) -> None:
    access = logging.getLogger("uvicorn.access")

    assert access.disabled
    assert not access.propagate


def test_library_log_line_any_logger_gets_default_event_name(
    captured: tuple[TestClient, Captured],
) -> None:
    _, sink = captured

    logging.getLogger("uvicorn.error").warning("server shutting down")

    line = log_lines(sink.stdout)[-1]
    assert (line["level"], line["event.name"]) == ("WARN", "uvicorn.error")


@pytest.mark.parametrize(("env", "is_json"), [("local", False), ("prd", True)])
def test_console_format_only_in_local(settings: Settings, env: str, is_json: bool) -> None:
    stream = io.StringIO()
    console = settings.model_copy(update={"log_format": "console", "platform_env": env})
    configure_logging(console, {"service.name": "x"}, stream)

    logging.getLogger("synthwerk_vision").warning("probe")

    first = stream.getvalue().strip()
    assert first.startswith("{") is is_json


# --- OpenTelemetry (§4) ------------------------------------------------------------------


def test_otel_setup_endpoint_not_set_no_exporter_one_warning(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    stream = io.StringIO()
    resource = build_resource(settings)
    configure_logging(settings, {"service.name": "x"}, stream)

    telemetry = setup_telemetry(resource)

    assert telemetry.logger_provider is None
    processors = telemetry.tracer_provider._active_span_processor._span_processors
    assert processors == ()
    (line,) = log_lines(stream)
    assert (line["level"], line["event.name"]) == ("WARN", "otel.export.disabled")
    telemetry.shutdown()


def test_otel_setup_endpoint_set_exporters_for_every_signal(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")

    telemetry = setup_telemetry(build_resource(settings))

    assert telemetry.logger_provider is not None
    assert len(telemetry.tracer_provider._active_span_processor._span_processors) == 1
    assert len(telemetry.meter_provider._metric_readers) == 1
    telemetry.shutdown()


def test_resource_otel_env_set_overrides_defaults(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "vision-canary")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=stg")

    attributes = build_resource(settings).attributes

    assert attributes["service.name"] == "vision-canary"
    assert attributes["deployment.environment.name"] == "stg"
    assert attributes["service.namespace"] == "synthwerk"


# --- settings (§3) -----------------------------------------------------------------------


def test_settings_platform_env_without_service_prefix_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHWERK_PLATFORM_ENV", "stg")
    monkeypatch.setenv("SYNTHWERK_VISION_LOG_LEVEL", "WARN")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert (settings.platform_env, settings.log_level) == ("stg", "WARN")
