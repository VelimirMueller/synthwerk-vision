"""CORS of the widget routes `POST /v1/classifications` and `POST /v1/images` (infra#24).

The vision widget runs on customer sites, so a static origin list is not enough. An origin
is allowed when it is in the static list (first-party apps and local development,
`SYNTHWERK_VISION_CORS_ORIGINS`) or when identity says that an app of an active org lists it
(`GET /internal/v1/widget-origins`, `SYNTHWERK_VISION_IDENTITY_URL`). Everything else is
denied. Identity answers are cached per canonical origin for a short time.

Same semantics as synthwerk-llm (`internal/cors`). CORS only lets the browser read the
answer. The real check stays in the route: the widget token is bound to its origin
(`synthwerk:origin`, see `api/auth.py`).

Other routes get no CORS: health is not called from a browser, and the label admin routes
are server-to-server.
"""

import asyncio
import logging
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from urllib.parse import quote

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from synthwerk_vision.api.origin import OriginError, canonical_origin

logger = logging.getLogger("synthwerk_vision")

DEFAULT_TTL_S = 60.0
DEFAULT_MAX_ENTRIES = 4096
DEFAULT_LOOKUP_TIMEOUT_S = 2.0
MAX_AGE = "600"  # Access-Control-Max-Age of an allowed preflight, in seconds
ALLOW_HEADERS = "Authorization, Content-Type"  # the request headers the widget sends
# Vision sends no Retry-After. The widget may read the request id of an error answer.
EXPOSE_HEADERS = "X-Request-ID"

# Path -> method of the routes that answer CORS.
WIDGET_ROUTES: Mapping[str, str] = {
    "/v1/classifications": "POST",
    "/v1/images": "POST",
}

WIDGET_ORIGINS_PATH = "/internal/v1/widget-origins"

OriginLookup = Callable[[str], Awaitable[bool]]
"""Asks identity whether an app of an active org lists a canonical origin. Raises on error."""


class OriginLookupError(Exception):
    """Identity gave no usable answer."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # a 3xx then raises HTTPError: not a yes or no


class IdentityOrigins:
    """`GET <identity>/internal/v1/widget-origins?origin=<origin>`: 204 yes, 404 no, anything
    else raises OriginLookupError. The call sends no token. urllib in a worker thread, like
    the key set fetch (`platform/auth.py`), so the event loop never blocks."""

    def __init__(self, base_url: str, timeout_s: float = DEFAULT_LOOKUP_TIMEOUT_S) -> None:
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # No proxy from the environment and no redirects: identity is an internal service.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    async def __call__(self, origin: str) -> bool:
        return await asyncio.to_thread(self._get, origin)

    def _get(self, origin: str) -> bool:
        url = f"{self._base}{WIDGET_ORIGINS_PATH}?origin={quote(origin, safe='')}"
        request = urllib.request.Request(url, method="GET")  # noqa: S310 (http/https only)
        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                response.read(4096)
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
        except (OSError, ValueError) as exc:  # URLError, timeout, refused, bad URL
            raise OriginLookupError("no answer") from exc
        if status == 204:
            return True
        if status == 404:
            return False
        raise OriginLookupError(f"status {status}")


class OriginPolicy:
    """Decides which origins may read the answer of a widget route.

    Identity answers (yes and no) are cached per canonical origin for `ttl_s`, at most
    `max_entries`. One identity call per origin at a time. A failed or slow call denies and
    is not cached.
    """

    def __init__(
        self,
        static: Iterable[str] = (),
        lookup: OriginLookup | None = None,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        lookup_timeout_s: float = DEFAULT_LOOKUP_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._static = frozenset(static)
        self._lookup = lookup
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._lookup_timeout_s = lookup_timeout_s
        self._clock = clock
        self._cache: OrderedDict[str, tuple[bool, float]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[bool]] = {}

    def __len__(self) -> int:
        return len(self._cache)

    async def allowed(self, header: str) -> bool:
        """Whether the Origin header value may read the answer. A value that fails the strict
        parser is denied without a lookup."""
        try:
            origin = canonical_origin(header)
        except OriginError:
            return False
        if origin in self._static:
            return True
        if self._lookup is None:
            return False
        cached = self._cached(origin)
        if cached is not None:
            return cached
        task = self._inflight.get(origin)
        if task is None:
            task = asyncio.create_task(self._ask(origin))
            self._inflight[origin] = task
            task.add_done_callback(self._done(origin))
        try:
            # shield: a caller that goes away does not cancel the call the others wait for.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "CORS origin lookup failed, origin denied",
                extra={"event.name": "vision.cors.lookup_failed", "error.type": type(exc).__name__},
            )
            return False

    def _done(self, origin: str) -> Callable[[asyncio.Task[bool]], None]:
        def done(task: asyncio.Task[bool]) -> None:
            if self._inflight.get(origin) is task:
                del self._inflight[origin]
            if not task.cancelled():
                task.exception()  # retrieved: no "never retrieved" warning when nobody waits

        return done

    async def _ask(self, origin: str) -> bool:
        assert self._lookup is not None  # noqa: S101
        allowed = await asyncio.wait_for(self._lookup(origin), self._lookup_timeout_s)
        self._store(origin, allowed)
        return allowed

    def _cached(self, origin: str) -> bool | None:
        entry = self._cache.get(origin)
        if entry is None:
            return None
        allowed, expires = entry
        if self._clock() >= expires:
            del self._cache[origin]
            return None
        return allowed

    def _store(self, origin: str, allowed: bool) -> None:
        """A full cache first drops expired entries, then the oldest ones."""
        now = self._clock()
        self._cache.pop(origin, None)
        if len(self._cache) >= self._max_entries:
            for key in [k for k, (_, expires) in self._cache.items() if now >= expires]:
                del self._cache[key]
            while len(self._cache) >= self._max_entries:
                self._cache.popitem(last=False)
        self._cache[origin] = (allowed, now + self._ttl_s)


def _route_path(scope: Scope) -> str:
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path


class WidgetCorsMiddleware:
    """Answers CORS on the widget routes only; every other path passes through untouched.

    - `OPTIONS` (no token): one allowed `Origin` and `Access-Control-Request-Method` equal to
      the route method → 204 with the CORS headers; else 403 without CORS headers. Both send
      `Vary: Origin` and `Cache-Control: no-store`.
    - Other methods: every answer gets `Vary: Origin`. An allowed origin gets
      `Access-Control-Allow-Origin` and `Access-Control-Expose-Headers`, also on an error.
    - Never `Access-Control-Allow-Credentials`: the widget sends a Bearer token, no cookie.
    """

    def __init__(
        self, app: ASGIApp, policy: OriginPolicy, routes: Mapping[str, str] = WIDGET_ROUTES
    ) -> None:
        self.app = app
        self.policy = policy
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = self.routes.get(_route_path(scope)) if scope["type"] == "http" else None
        if method is None:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origins = headers.getlist("origin")
        if scope["method"] == "OPTIONS":
            response = await self._preflight(method, origins, headers)
            await response(scope, receive, send)
            return
        allowed = len(origins) == 1 and await self.policy.allowed(origins[0])
        # The value passed the strict parser: ASCII, no white space, no CR or LF.
        origin = origins[0] if allowed else None

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.add_vary_header("Origin")
                if origin is not None:
                    response_headers["Access-Control-Allow-Origin"] = origin
                    response_headers["Access-Control-Expose-Headers"] = EXPOSE_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_cors)

    async def _preflight(self, method: str, origins: list[str], headers: Headers) -> Response:
        answer = {"Vary": "Origin", "Cache-Control": "no-store"}
        requested = headers.getlist("access-control-request-method")
        if len(origins) != 1 or requested != [method] or not await self.policy.allowed(origins[0]):
            return Response(status_code=403, headers=answer)
        return Response(
            status_code=204,
            headers={
                **answer,
                "Access-Control-Allow-Origin": origins[0],
                "Access-Control-Allow-Methods": method,
                "Access-Control-Allow-Headers": ALLOW_HEADERS,
                "Access-Control-Max-Age": MAX_AGE,
            },
        )
