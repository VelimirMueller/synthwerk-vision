"""Endpoints that change what the server knows: label sets and corrections.

They need `Authorization: Bearer <SYNTHWERK_VISION_ADMIN_TOKEN>` and do not exist (404) when
no token is configured. The admin token is a service credential: an operator uses it for the
default set, and the studio BFF uses it for the org routes after it checked that the signed-in
user is an owner or admin of that org (synthwerk#44). Vision trusts the org id in the path.

| Routes | Label set |
|---|---|
| `/v1/labels`, `/v1/feedback` | The default set and default corrections |
| `/v1/orgs/{org_id}/labels`, `/v1/orgs/{org_id}/feedback` | The set and corrections of one org |

A label change builds a complete new service next to the running one and swaps it in with one
assignment, so in-flight requests finish on the old service and none sees a half-updated model.
"""

import hashlib
import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from synthwerk_vision.api.routes import OrgServices, get_service, service_for, validated_image
from synthwerk_vision.feedback import DEFAULT_ORG
from synthwerk_vision.images import ValidatedImage, dhash
from synthwerk_vision.label_sets import ORG_ID_PATTERN, LabelSets
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.runtime_labels import RuntimeLabelError, RuntimeLabels, signature
from synthwerk_vision.schemas import (
    ErrorOut,
    FeedbackOut,
    LabelIn,
    LabelSetOut,
    LabelSetTopicOut,
    RuntimeLabelOut,
    RuntimeLabelsOut,
)
from synthwerk_vision.service import ClassificationService
from synthwerk_vision.taxonomy import Taxonomy, TaxonomyError

router = APIRouter(tags=["admin"])
_bearer = HTTPBearer(auto_error=False)


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if settings.admin_token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    # Compare digests: equal length always, so timing reveals neither content nor length.
    expected = hashlib.sha256(settings.admin_token.get_secret_value().encode()).digest()
    given = hashlib.sha256(credentials.credentials.encode() if credentials else b"").digest()
    if not secrets.compare_digest(given, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid token", headers={"WWW-Authenticate": "Bearer"}
        )


Admin = Annotated[None, Depends(require_admin)]
OrgId = Annotated[
    str,
    PathParam(
        pattern=ORG_ID_PATTERN,
        max_length=64,
        description="Zitadel org id: the `urn:zitadel:iam:user:resourceowner:id` of widget tokens",
    ),
]
_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorOut},
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
}
# Runs after a label change: puts the new service where the next request finds it.
Publish = Callable[[ClassificationService], None]
# Runs under the admin lock: the service of the set now, so a change builds on the last one.
Current = Callable[[], ClassificationService | None]


def _runtime_label_out(item: dict[str, object], taxonomy: Taxonomy) -> RuntimeLabelOut:
    label = taxonomy.label(str(item["id"]))
    return RuntimeLabelOut(
        id=str(item["id"]),
        name=str(item["name"]),
        topic=str(item["topic"]),
        prompt=str(item["prompt"]) if item.get("prompt") else None,
        path=taxonomy.label_path(label) if label else [],
    )


def _org_labels(request: Request, org_id: str) -> RuntimeLabels:
    sets: LabelSets = request.app.state.label_sets
    return sets.org(org_id)  # the path pattern makes the id a safe file name


def _current_default(request: Request) -> Current:
    return lambda: request.app.state.service


def _current_org(request: Request, org_id: str, labels: RuntimeLabels) -> Current:
    def current() -> ClassificationService | None:
        cache: OrgServices = request.app.state.org_services
        own = cache.get(org_id, signature(labels.overlay)) if labels.own else None
        default: ClassificationService | None = request.app.state.service
        return own or default

    return current


def _publish_default(request: Request) -> Publish:
    def publish(service: ClassificationService) -> None:
        request.app.state.service = service
        request.app.state.overlay_signature = signature(request.app.state.runtime_labels.overlay)

    return publish


def _publish_org(request: Request, org_id: str, labels: RuntimeLabels) -> Publish:
    def publish(service: ClassificationService) -> None:
        cache: OrgServices = request.app.state.org_services
        cache.put(org_id, service, signature(labels.overlay))

    return publish


def _builder(current: Current) -> ClassificationService:
    service = current()
    if service is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no model with runtime labels loaded")
    if not service.supports_runtime_labels:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{service.model_name} has a fixed label set")
    return service


async def _add(
    request: Request, body: LabelIn, labels: RuntimeLabels, current: Current, publish: Publish
) -> RuntimeLabelOut:
    """Add a label to `labels`. The service of the set, read under the lock, builds the new
    one: its known label embeddings are reused, so only the new text needs the text tower."""
    parent = (body.topic_parent or "") if body.new_topic else None
    async with request.app.state.admin_lock:
        builder = _builder(current)
        try:
            item, new_service = await run_in_threadpool(
                labels.add, body.name, body.topic, body.prompt, parent, builder.with_taxonomy
            )
        except (RuntimeLabelError, TaxonomyError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        publish(new_service)
    return _runtime_label_out(item, new_service.taxonomy)


async def _remove(
    request: Request, label_id: str, labels: RuntimeLabels, current: Current, publish: Publish
) -> Response:
    async with request.app.state.admin_lock:
        builder = _builder(current)
        try:
            new_service = await run_in_threadpool(labels.remove, label_id, builder.with_taxonomy)
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no runtime label {label_id!r}"
            ) from exc
        except (RuntimeLabelError, TaxonomyError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        publish(new_service)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _feedback(
    request: Request,
    image: ValidatedImage,
    label_id: str,
    service: ClassificationService,
    org_id: str,
) -> FeedbackOut:
    store = request.app.state.feedback
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "feedback store not available")
    if service.taxonomy.label(label_id) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"unknown label id {label_id!r}")
    fingerprint = await run_in_threadpool(dhash, image.data)
    entry = await run_in_threadpool(
        store.add, hashlib.sha256(image.data).hexdigest(), fingerprint, label_id, org_id
    )
    return FeedbackOut(id=entry.id, label_id=entry.label_id, dhash=entry.dhash)


# --- Default set (platform) ---


@router.get("/v1/labels", responses=_ERRORS)
def list_runtime_labels(
    _: Admin,
    request: Request,
    service: Annotated[ClassificationService, Depends(get_service)],
) -> RuntimeLabelsOut:
    """Labels added to the default set (packaged labels are in the taxonomy files)."""
    labels = request.app.state.runtime_labels.labels()
    return RuntimeLabelsOut(
        labels=[_runtime_label_out(item, service.taxonomy) for item in labels],
        total_labels=len(service.taxonomy.labels),
    )


@router.post("/v1/labels", status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def add_runtime_label(
    _: Admin,
    request: Request,
    body: LabelIn,
    service: Annotated[ClassificationService, Depends(get_service)],
) -> RuntimeLabelOut:
    """Add a label to the default set; only its text is embedded (no retraining)."""
    if not service.supports_runtime_labels:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{service.model_name} has a fixed label set")
    labels: RuntimeLabels = request.app.state.runtime_labels
    return await _add(request, body, labels, _current_default(request), _publish_default(request))


@router.delete("/v1/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_runtime_label(_: Admin, request: Request, label_id: str) -> Response:
    labels: RuntimeLabels = request.app.state.runtime_labels
    return await _remove(
        request, label_id, labels, _current_default(request), _publish_default(request)
    )


@router.post("/v1/feedback", status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def add_feedback(
    _: Admin,
    request: Request,
    image: Annotated[ValidatedImage, Depends(validated_image)],
    label_id: Annotated[str, Form(description="Correct label id for this picture")],
    service: Annotated[ClassificationService, Depends(get_service)],
) -> FeedbackOut:
    """Record the correct label for a picture as a default correction: every org without an
    own correction for the picture gets it as `correction`."""
    return await _feedback(request, image, label_id, service, DEFAULT_ORG)


# --- Org sets (synthwerk#44) ---


@router.get("/v1/orgs/{org_id}/labels", responses=_ERRORS)
async def get_label_set(_: Admin, request: Request, org_id: OrgId) -> LabelSetOut:
    """The label set of an org: the default set until the org changes it."""
    labels = _org_labels(request, org_id)
    service = await service_for(request, org_id)
    taxonomy = service.taxonomy
    items = await run_in_threadpool(labels.labels)
    return LabelSetOut(
        org_id=org_id,
        custom=labels.own,
        labels=[_runtime_label_out(item, taxonomy) for item in items],
        topics=[
            LabelSetTopicOut(id=t.id, parent=t.parent, depth=t.depth)
            for t in taxonomy.topics.values()
        ],
        total_labels=len(taxonomy.labels),
    )


@router.post("/v1/orgs/{org_id}/labels", status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def add_org_label(
    _: Admin, request: Request, org_id: OrgId, body: LabelIn
) -> RuntimeLabelOut:
    """Add a label to the org's set. The first change copies the default set into the org."""
    labels = _org_labels(request, org_id)
    await service_for(request, org_id)  # builds the org service into the cache when missing
    return await _add(
        request,
        body,
        labels,
        _current_org(request, org_id, labels),
        _publish_org(request, org_id, labels),
    )


@router.delete(
    "/v1/orgs/{org_id}/labels/{label_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
async def delete_org_label(_: Admin, request: Request, org_id: OrgId, label_id: str) -> Response:
    """Remove an added label from the org's set. Packaged labels stay (409)."""
    labels = _org_labels(request, org_id)
    await service_for(request, org_id)  # builds the org service into the cache when missing
    return await _remove(
        request,
        label_id,
        labels,
        _current_org(request, org_id, labels),
        _publish_org(request, org_id, labels),
    )


@router.post("/v1/orgs/{org_id}/feedback", status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def add_org_feedback(
    _: Admin,
    request: Request,
    org_id: OrgId,
    image: Annotated[ValidatedImage, Depends(validated_image)],
    label_id: Annotated[str, Form(description="Correct label id for this picture")],
) -> FeedbackOut:
    """Record the correct label for a picture for one org. Other orgs never see it."""
    service = await service_for(request, org_id)
    return await _feedback(request, image, label_id, service, org_id)
