---
title: CORS of the widget routes
slug: widget-cors
status: beta
epic: E5
owner: vision
paths: ["src/synthwerk_vision/api/cors.py", "src/synthwerk_vision/api/origin.py"]
api: ["OPTIONS /v1/classifications", "OPTIONS /v1/images", "POST /v1/classifications", "POST /v1/images"]
events: []
mcp: []
updated: 2026-10-30
---

# CORS of the widget routes

> **In 30 seconds**
> - The vision widget runs on customer sites. Vision answers CORS for `POST /v1/classifications` and `POST /v1/images` itself (infra#24).
> - Allowed origin: in `SYNTHWERK_VISION_CORS_ORIGINS` (first-party apps), or identity answers 204 on `GET /internal/v1/widget-origins?origin=<origin>`. Everything else is denied.
> - Same rules as synthwerk-llm (llm#35). CORS only lets the browser read the answer. The widget token and its `synthwerk:origin` binding stay the authorisation.

## What it does

- Only an origin that passes the strict parser (`api/origin.py`, same as identity `CanonicalOrigin`) is looked up. `null`, a path, a wildcard or `http` on a public host is denied without a call.
- Identity answers are cached per canonical origin: yes and no for 60 s, at most 4096 entries. One call per origin at a time.
- An identity error or a timeout (2 s) denies the origin. The error is not cached. The log gets one `WARN` line `vision.cors.lookup_failed`.
- `SYNTHWERK_VISION_IDENTITY_URL` unset: the static list only. In `dev`, `stg` and `prd` the start logs one `WARN` line `vision.cors.not_configured`.
- The identity call sends no token, follows no redirect and uses no proxy. It runs in a worker thread: the event loop does not block.
- `/readyz` does not ask identity. Identity down only narrows CORS to the static list.

## How to use

1. Set `SYNTHWERK_VISION_IDENTITY_URL` to the identity base URL as vision reaches it, for example `http://identity:8080`.
2. Optional: set `SYNTHWERK_VISION_CORS_ORIGINS` to a JSON list of first-party origins, for example `["http://localhost:5173"]`.
3. A customer adds the site origin to the app in identity. Vision allows it within 60 s.

## Rules and limits

| Case | Answer |
|---|---|
| `OPTIONS`, one allowed `Origin`, `Access-Control-Request-Method: POST` | 204, `Access-Control-Allow-Origin: <origin>`, `-Methods: POST`, `-Headers: Authorization, Content-Type`, `-Max-Age: 600`. No token needed. |
| `OPTIONS`, any other case | 403 without CORS headers |
| `OPTIONS`, both answers | `Vary: Origin`, `Cache-Control: no-store` |
| `POST`, every answer | `Vary: Origin` |
| `POST`, allowed origin | `Access-Control-Allow-Origin: <origin>`, `Access-Control-Expose-Headers: X-Request-ID`, also on 401, 403, 413, 415, 503 and 507 |
| Credentials | Never `Access-Control-Allow-Credentials`: the widget sends a Bearer token, no cookie |
| Other routes | No CORS. Health is not called from a browser. The label admin routes are server-to-server. |
| Static list | JSON list. Each item is `scheme://host[:port]`, `http` only for localhost and loopback. An invalid item (also `*`) stops the start. |
| Origin value | At most 256 characters. ASCII only. No path, query, fragment, user info, wildcard or `%`. |

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 403 | None (empty body) | Preflight from an origin that is not allowed, a second `Origin`, or another method. |

## Tests

- `tests/test_cors.py` — `canonical_origin - valid value - canonical form`; `invalid value - raises` (`null`, path, wildcard, `http` on a public host, leading-zero port, zone, and more)
- `tests/test_cors.py` — `settings - invalid CORS origin - stops the start`; `identity URL - invalid - stops the start`
- `tests/test_cors.py` — `policy - yes and no - cached for the TTL, asked again after it`; `identity fails - denied, not cached`; `timeout - denied, not cached`; `300 origins, bound 100 - cache never above 100`; `50 parallel requests for one new origin - one lookup`
- `tests/test_cors.py` — `preflight - listed or static origin, no token - 204` (both routes); `denied - 403 without CORS headers`; `preflight then POST - one identity call`
- `tests/test_cors.py` — `POST - no token, listed origin - 401 the widget can read`; `403` and `413` with CORS; `unknown origin - Vary only`
- `tests/test_cors.py` — `health and admin routes - no CORS`
- `tests/test_cors.py` — `identity client - 204 yes, escaped origin, no token`; `404 no`; `200, 302, 400, 500, unreachable - error`

## Changes

| Date | Change |
|---|---|
| 2026-10-30 | First version (infra#24): `SYNTHWERK_VISION_IDENTITY_URL`, identity lookup `GET /internal/v1/widget-origins`, `OPTIONS` without token. The global CORS middleware is removed. |
