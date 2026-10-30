import asyncio
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from synthwerk_vision import __version__
from synthwerk_vision.api import admin
from synthwerk_vision.api.auth import build_verifier
from synthwerk_vision.api.cors import (
    IdentityOrigins,
    OriginLookup,
    OriginPolicy,
    WidgetCorsMiddleware,
)
from synthwerk_vision.api.routes import OrgServices, router
from synthwerk_vision.classifier import Classifier, ModelNotAvailableError, OnnxClassifier
from synthwerk_vision.feedback import FeedbackStore
from synthwerk_vision.label_sets import LabelSets
from synthwerk_vision.platform import health
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.platform.health import CheckResult, ReadinessCheck
from synthwerk_vision.platform.log import configure_logging, request_id_bound
from synthwerk_vision.platform.otel import (
    HEALTH_PATHS,
    Telemetry,
    build_resource,
    log_fields,
    setup_telemetry,
)
from synthwerk_vision.runtime_labels import RuntimeLabels, signature
from synthwerk_vision.service import ClassificationService
from synthwerk_vision.siglip import SiglipClassifier
from synthwerk_vision.storage import ImageStorage, S3ImageStorage, build_storage
from synthwerk_vision.taxonomy import Taxonomy

logger = logging.getLogger("synthwerk_vision")

# Accept a caller's X-Request-ID only when it is short and log-safe; otherwise mint one.
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


PACKAGED_TAXONOMY = {"siglip": "taxonomy_open.yaml", "mobilenet": "taxonomy.yaml"}


def _load_classifier(settings: Settings, taxonomy: Taxonomy) -> Classifier | None:
    try:
        if settings.backend == "siglip":
            return SiglipClassifier.from_dir(
                settings.siglip_dir, taxonomy, settings.label_cache_dir, settings.ort_threads
            )
        return OnnxClassifier(settings.model_path, settings.ort_threads)
    except ModelNotAvailableError as exc:
        logger.warning(
            "classification disabled: model not available",
            extra={"event.name": "vision.model.unavailable", "error.message": str(exc)},
        )
        return None


def label_sets(settings: Settings) -> LabelSets:
    """The default label set and the org sets, on top of the configured base taxonomy."""
    base_ref = (
        str(settings.taxonomy_path.resolve())
        if settings.taxonomy_path
        else f"package:{PACKAGED_TAXONOMY[settings.backend]}"
    )
    return LabelSets(settings.data_dir, base_ref)


def runtime_labels(settings: Settings) -> RuntimeLabels:
    """The default label set: what every org without an own set and every platform call uses."""
    return label_sets(settings).default


def feedback_store(settings: Settings) -> FeedbackStore | None:
    """Only touch the disk when corrections can be written (admin on) or already exist."""
    path = settings.data_dir / "feedback.sqlite3"
    return FeedbackStore(path) if settings.admin_token or path.is_file() else None


def build_service(
    settings: Settings, classifier: Classifier | None = None
) -> ClassificationService | None:
    """Classification service for the configured backend, or None when its model is missing."""
    # A missing model is an ops state (503 + /readyz down); a broken taxonomy is a config bug and
    # must stop startup, so TaxonomyError is not caught here.
    packaged = PACKAGED_TAXONOMY[settings.backend]
    taxonomy = runtime_labels(settings).load(
        lambda: Taxonomy.load(settings.taxonomy_path, packaged=packaged)
    )
    classifier = classifier or _load_classifier(settings, taxonomy)
    if classifier is None:
        return None
    return ClassificationService(
        classifier,
        taxonomy,
        top_k=settings.top_k,
        topic_min_score=settings.topic_min_score,
        uncertain_below=settings.uncertain_below,
    )


async def _model_ready(request: Request) -> CheckResult:
    if getattr(request.app.state, "service", None) is None:
        return CheckResult("down", "model not loaded")
    return CheckResult("ok")


def _data_dir_check(data_dir: Path) -> ReadinessCheck:
    """Runtime labels and corrections are written here. Not writable: the admin endpoints
    fail, classification still works, so the service is degraded, not down. Uses
    os.access: as root it reports writable; the image runs as uid 10001."""

    async def run(_: Request) -> CheckResult:
        candidates = (data_dir, *data_dir.absolute().parents)
        existing = next((p for p in candidates if p.exists()), Path(data_dir.absolute().anchor))
        writable = await asyncio.to_thread(os.access, existing, os.W_OK)
        return CheckResult("ok") if writable else CheckResult("degraded", "data dir not writable")

    return ReadinessCheck("data_dir", run)


def _storage_check(storage: S3ImageStorage) -> ReadinessCheck:
    """The upload bucket must exist and be listable. Not reachable or slow: uploads fail
    with 503, classification still works, so the service is degraded, not down."""

    async def run(_: Request) -> CheckResult:
        try:
            reachable = await asyncio.wait_for(storage.reachable(), 0.4)
        except TimeoutError:
            return CheckResult("degraded", "upload bucket timeout")
        if reachable:
            return CheckResult("ok")
        return CheckResult("degraded", "upload bucket not reachable")

    return ReadinessCheck("storage", run, timeout_s=0.45)


def _lookup(settings: Settings, given: OriginLookup | None) -> OriginLookup | None:
    """Identity origin lookup for widget CORS; not part of /readyz (identity down only
    narrows CORS to the static list)."""
    if given is not None:
        return given
    if settings.identity_url is not None:
        return IdentityOrigins(settings.identity_url)
    if settings.platform_env not in {"local", "ci"}:
        logger.warning(
            "widget CORS uses the static list only: identity is not configured",
            extra={"event.name": "vision.cors.not_configured"},
        )
    return None


def create_app(
    settings: Settings | None = None,
    *,
    storage: ImageStorage | None = None,
    classifier: Classifier | None = None,
    telemetry: Telemetry | None = None,
    origin_lookup: OriginLookup | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    resource = build_resource(settings)
    configure_logging(settings, log_fields(resource))
    verifier = build_verifier(settings)  # before telemetry: a config error starts nothing
    storage = storage or build_storage(settings)
    telemetry = telemetry or setup_telemetry(resource)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.storage = storage
        app.state.label_sets = label_sets(settings)
        app.state.label_sets.migrate()  # before the default set is read
        app.state.service = build_service(settings, classifier)
        app.state.runtime_labels = app.state.label_sets.default
        app.state.org_services = OrgServices()
        app.state.overlay_signature = signature(app.state.runtime_labels.overlay)
        app.state.load_base_taxonomy = lambda: Taxonomy.load(
            settings.taxonomy_path, packaged=PACKAGED_TAXONOMY[settings.backend]
        )
        app.state.feedback = feedback_store(settings)
        app.state.admin_lock = asyncio.Lock()
        try:
            yield
        finally:
            telemetry.shutdown()

    app = FastAPI(title="Synthwerk Vision API", version=__version__, lifespan=lifespan)
    app.state.commit = settings.commit
    app.state.verifier = verifier
    app.state.readiness_checks = [ReadinessCheck("model", _model_ready)]
    if settings.admin_token is not None:
        app.state.readiness_checks.append(_data_dir_check(settings.data_dir))
    if isinstance(storage, S3ImageStorage):
        app.state.readiness_checks.append(_storage_check(storage))
    # Inside request_context: a preflight gets a request id and a log line too.
    app.add_middleware(
        WidgetCorsMiddleware,
        policy=OriginPolicy(settings.cors_origins, _lookup(settings, origin_lookup)),
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.started = time.perf_counter()
        with request_id_bound(request_id):
            error: Exception | None = None
            try:
                response = await call_next(request)
            except Exception as exc:
                # Unhandled error: answer the 500 here (same body as Starlette's), so the
                # response carries the request id and the request still gets its log line.
                error = exc
                response = PlainTextResponse("Internal Server Error", status_code=500)
            response.headers["X-Request-ID"] = request_id
            route = request.scope.get("route")
            fields: dict[str, object] = {
                "event.name": "http.server.request",
                "http.request.method": request.method,
                "http.response.status_code": response.status_code,
                "synthwerk.duration_ms": round(
                    (time.perf_counter() - request.state.started) * 1000, 1
                ),
            }
            if route is not None:  # unmatched paths (404) get no route: never the raw path
                fields["http.route"] = route.path
            if error is not None:
                fields["error.type"] = type(error).__qualname__
                logger.error("request completed", extra=fields, exc_info=error)
            else:
                level = logging.DEBUG if request.url.path in HEALTH_PATHS else logging.INFO
                logger.log(level, "request completed", extra=fields)
        return response

    app.include_router(health.router)
    app.include_router(router)
    app.include_router(admin.router)
    telemetry.instrument(app)
    return app
