"""Widget token verification (synthwerk#76).

A Python copy of the token check in the synthwerk-contracts Go kit (`go/kit/auth`), for the
widget token that synthwerk-identity issues (synthwerk#30):

- JWT, header `alg` ES256 only, `kid` required, `typ` an access token type.
- `iss` equals the configured issuer, `aud` holds one configured audience.
- `exp` and `iat` required; `exp`, `nbf` and `iat` checked with a small leeway.
- Lifetime (`exp` - `iat`) at most 15 minutes plus the leeway.
- Zitadel claim names: `sub`, `urn:zitadel:iam:user:resourceowner:id`, `client_id` (the app
  id) and `synthwerk:origin` are all required. A token without an origin is not a widget token.
- An ID token claim (`nonce`, `at_hash`) refuses the token.

The key set (JWKS) is cached. An unknown `kid` or a key set older than `max_age` triggers one
refresh, at most once per `refresh_interval`. When a refresh fails, a known key stays usable.

The origin binding (the `Origin` header equals `synthwerk:origin`) is a rule of the HTTP layer:
see `synthwerk_vision.api.auth`.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

# Claim names, as in kit/auth zitadel.go.
CLAIM_ORG_ID = "urn:zitadel:iam:user:resourceowner:id"
CLAIM_CLIENT_ID = "client_id"
CLAIM_ORIGIN = "synthwerk:origin"

ALGORITHM = "ES256"
DEFAULT_LEEWAY_S = 30.0
MAX_LEEWAY_S = 60.0
MAX_LIFETIME_S = 15 * 60
DEFAULT_MAX_AGE_S = 15 * 60.0
DEFAULT_REFRESH_INTERVAL_S = 10.0
FETCH_TIMEOUT_S = 5.0

_MAX_TOKEN_LEN = 16 << 10
_MAX_JWKS_BYTES = 1 << 20
# Header typ of an access token (RFC 9068); Zitadel sends "JWT". Compared without case and
# without the "application/" prefix.
_ACCEPTED_TYPES = frozenset({"at+jwt", "jwt"})
_ID_TOKEN_CLAIMS = ("nonce", "at_hash")

JwksFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class AuthConfigError(ValueError):
    """The token settings are missing or wrong: the service must not start."""


class Unauthenticated(Exception):
    """The token is refused: answer 401. `reason` is constant text, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeysUnavailable(Exception):
    """The key set cannot be loaded: answer 503, not 401."""


@dataclass(frozen=True)
class WidgetPrincipal:
    """Caller of a request, taken from a verified widget token."""

    subject: str
    org_id: str
    app_id: str
    origin: str


async def fetch_jwks(url: str) -> dict[str, Any]:
    """GET the key set. Runs the blocking request in a thread, bounded in time and size."""

    def get() -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 (scheme checked in TokenVerifier)
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310
            if response.status != 200:
                raise OSError(f"jwks: status {response.status}")
            body = response.read(_MAX_JWKS_BYTES + 1)
        if len(body) > _MAX_JWKS_BYTES:
            raise OSError("jwks: body too large")
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError("jwks: not a JSON object")
        return document

    return await asyncio.to_thread(get)


def _usable_keys(document: dict[str, Any]) -> dict[str, jwt.PyJWK]:
    """ES256 public signing keys by kid. Keys this verifier cannot use are skipped one by one."""
    keys: dict[str, jwt.PyJWK] = {}
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list):
        return keys
    for raw in raw_keys:
        if not isinstance(raw, dict):
            continue
        kid = raw.get("kid")
        if (
            not isinstance(kid, str)
            or not kid
            or raw.get("kty") != "EC"
            or raw.get("crv") != "P-256"
            or "d" in raw  # a private key never belongs in a key set
            or raw.get("use", "sig") != "sig"
            or raw.get("alg", ALGORITHM) != ALGORITHM
        ):
            continue
        try:
            key = jwt.PyJWK(raw, ALGORITHM)
        except (jwt.PyJWTError, ValueError):
            continue
        if isinstance(key.key, ec.EllipticCurvePublicKey) and isinstance(
            key.key.curve, ec.SECP256R1
        ):
            keys[kid] = key
    return keys


@dataclass
class _KeySet:
    url: str
    fetch: JwksFetcher
    max_age: float
    refresh_interval: float
    keys: dict[str, jwt.PyJWK] = field(default_factory=dict)
    fetched: float | None = None
    last_attempt: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _fresh(self) -> bool:
        return self.fetched is not None and time.monotonic() - self.fetched < self.max_age

    async def get(self, kid: str) -> jwt.PyJWK:
        key = self.keys.get(kid)
        if key is not None and self._fresh():
            return key
        try:
            await self._refresh()
        except KeysUnavailable:
            if key is None:
                raise
        key = self.keys.get(kid)
        if key is not None:
            return key
        if not self.keys:
            raise KeysUnavailable("no key loaded")
        raise Unauthenticated("unknown key id")

    async def _refresh(self) -> None:
        async with self.lock:  # one fetch at a time; waiters reuse its result
            now = time.monotonic()
            if self.last_attempt is not None and now - self.last_attempt < self.refresh_interval:
                return
            self.last_attempt = now
            try:
                document = await self.fetch(self.url)
            except Exception as exc:
                raise KeysUnavailable("key set fetch failed") from exc
            keys = _usable_keys(document)
            if not keys:
                raise KeysUnavailable("no usable ES256 signing key")
            self.keys, self.fetched = keys, time.monotonic()


class TokenVerifier:
    """Checks widget tokens. Safe for concurrent use inside one event loop."""

    def __init__(
        self,
        *,
        issuer: str,
        audiences: Sequence[str],
        jwks_url: str,
        leeway: float = DEFAULT_LEEWAY_S,
        max_age: float = DEFAULT_MAX_AGE_S,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL_S,
        fetch: JwksFetcher | None = None,
    ) -> None:
        errors = []
        if not _absolute(issuer, {"http", "https"}):
            errors.append("issuer is not an absolute URL")
        if not _absolute(jwks_url, {"http", "https"}):
            errors.append("JWKS URL is not an absolute http(s) URL")
        if not audiences or any(not a for a in audiences):
            errors.append("at least one non-empty audience is required")
        if not 0 <= leeway <= MAX_LEEWAY_S:
            errors.append(f"leeway must be between 0 and {MAX_LEEWAY_S:g} s")
        if errors:
            raise AuthConfigError("; ".join(errors))
        self.issuer = issuer
        self.audiences = list(audiences)
        self.leeway = leeway
        self._keys = _KeySet(jwks_url, fetch or fetch_jwks, max_age, refresh_interval)

    async def verify(self, raw: str) -> WidgetPrincipal:
        """Check raw (the token without "Bearer "). Raises Unauthenticated or KeysUnavailable."""
        if not raw or len(raw) > _MAX_TOKEN_LEN:
            raise Unauthenticated("malformed token")
        try:
            header = jwt.get_unverified_header(raw)
        except jwt.PyJWTError as exc:
            raise Unauthenticated("malformed token") from exc
        if header.get("alg") != ALGORITHM:
            raise Unauthenticated("algorithm not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise Unauthenticated("malformed token")
        typ = header.get("typ", "")
        if not isinstance(typ, str) or _norm_type(typ) not in _ACCEPTED_TYPES:
            raise Unauthenticated("not an access token")

        key = await self._keys.get(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                raw,
                key,
                algorithms=[ALGORITHM],
                audience=self.audiences,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise Unauthenticated("token expired") from exc
        except (jwt.ImmatureSignatureError, jwt.InvalidIssuedAtError) as exc:
            raise Unauthenticated("token not yet valid") from exc
        except jwt.InvalidIssuerError as exc:
            raise Unauthenticated("wrong issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise Unauthenticated("wrong audience") from exc
        except jwt.InvalidSignatureError as exc:
            raise Unauthenticated("bad signature") from exc
        except jwt.PyJWTError as exc:
            raise Unauthenticated("missing or bad claims") from exc

        if any(name in claims for name in _ID_TOKEN_CLAIMS):
            raise Unauthenticated("not an access token")
        if int(claims["exp"]) - int(claims["iat"]) > MAX_LIFETIME_S + self.leeway:
            raise Unauthenticated("lifetime too long")
        values = [claims.get(n) for n in ("sub", CLAIM_ORG_ID, CLAIM_CLIENT_ID, CLAIM_ORIGIN)]
        if not all(isinstance(v, str) and v for v in values):
            raise Unauthenticated("missing or bad claims")
        sub, org, app, origin = (str(v) for v in values)
        return WidgetPrincipal(subject=sub, org_id=org, app_id=app, origin=origin)


def _absolute(url: str, schemes: set[str]) -> bool:
    parts = urlsplit(url)
    return parts.scheme in schemes and bool(parts.netloc)


def _norm_type(typ: str) -> str:
    typ = typ.strip().lower()
    return typ.removeprefix("application/")
