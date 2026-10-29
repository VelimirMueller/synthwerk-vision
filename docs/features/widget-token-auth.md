---
title: Widget token on classification and upload
slug: widget-token-auth
status: beta
epic: E5
owner: vision
paths: ["src/synthwerk_vision/api/auth.py", "src/synthwerk_vision/platform/auth.py"]
api: ["POST /v1/classifications", "POST /v1/images"]
events: []
mcp: []
updated: 2026-10-29
---

# Widget token on classification and upload

> **In 30 seconds**
> - `POST /v1/classifications` and `POST /v1/images` need a widget token (synthwerk#76).
> - synthwerk-identity issues the token for one app and one origin (synthwerk#30).
> - No valid token: 401. Token for another origin: 403.

## What it does

- The service reads `Authorization: Bearer <token>` and checks the token like the Go kit `auth.Verifier` (synthwerk-contracts `go/kit/auth`).
- The service gets the public keys from `SYNTHWERK_VISION_JWKS_URL` (identity `/.well-known/jwks.json`) and keeps them in memory.
- An unknown `kid` makes the service load the keys again, at most once per 10 s. When a reload fails, a known key stays usable.
- The browser `Origin` header must equal the `synthwerk:origin` claim, as in synthwerk-llm.
- The check runs before the route reads the model or writes a file.
- The service does not start without `SYNTHWERK_VISION_OIDC_ISSUER` and `SYNTHWERK_VISION_JWKS_URL`.

## How to use

1. Set `SYNTHWERK_VISION_OIDC_ISSUER` to the widget issuer of identity (`SYNTHWERK_IDENTITY_WIDGET_ISSUER`).
2. Set `SYNTHWERK_VISION_JWKS_URL` to `<identity>/.well-known/jwks.json`.
3. Optional: set `SYNTHWERK_VISION_OIDC_AUDIENCE` (comma-separated, default `synthwerk-api`). It must contain the `aud` of the widget token.
4. The widget gets a token from `POST /v1/apps/{appId}/widget-tokens` and sends it on each call.
5. Local development without identity: set `SYNTHWERK_VISION_AUTH_DISABLED=true`. The service logs one `WARN` line at start.

## Rules and limits

| Rule | Value |
|---|---|
| Algorithm | `ES256` only. `none`, `HS*` and `RS256` are refused. |
| Header | `kid` required. `typ` `at+jwt` or `JWT` (no case, `application/` prefix allowed). |
| `iss` | Equals `SYNTHWERK_VISION_OIDC_ISSUER` |
| `aud` | Contains one value of `SYNTHWERK_VISION_OIDC_AUDIENCE` |
| `exp`, `iat` | Required. `exp`, `nbf` and `iat` checked with 30 s clock skew. |
| Lifetime | `exp` - `iat` at most 15 min (+ 30 s) |
| Required claims | `sub`, `urn:zitadel:iam:user:resourceowner:id`, `client_id` (app id), `synthwerk:origin` |
| Refused claims | `nonce`, `at_hash` (ID token) |
| Origin | Exactly one `Origin` header, equal to `synthwerk:origin` (exact string compare) |
| Zitadel user token | Refused: other issuer, `RS256`, no `synthwerk:origin` |
| Token size | At most 16 KiB |
| Key set | ES256 P-256 public signing keys only. Cached 15 min, reload at most once per 10 s. 5 s fetch timeout, 1 MiB body limit. |
| `SYNTHWERK_VISION_AUTH_DISABLED` | Allowed only when `SYNTHWERK_PLATFORM_ENV` is `local` or `ci`. Else the service does not start. |
| Log | Refusal reason as constant text in `synthwerk.auth.reason`. Never the token. |

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 401 | None (FastAPI `detail`) | No token, or the token is not a valid widget token. `WWW-Authenticate: Bearer`. |
| 403 | None (FastAPI `detail`) | `Origin` is missing, sent twice, or not the origin of the token. |
| 503 | None (FastAPI `detail`) | The key set cannot be loaded and no key is known. |

## Tests

- `tests/test_auth.py` — `endpoint - without token - 401` (both endpoints)
- `tests/test_auth.py` — `endpoint - with valid widget token - succeeds` (both endpoints)
- `tests/test_auth.py` — `endpoint - with other origin - 403` (both endpoints)
- `tests/test_auth.py` — `classification - refused token - 401` (expired, other issuer, other audience, bad signature, RS256, HS256, lifetime 1 h, user token without origin, and more)
- `tests/test_auth.py` — `unknown kid - new key published - refresh and 200`
- `tests/test_auth.py` — `key set - unavailable - 503`
- `tests/test_auth.py` — `start - without token settings - refused`
- `tests/test_auth.py` — `auth disabled - outside local - refused`

## Changes

| Date | Change |
|---|---|
| 2026-10-29 | First version (synthwerk#76). |
