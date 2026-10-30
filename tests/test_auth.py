"""Widget token on the classification and image routes (synthwerk#76)."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from synthwerk_vision.main import create_app
from synthwerk_vision.platform import auth
from synthwerk_vision.platform.auth import AuthConfigError, TokenVerifier
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.storage import LocalImageStorage
from tests.conftest import (
    AUDIENCE,
    ISSUER,
    JWKS,
    JWKS_URL,
    ORIGIN,
    FakeClassifier,
    make_image,
    public_jwk,
    widget_headers,
    widget_token,
)

REAL_FETCH = auth.fetch_jwks  # bound at import, before the conftest fixture replaces it
ENDPOINTS = [("/v1/classifications", 200), ("/v1/images", 201)]


@pytest.fixture
def bare(settings: Settings) -> Iterator[TestClient]:
    """A client that sends no token and no Origin unless the test does."""
    app = create_app(
        settings,
        storage=LocalImageStorage(
            settings.upload_dir,
            max_bytes=settings.upload_quota_bytes,
            max_files=settings.upload_quota_files,
            max_total_bytes=settings.upload_quota_total_bytes,
        ),
        classifier=FakeClassifier(),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client


def post(client: TestClient, path: str, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        path, files={"image": ("a.png", make_image(), "image/png")}, headers=headers or {}
    )


# --- both endpoints, with and without a token ------------------------------------------


@pytest.mark.parametrize(("path", "ok"), ENDPOINTS)
def test_endpoint_without_token_401(bare: TestClient, path: str, ok: int) -> None:
    response = post(bare, path, {"Origin": ORIGIN})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "a valid widget token is required"}


@pytest.mark.parametrize(("path", "ok"), ENDPOINTS)
def test_endpoint_with_valid_widget_token_succeeds(bare: TestClient, path: str, ok: int) -> None:
    assert post(bare, path, widget_headers()).status_code == ok


@pytest.mark.parametrize(("path", "ok"), ENDPOINTS)
def test_endpoint_with_bad_token_401_invalid_token(bare: TestClient, path: str, ok: int) -> None:
    response = post(bare, path, widget_headers("not.a.jwt"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert "not.a.jwt" not in response.text


@pytest.mark.parametrize(("path", "ok"), ENDPOINTS)
def test_endpoint_with_other_origin_403(bare: TestClient, path: str, ok: int) -> None:
    headers = {**widget_headers(), "Origin": "https://evil.example"}

    assert post(bare, path, headers).status_code == 403


# --- token rules ----------------------------------------------------------------------

OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(widget_token(issued=time.time() - 3600), id="expired"),
        pytest.param(widget_token(issued=time.time() + 3600), id="issued in the future"),
        pytest.param(widget_token(nbf=int(time.time()) + 3600), id="not yet valid"),
        pytest.param(widget_token(iss="https://zitadel.test"), id="other issuer"),
        pytest.param(widget_token(aud=["other-api"]), id="other audience"),
        pytest.param(widget_token(key=OTHER_KEY), id="bad signature"),
        pytest.param(widget_token(key=RSA_KEY, algorithm="RS256"), id="RS256"),
        pytest.param(widget_token(key="s" * 32, algorithm="HS256"), id="HS256"),
        pytest.param(widget_token(typ="JWT+id"), id="other typ"),
        pytest.param(widget_token(kid=""), id="no kid"),
        pytest.param(widget_token(lifetime=3600), id="lifetime 1h"),
        pytest.param(widget_token(nonce="n"), id="ID token claim"),
        pytest.param(widget_token(drop=("exp",)), id="no exp"),
        pytest.param(widget_token(drop=(auth.CLAIM_ORIGIN,)), id="user token, no origin"),
        pytest.param(widget_token(drop=(auth.CLAIM_ORG_ID,)), id="no org"),
        pytest.param(widget_token(drop=(auth.CLAIM_CLIENT_ID,)), id="no app id"),
        pytest.param(widget_token(sub=""), id="empty sub"),
        pytest.param("x" * (17 << 10), id="too long"),
    ],
)
def test_classification_refused_token_401(bare: TestClient, token: str) -> None:
    assert post(bare, "/v1/classifications", widget_headers(token)).status_code == 401


def test_classification_token_within_leeway_accepted(bare: TestClient) -> None:
    token = widget_token(issued=time.time() - 610)  # expired 10 s ago, leeway 30 s

    assert post(bare, "/v1/classifications", widget_headers(token)).status_code == 200


def test_classification_typ_jwt_accepted(bare: TestClient) -> None:
    token = widget_token(typ="application/AT+JWT")

    assert post(bare, "/v1/classifications", widget_headers(token)).status_code == 200


def test_classification_without_origin_header_403(bare: TestClient) -> None:
    headers = {"Authorization": f"Bearer {widget_token()}"}

    assert post(bare, "/v1/classifications", headers).status_code == 403


# --- key set --------------------------------------------------------------------------


def test_unknown_kid_new_key_published_refresh_and_200(
    bare: TestClient, widget_auth: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert post(bare, "/v1/classifications", widget_headers()).status_code == 200  # cache warm
    verifier: TokenVerifier = bare.app.state.verifier  # type: ignore[attr-defined]
    monkeypatch.setattr(verifier._keys, "refresh_interval", 0)  # else one refresh per 10 s
    widget_auth["keys"].append(public_jwk(OTHER_KEY, "rotated"))
    token = widget_token(key=OTHER_KEY, kid="rotated")

    assert post(bare, "/v1/classifications", widget_headers(token)).status_code == 200


def test_unknown_kid_not_published_401(bare: TestClient) -> None:
    token = widget_token(key=OTHER_KEY, kid="never-published")

    assert post(bare, "/v1/classifications", widget_headers(token)).status_code == 401


def test_key_set_unavailable_503(bare: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier: TokenVerifier = bare.app.state.verifier  # type: ignore[attr-defined]

    async def down(url: str) -> dict[str, Any]:
        raise OSError("connection refused")

    monkeypatch.setattr(verifier._keys, "fetch", down)

    assert post(bare, "/v1/classifications", widget_headers()).status_code == 503


def test_key_set_skips_unusable_keys() -> None:
    good = JWKS["keys"][0]
    rsa_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(RSA_KEY.public_key()))
    p384 = public_jwk(ec.generate_private_key(ec.SECP384R1()), "p384")
    keys = auth._usable_keys(
        {
            "keys": [
                good,
                {**rsa_jwk, "kid": "rsa"},
                {**p384, "crv": "P-384"},
                {**good, "kid": "enc", "use": "enc"},
                {**good, "kid": "hs", "alg": "HS256"},
                {**good, "kid": "private", "d": "x"},
                {**good, "kid": ""},
                {**good, "kid": "broken", "x": "!!"},
                "not a key",
            ]
        }
    )

    assert list(keys) == ["test-key"]
    assert auth._usable_keys({"keys": "nope"}) == {}


def test_known_key_survives_failed_refresh() -> None:
    calls = 0

    async def flaky(url: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("down")
        return JWKS

    verifier = TokenVerifier(
        issuer=ISSUER,
        audiences=[AUDIENCE],
        jwks_url=JWKS_URL,
        max_age=0,
        refresh_interval=0,
        fetch=flaky,
    )

    async def twice() -> None:
        await verifier.verify(widget_token())
        await verifier.verify(widget_token())  # key set stale, refresh fails, key still known

    asyncio.run(twice())
    assert calls == 2


def test_key_set_without_usable_key_503(bare: TestClient, widget_auth: dict[str, Any]) -> None:
    widget_auth["keys"].clear()

    assert post(bare, "/v1/classifications", widget_headers()).status_code == 503


def test_fetch_jwks_reads_local_server() -> None:
    body = json.dumps(JWKS).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = 200 if self.path == "/jwks.json" else 404
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body if status == 200 else b"")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert asyncio.run(REAL_FETCH(f"{base}/jwks.json")) == JWKS
        with pytest.raises(OSError):
            asyncio.run(REAL_FETCH(f"{base}/missing"))
    finally:
        server.shutdown()


# --- start-up -------------------------------------------------------------------------


@pytest.mark.parametrize("unset", ["SYNTHWERK_VISION_OIDC_ISSUER", "SYNTHWERK_VISION_JWKS_URL"])
def test_start_without_token_settings_refused(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    monkeypatch.delenv(unset)

    with pytest.raises(AuthConfigError, match=unset):
        create_app(Settings(_env_file=None))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("issuer", "jwks_url", "audience"),
    [
        ("identity", JWKS_URL, AUDIENCE),
        (ISSUER, "file:///etc/jwks.json", AUDIENCE),
        (ISSUER, JWKS_URL, " , "),
    ],
)
def test_start_with_bad_token_settings_refused(
    settings: Settings, issuer: str, jwks_url: str, audience: str
) -> None:
    bad = settings.model_copy(
        update={"oidc_issuer": issuer, "jwks_url": jwks_url, "oidc_audience": audience}
    )

    with pytest.raises(AuthConfigError):
        create_app(bad)


def test_verifier_leeway_over_60s_refused() -> None:
    with pytest.raises(AuthConfigError, match="leeway"):
        TokenVerifier(issuer=ISSUER, audiences=[AUDIENCE], jwks_url=JWKS_URL, leeway=61)


def test_auth_disabled_locally_no_token_needed(settings: Settings) -> None:
    local = settings.model_copy(update={"auth_disabled": True, "oidc_issuer": None})
    app = create_app(
        local,
        storage=LocalImageStorage(
            local.upload_dir,
            max_bytes=local.upload_quota_bytes,
            max_files=local.upload_quota_files,
            max_total_bytes=local.upload_quota_total_bytes,
        ),
        classifier=FakeClassifier(),
    )
    with TestClient(app) as client:
        assert post(client, "/v1/images").status_code == 201


@pytest.mark.parametrize("env", ["dev", "stg", "prd"])
def test_auth_disabled_outside_local_refused(settings: Settings, env: str) -> None:
    deployed = settings.model_copy(update={"auth_disabled": True, "platform_env": env})

    with pytest.raises(AuthConfigError, match="not allowed"):
        create_app(deployed)
