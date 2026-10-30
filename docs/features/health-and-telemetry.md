---
title: Health and telemetry
slug: health-and-telemetry
status: beta
epic: E1
owner: vision
paths: ["src/synthwerk_vision/platform/__init__.py", "src/synthwerk_vision/platform/health.py", "src/synthwerk_vision/platform/log.py", "src/synthwerk_vision/platform/otel.py", "src/synthwerk_vision/main.py"]
api: ["GET /healthz", "GET /readyz"]
events: []
mcp: []
updated: 2026-10-30
---

# Health and telemetry

> **In 30 seconds**
> - `/healthz` says the process runs. `/readyz` says the service can classify.
> - Logs are JSON lines on stdout with a `trace_id` and the request id.
> - With `OTEL_EXPORTER_OTLP_ENDPOINT` set, traces, metrics and logs go to the collector.

## What it does

- `GET /healthz` returns 200 `{"status":"ok"}`. It checks no dependency.
- `GET /readyz` runs the readiness checks and returns `{status, version, commit, checks}`.
  - `model`: `down` until the model is loaded.
  - `data_dir`: only when the admin token is set. `degraded` when the data directory is not writable.
  - `storage`: only with `SYNTHWERK_VISION_STORAGE_BACKEND=s3`. `degraded` when the upload bucket does not answer, is missing or refuses the credentials.
  - The overall `status` is the worst check. `ok` and `degraded` return 200, `down` returns 503.
- The body follows the health contract `openapi/_shared/health.yaml` in synthwerk-contracts.
- Every log record is one JSON object on stdout. uvicorn and library loggers use the same handler.
- Each request writes one `request completed` line with the route template, never the raw path.
- A request that ends in an unhandled error also writes this line. The line has level `ERROR`, status 500, `error.type` and the traceback in `exception`.
- The service then answers 500 with the `X-Request-ID` header.
- The service drops forbidden keys (`token`, `authorization`, `image`, `body` and others) from every log record.
- The FastAPI instrumentation makes one span per request. `/healthz` and `/readyz` make no span and no `INFO` line.
- Without `OTEL_EXPORTER_OTLP_ENDPOINT`, no exporter starts. The service writes one `WARN` line.

## How to use

1. Point the container health check at `/healthz` (the image does this).
2. Point the deploy smoke test and pulse at `/readyz`.
3. Set `OTEL_EXPORTER_OTLP_ENDPOINT` (for example `http://otel-collector:4317`) to export telemetry.
4. Search the logs by `trace_id` or `synthwerk.request_id` to follow one request.

## Rules and limits

| Rule | Value |
|---|---|
| Readiness check timeout | 0.25 s per check (`storage`: 0.4 s for the bucket, then `degraded`; 0.45 s for the check), checks run at the same time |
| `detail` | Constant text. No secrets, paths or user data. |
| Log level | `SYNTHWERK_VISION_LOG_LEVEL`, default `INFO` |
| Log format | `SYNTHWERK_VISION_LOG_FORMAT`, `json` (default) or `console` (only when `SYNTHWERK_PLATFORM_ENV=local`) |
| `commit` | `SYNTHWERK_VISION_COMMIT`, set by the image build argument `COMMIT`, default `0000000` |
| OTLP protocol | gRPC |
| HTTP metric | `http.server.request.duration` (stable HTTP semantic conventions) |

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 503 | None (health body with `status: down`) | Startup is not complete, or the model is not loaded. |

## Tests

- `tests/test_platform.py` — `healthz - process runs - 200 ok`
- `tests/test_platform.py` — `readyz - startup not complete - 503 down`
- `tests/test_platform.py` — `readyz - optional dependency down - 200 degraded`
- `tests/test_platform.py` — `readyz - any state - body matches health schema`
- `tests/test_platform.py` — `log line - request completed - required fields, no forbidden keys`
- `tests/test_platform.py` — `log line - path with id - route template, not raw path`
- `tests/test_platform.py` — `log line - unhandled error - 500 with error field and request id`
- `tests/test_platform.py` — `health endpoints - called - no INFO lines and no spans`
- `tests/test_platform.py` — `request - traced - one trace_id in span, stdout and OTel log`
- `tests/test_platform.py` — `otel setup - endpoint not set - no exporter, one warning`
- `tests/test_platform.py` — `settings - platform env without service prefix - read`
- `tests/test_storage.py` — `readyz - s3 backend - storage ok, degraded when the bucket is missing`
- `tests/test_storage.py` — `readyz - slow bucket - degraded, not down`
- `tests/test_storage.py` — `readyz - local backend - no storage check`

## Changes

| Date | Change |
|---|---|
| 2026-10-21 | New. `/healthz` and `/readyz` replace `/health`. JSON logs and OpenTelemetry (#23). |
| 2026-10-29 | A request that ends in an unhandled error gets its log line and an `X-Request-ID` (#27). |
| 2026-10-29 | `main.py` gives the upload storage its quota. Health and telemetry do not change (#19). |
| 2026-10-29 | `main.py` checks the widget token settings before telemetry starts. A bad token setting stops the start. Health and telemetry do not change (#76). |
| 2026-10-30 | `main.py` moves `labels.yaml` to the default label set at startup and keeps the org label sets. Health and telemetry do not change (synthwerk#44). |
| 2026-10-30 | New readiness check `storage` for the S3 upload bucket. `main.py` builds the storage from the settings; a bad storage setting stops the start (#77). |
