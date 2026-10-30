"""CORS of the widget routes (infra#24): strict origin parser, cached identity lookup, the
middleware on POST /v1/classifications and POST /v1/images, and the identity client."""

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from synthwerk_vision.api.cors import (
    WIDGET_ORIGINS_PATH,
    IdentityOrigins,
    OriginLookupError,
    OriginPolicy,
)
from synthwerk_vision.api.origin import OriginError, canonical_origin
from synthwerk_vision.main import create_app
from synthwerk_vision.platform import auth
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.storage import LocalImageStorage
from tests.conftest import FakeClassifier, make_image, widget_token


def _storage(settings: Settings) -> LocalImageStorage:
    return LocalImageStorage(
        settings.upload_dir,
        max_bytes=settings.upload_quota_bytes,
        max_files=settings.upload_quota_files,
        max_total_bytes=settings.upload_quota_total_bytes,
    )


SHOP = "https://shop.example"  # a customer origin that an app lists in identity
FIRST_PARTY = "http://localhost:5173"  # in the static list (default settings)
CORS_HEADERS = (
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-max-age",
    "access-control-expose-headers",
    "access-control-allow-credentials",
)


def shop_token() -> str:
    """A widget token bound to the customer origin."""
    claims: dict[str, Any] = {auth.CLAIM_ORIGIN: SHOP}
    return widget_token(**claims)


# ---- strict origin parser (parity with identity CanonicalOrigin) ----


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("https://shop.example", "https://shop.example"),
        ("HTTPS://Shop.Example:443", "https://shop.example"),
        ("https://shop.example:8443", "https://shop.example:8443"),
        ("http://localhost:80", "http://localhost"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:5173", "http://127.0.0.1:5173"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("https://[2001:DB8:0:0::1]", "https://[2001:db8::1]"),
        ("https://192.168.0.1", "https://192.168.0.1"),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
    ],
)
def test_canonical_origin_valid_value_returns_canonical_form(value: str, canonical: str) -> None:
    assert canonical_origin(value) == canonical


@pytest.mark.parametrize(
    "value",
    [
        "",
        "null",
        "*",
        "https://*.example",
        "https://shop.example/",
        "https://shop.example/path",
        "https://shop.example?x=1",
        "https://shop.example#x",
        "https://user@shop.example",
        "https://shop%2eexample",
        "https://shop.example\\",
        "https://shop .example",
        "https://shop.example\r\nX: y",
        "https://bücher.example",
        "https://shop.example.",
        "https://-shop.example",
        "https://shop_x.example",
        "https://shop.example:0",
        "https://shop.example:080",
        "https://shop.example:65536",
        "https://shop.example:",
        "https://shop.example:+80",
        "https://shop.example:1_0",
        "https://a:b:c",
        "http://shop.example",
        "http://192.168.0.1",
        "ftp://shop.example",
        "shop.example",
        "https://",
        "https://:443",
        "https://010.0.0.1",
        "https://1.2.3",
        "https://[::1",
        "https://[::1]x",
        "https://[fe80::1%25eth0]",
        "https://[1.2.3.4]",
        "https://" + "a" * 64 + ".example",
        "https://" + "a." * 127 + "example",
        "https://" + "a" * 260,
    ],
)
def test_canonical_origin_invalid_value_raises(value: str) -> None:
    with pytest.raises(OriginError):
        canonical_origin(value)


# ---- settings ----


def test_settings_cors_origins_canonical_and_deduplicated() -> None:
    settings = Settings(cors_origins=["HTTP://LOCALHOST:5173", "http://localhost:5173"])

    assert settings.cors_origins == ["http://localhost:5173"]


@pytest.mark.parametrize("item", ["*", "https://shop.example/", "http://shop.example"])
def test_settings_invalid_cors_origin_stops_the_start(item: str) -> None:
    with pytest.raises(ValidationError, match="item 1 is not an origin"):
        Settings(cors_origins=[FIRST_PARTY, item])


@pytest.mark.parametrize(
    ("value", "stored"),
    [(None, None), ("  ", None), ("http://identity:8080/", "http://identity:8080")],
)
def test_settings_identity_url_valid(value: str | None, stored: str | None) -> None:
    assert Settings(identity_url=value).identity_url == stored


@pytest.mark.parametrize(
    "value",
    [
        "identity:8080",
        "ftp://identity",
        "http://identity?x=1",
        "http://identity#x",
        "http://u:p@identity",
    ],
)
def test_settings_identity_url_invalid_stops_the_start(value: str) -> None:
    with pytest.raises(ValidationError, match="http"):
        Settings(identity_url=value)


# ---- policy: cache, timeout, errors ----


class FakeIdentity:
    """Answers like GET /internal/v1/widget-origins: listed origins are yes."""

    def __init__(self, listed: set[str] | None = None, delay: float = 0.0) -> None:
        self.listed = listed if listed is not None else {SHOP}
        self.delay = delay
        self.calls: list[str] = []
        self.fail = False

    async def __call__(self, origin: str) -> bool:
        self.calls.append(origin)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise OriginLookupError("status 500")
        return origin in self.listed


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_policy_invalid_origin_denied_without_lookup() -> None:
    identity = FakeIdentity()
    policy = OriginPolicy([], identity)

    assert not asyncio.run(policy.allowed("null"))
    assert identity.calls == []
    assert len(policy) == 0


def test_policy_static_origin_allowed_without_lookup() -> None:
    identity = FakeIdentity()
    policy = OriginPolicy([FIRST_PARTY], identity)

    assert asyncio.run(policy.allowed("HTTP://localhost:5173"))
    assert identity.calls == []


def test_policy_non_canonical_static_entry_still_matches() -> None:
    policy = OriginPolicy(["HTTPS://Shop.Example:443", "not an origin"])

    assert asyncio.run(policy.allowed(SHOP))


def test_policy_without_lookup_static_list_only() -> None:
    assert not asyncio.run(OriginPolicy([FIRST_PARTY]).allowed(SHOP))


def test_policy_yes_and_no_cached_for_ttl_then_asked_again() -> None:
    identity = FakeIdentity()
    clock = Clock()
    policy = OriginPolicy([], identity, ttl_s=60, clock=clock)

    async def run() -> list[bool]:
        answers = [await policy.allowed(o) for o in (SHOP, "https://other.example")]
        answers += [
            await policy.allowed(o) for o in ("https://SHOP.example", "https://other.example")
        ]
        clock.now += 60
        answers.append(await policy.allowed(SHOP))
        return answers

    assert asyncio.run(run()) == [True, False, True, False, True]
    assert identity.calls == [SHOP, "https://other.example", SHOP]


def test_policy_identity_fails_denied_and_not_cached(caplog: pytest.LogCaptureFixture) -> None:
    identity = FakeIdentity()
    identity.fail = True
    policy = OriginPolicy([], identity)

    with caplog.at_level(logging.WARNING, logger="synthwerk_vision"):
        assert not asyncio.run(policy.allowed(SHOP))
    identity.fail = False
    assert asyncio.run(policy.allowed(SHOP))
    assert identity.calls == [SHOP, SHOP]
    assert [r.__dict__["event.name"] for r in caplog.records] == ["vision.cors.lookup_failed"]


def test_policy_identity_timeout_denied_and_not_cached() -> None:
    identity = FakeIdentity(delay=0.2)
    policy = OriginPolicy([], identity, lookup_timeout_s=0.01)

    assert not asyncio.run(policy.allowed(SHOP))
    assert len(policy) == 0


def test_policy_many_origins_cache_never_above_bound() -> None:
    identity = FakeIdentity(listed=set())
    clock = Clock()
    policy = OriginPolicy([], identity, max_entries=100, clock=clock)

    async def run() -> None:
        for i in range(300):
            if i == 150:
                clock.now += 61  # the first 100 expire: dropped before the oldest valid ones
            await policy.allowed(f"https://shop{i}.example")
            assert len(policy) <= 100

    asyncio.run(run())
    assert len(policy) == 100


def test_policy_parallel_requests_for_one_new_origin_one_lookup() -> None:
    identity = FakeIdentity(delay=0.05)
    policy = OriginPolicy([], identity)

    async def run() -> list[bool]:
        return list(await asyncio.gather(*(policy.allowed(SHOP) for _ in range(50))))

    assert asyncio.run(run()) == [True] * 50
    assert identity.calls == [SHOP]


def test_policy_cancelled_caller_does_not_cancel_the_shared_lookup() -> None:
    identity = FakeIdentity(delay=0.05)
    policy = OriginPolicy([], identity)

    async def run() -> bool:
        first = asyncio.create_task(policy.allowed(SHOP))
        await asyncio.sleep(0.01)
        first.cancel()
        return await policy.allowed(SHOP)

    assert asyncio.run(run())
    assert identity.calls == [SHOP]


def test_policy_flood_of_new_origins_at_most_max_inflight_lookups() -> None:
    identity = FakeIdentity(listed=set(), delay=0.05)
    active = peak = 0
    plain = identity.__call__

    async def counting(origin: str) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            return await plain(origin)
        finally:
            active -= 1

    policy = OriginPolicy([], counting, max_inflight=8)

    async def run() -> list[bool]:
        return list(
            await asyncio.gather(*(policy.allowed(f"https://r{i}.example") for i in range(100)))
        )

    assert asyncio.run(run()) == [False] * 100
    assert peak == 8
    assert len(identity.calls) == 8
    assert len(policy) == 8  # only the answered lookups are cached


def test_policy_full_denies_new_origin_but_joins_running_lookup() -> None:
    identity = FakeIdentity(delay=0.05)
    policy = OriginPolicy([], identity, max_inflight=1)

    async def run() -> list[bool]:
        return list(
            await asyncio.gather(
                policy.allowed(SHOP), policy.allowed(SHOP), policy.allowed("https://x.example")
            )
        )

    assert asyncio.run(run()) == [True, True, False]
    assert identity.calls == [SHOP]


# ---- middleware on the app ----


@pytest.fixture
def identity() -> FakeIdentity:
    return FakeIdentity()


@pytest.fixture
def cors_client(settings: Settings, identity: FakeIdentity) -> Iterator[TestClient]:
    """A client without default headers: each test sets Origin and Authorization itself."""
    storage = _storage(settings)
    app = create_app(settings, storage=storage, classifier=FakeClassifier(), origin_lookup=identity)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client


def preflight(origin: str, method: str = "POST") -> dict[str, str]:
    return {"Origin": origin, "Access-Control-Request-Method": method}


def no_cors(headers: Any) -> bool:
    return not any(name in headers for name in CORS_HEADERS)


@pytest.mark.parametrize("path", ["/v1/classifications", "/v1/images"])
@pytest.mark.parametrize("origin", [SHOP, FIRST_PARTY])
def test_preflight_allowed_origin_no_token_204(
    cors_client: TestClient, path: str, origin: str
) -> None:
    response = cors_client.options(path, headers=preflight(origin))

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-methods"] == "POST"
    assert response.headers["access-control-allow-headers"] == "Authorization, Content-Type"
    assert response.headers["access-control-max-age"] == "600"
    assert response.headers["vary"] == "Origin"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-credentials" not in response.headers
    assert len(response.headers["x-request-id"]) == 32


@pytest.mark.parametrize(
    "headers",
    [
        preflight("https://evil.example"),
        preflight(SHOP, "PUT"),
        preflight("null"),
        {"Origin": SHOP},
        {"Access-Control-Request-Method": "POST"},
    ],
)
def test_preflight_denied_403_without_cors_headers(
    cors_client: TestClient, headers: dict[str, str]
) -> None:
    response = cors_client.options("/v1/classifications", headers=headers)

    assert response.status_code == 403
    assert no_cors(response.headers)
    assert response.headers["vary"] == "Origin"
    assert response.headers["cache-control"] == "no-store"


def test_preflight_two_origin_headers_403(cors_client: TestClient) -> None:
    response = cors_client.options(
        "/v1/images",
        headers=[
            ("Origin", SHOP),
            ("Origin", FIRST_PARTY),
            ("Access-Control-Request-Method", "POST"),
        ],
    )

    assert response.status_code == 403
    assert no_cors(response.headers)


def test_preflight_then_post_one_identity_call(
    cors_client: TestClient, identity: FakeIdentity
) -> None:
    token = shop_token()

    cors_client.options("/v1/classifications", headers=preflight(SHOP))
    response = cors_client.post(
        "/v1/classifications",
        headers={"Origin": SHOP, "Authorization": f"Bearer {token}"},
        files={"image": ("a.png", make_image(), "image/png")},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == SHOP
    assert response.headers["access-control-expose-headers"] == "X-Request-ID"
    assert response.headers["vary"] == "Origin"
    assert identity.calls == [SHOP]


def test_upload_listed_origin_201_with_cors(cors_client: TestClient) -> None:
    token = shop_token()

    response = cors_client.post(
        "/v1/images",
        headers={"Origin": SHOP, "Authorization": f"Bearer {token}"},
        files={"image": ("a.png", make_image(), "image/png")},
    )

    assert response.status_code == 201
    assert response.headers["access-control-allow-origin"] == SHOP


@pytest.mark.parametrize("path", ["/v1/classifications", "/v1/images"])
def test_post_without_token_listed_origin_401_the_widget_can_read(
    cors_client: TestClient, path: str
) -> None:
    response = cors_client.post(
        path, headers={"Origin": SHOP}, files={"image": ("a.png", make_image(), "image/png")}
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == SHOP
    assert response.headers["vary"] == "Origin"


def test_post_token_of_other_origin_403_with_cors(cors_client: TestClient) -> None:
    response = cors_client.post(
        "/v1/classifications",
        headers={"Origin": SHOP, "Authorization": f"Bearer {widget_token()}"},
        files={"image": ("a.png", make_image(), "image/png")},
    )

    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == SHOP


def test_post_too_large_413_with_cors(cors_client: TestClient) -> None:
    token = shop_token()

    response = cors_client.post(
        "/v1/images",
        headers={"Origin": SHOP, "Authorization": f"Bearer {token}"},
        files={"image": ("a.png", b"x" * (64 * 1024 + 1), "image/png")},
    )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == SHOP


def test_post_unknown_origin_vary_but_no_cors(cors_client: TestClient) -> None:
    response = cors_client.post(
        "/v1/classifications",
        headers={"Origin": "https://evil.example"},
        files={"image": ("a.png", make_image(), "image/png")},
    )

    assert response.status_code == 401
    assert no_cors(response.headers)
    assert response.headers["vary"] == "Origin"


def test_post_without_origin_vary_only(cors_client: TestClient, identity: FakeIdentity) -> None:
    response = cors_client.post("/v1/images", files={"image": ("a.png", make_image(), "image/png")})

    assert response.headers["vary"] == "Origin"
    assert no_cors(response.headers)
    assert identity.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("OPTIONS", "/healthz"),
        ("GET", "/v1/labels"),
        ("OPTIONS", "/v1/labels"),
        ("OPTIONS", "/v1/orgs/org-1/labels"),
    ],
)
def test_other_routes_no_cors(
    cors_client: TestClient, identity: FakeIdentity, method: str, path: str
) -> None:
    response = cors_client.request(method, path, headers=preflight(SHOP))

    assert no_cors(response.headers)
    assert "Origin" not in response.headers.get("vary", "")
    assert identity.calls == []


def test_admin_routes_no_cors_when_enabled(settings: Settings, identity: FakeIdentity) -> None:
    admin = settings.model_copy(update={"admin_token": SecretStr("t")})
    app = create_app(admin, storage=_storage(admin), origin_lookup=identity)
    app.dependency_overrides[get_settings] = lambda: admin
    with TestClient(app) as client:
        response = client.get(
            "/v1/labels", headers={"Origin": FIRST_PARTY, "Authorization": "Bearer t"}
        )

    assert no_cors(response.headers)


# ---- identity client against a local stub ----


class _Identity(BaseHTTPRequestHandler):
    status = 204
    delay = 0.0
    seen: list[tuple[str, str | None]] = []  # noqa: RUF012 (one test at a time)

    def do_GET(self) -> None:
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        time.sleep(self.delay)
        self.send_response(self.status)
        if self.status in (301, 302):
            self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def identity_server() -> Iterator[str]:
    _Identity.status = 204
    _Identity.delay = 0.0
    _Identity.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Identity)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_identity_204_yes_path_escaped_origin_no_token(identity_server: str) -> None:
    assert asyncio.run(IdentityOrigins(identity_server)("http://[::1]:3000"))
    assert _Identity.seen == [
        (f"{WIDGET_ORIGINS_PATH}?origin=http%3A%2F%2F%5B%3A%3A1%5D%3A3000", None)
    ]


def test_identity_404_no(identity_server: str) -> None:
    _Identity.status = 404

    assert not asyncio.run(IdentityOrigins(identity_server)(SHOP))


@pytest.mark.parametrize("status", [200, 302, 400, 500])
def test_identity_other_status_raises(identity_server: str, status: int) -> None:
    _Identity.status = status

    with pytest.raises(OriginLookupError, match=f"status {status}"):
        asyncio.run(IdentityOrigins(identity_server)(SHOP))
    assert len(_Identity.seen) == 1  # a redirect is not followed


def test_identity_slow_answer_times_out(identity_server: str) -> None:
    _Identity.delay = 1.0
    started = time.perf_counter()

    with pytest.raises(OriginLookupError, match="no answer"):
        asyncio.run(IdentityOrigins(identity_server, timeout_s=0.1)(SHOP))
    assert time.perf_counter() - started < 0.8


def test_flood_of_random_origin_preflights_keeps_the_service_answering(
    settings: Settings,
) -> None:
    """100 parallel preflights from new origins against a slow identity: at most 8 lookups,
    the rest denied at once, and /healthz stays fast meanwhile."""
    identity = FakeIdentity(listed=set(), delay=0.5)
    app = create_app(settings, storage=_storage(settings), origin_lookup=identity)

    async def run() -> tuple[list[int], float]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://vision") as client:
            flood = [
                asyncio.create_task(
                    client.options(
                        "/v1/classifications", headers=preflight(f"https://r{i}.example")
                    )
                )
                for i in range(100)
            ]
            await asyncio.sleep(0.05)
            started = time.perf_counter()
            health = await client.get("/healthz")
            health_s = time.perf_counter() - started
            assert health.status_code == 200
            return [r.status_code for r in await asyncio.gather(*flood)], health_s

    statuses, health_s = asyncio.run(run())

    assert statuses == [403] * 100
    assert len(identity.calls) == 8
    assert health_s < 0.2


def test_identity_unreachable_raises() -> None:
    with pytest.raises(OriginLookupError, match="no answer"):
        asyncio.run(IdentityOrigins("http://127.0.0.1:9", timeout_s=0.5)(SHOP))


def test_app_with_identity_url_asks_identity(settings: Settings, identity_server: str) -> None:
    configured = settings.model_copy(update={"identity_url": identity_server.rstrip("/")})
    app = create_app(configured, storage=_storage(configured))
    with TestClient(app) as client:
        allowed = client.options("/v1/images", headers=preflight(SHOP))

    assert allowed.status_code == 204
    assert len(_Identity.seen) == 1


def test_app_deployed_without_identity_url_warns(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    deployed = settings.model_copy(update={"platform_env": "dev"})
    with caplog.at_level(logging.WARNING, logger="synthwerk_vision"):
        app = create_app(deployed, storage=_storage(deployed))
    with TestClient(app) as client:
        denied = client.options("/v1/images", headers=preflight(SHOP))

    assert denied.status_code == 403
    assert "vision.cors.not_configured" in [r.__dict__.get("event.name") for r in caplog.records]
