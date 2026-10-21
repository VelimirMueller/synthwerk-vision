"""Endpoints that change what the server knows: runtime labels and corrections.

They need `Authorization: Bearer <SYNTHWERK_VISION_ADMIN_TOKEN>` and do not exist (404) when
no token is configured. A label change builds a complete new service next to the running one
and swaps it in with one assignment, so in-flight requests finish on the old service and none
sees a half-updated model.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from synthwerk_vision.api.routes import get_service, validated_image
from synthwerk_vision.images import ValidatedImage, dhash
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.runtime_labels import RuntimeLabelError, signature
from synthwerk_vision.schemas import (
    ErrorOut,
    FeedbackOut,
    LabelIn,
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
_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorOut},
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
}


def _runtime_label_out(item: dict[str, object], taxonomy: Taxonomy) -> RuntimeLabelOut:
    label = taxonomy.label(str(item["id"]))
    return RuntimeLabelOut(
        id=str(item["id"]),
        name=str(item["name"]),
        topic=str(item["topic"]),
        prompt=str(item["prompt"]) if item.get("prompt") else None,
        path=taxonomy.label_path(label) if label else [],
    )


@router.get("/v1/labels", responses=_ERRORS)
def list_runtime_labels(
    _: Admin,
    request: Request,
    service: Annotated[ClassificationService, Depends(get_service)],
) -> RuntimeLabelsOut:
    """Labels added at runtime (packaged labels are in the taxonomy files)."""
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
    """Add a label the model scores from now on; only its text is embedded (no retraining)."""
    if not service.supports_runtime_labels:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{service.model_name} has a fixed label set")
    parent = (body.topic_parent or "") if body.new_topic else None
    async with request.app.state.admin_lock:
        current: ClassificationService = request.app.state.service
        try:
            item, new_service = await run_in_threadpool(
                request.app.state.runtime_labels.add,
                body.name,
                body.topic,
                body.prompt,
                parent,
                current.with_taxonomy,
            )
        except (RuntimeLabelError, TaxonomyError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        request.app.state.service = new_service
        request.app.state.overlay_signature = signature(request.app.state.runtime_labels.overlay)
    return _runtime_label_out(item, new_service.taxonomy)


@router.delete("/v1/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_runtime_label(_: Admin, request: Request, label_id: str) -> Response:
    async with request.app.state.admin_lock:
        current: ClassificationService | None = request.app.state.service
        if current is None or not current.supports_runtime_labels:
            raise HTTPException(status.HTTP_409_CONFLICT, "no model with runtime labels loaded")
        try:
            new_service = await run_in_threadpool(
                request.app.state.runtime_labels.remove, label_id, current.with_taxonomy
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no runtime label {label_id!r}"
            ) from exc
        except (RuntimeLabelError, TaxonomyError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        request.app.state.service = new_service
        request.app.state.overlay_signature = signature(request.app.state.runtime_labels.overlay)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/v1/feedback", status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def add_feedback(
    _: Admin,
    request: Request,
    image: Annotated[ValidatedImage, Depends(validated_image)],
    label_id: Annotated[str, Form(description="Correct label id for this picture")],
    service: Annotated[ClassificationService, Depends(get_service)],
) -> FeedbackOut:
    """Record the correct label for a picture; the same picture gets it as `correction`."""
    store = request.app.state.feedback
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "feedback store not available")
    if service.taxonomy.label(label_id) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"unknown label id {label_id!r}")
    fingerprint = await run_in_threadpool(dhash, image.data)
    entry = await run_in_threadpool(
        store.add,
        hashlib.sha256(image.data).hexdigest(),
        fingerprint,
        label_id,
    )
    return FeedbackOut(id=entry.id, label_id=entry.label_id, dhash=entry.dhash)
