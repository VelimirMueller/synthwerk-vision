import hashlib
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from starlette.concurrency import run_in_threadpool

from synthwerk_vision.api.auth import TOKEN_ERRORS, require_widget_token
from synthwerk_vision.feedback import FeedbackStore
from synthwerk_vision.images import (
    InvalidImageError,
    UploadTooLargeError,
    ValidatedImage,
    dhash,
    read_limited,
    validate_image,
)
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.runtime_labels import signature
from synthwerk_vision.schemas import (
    ClassificationOut,
    CorrectionOut,
    ErrorOut,
    ImageInfo,
    ImageOut,
    LabelOut,
    ModelInfo,
    TopicOut,
)
from synthwerk_vision.service import Classification, ClassificationService, ScoredLabel, ScoredTopic
from synthwerk_vision.storage import ImageStorage, StorageFullError

router = APIRouter()

_IMAGE_ERRORS: dict[int | str, dict[str, object]] = {
    413: {"model": ErrorOut, "description": "Upload exceeds the size limit"},
    415: {"model": ErrorOut, "description": "Not a JPEG, PNG or WebP image"},
}


def get_storage(request: Request) -> ImageStorage:
    storage: ImageStorage = request.app.state.storage
    return storage


async def get_service(request: Request) -> ClassificationService:
    """The current service. With several worker processes, a label change made by another
    worker shows up as a changed overlay file; this worker then rebuilds from the shared
    embedding cache (no text tower needed) before serving."""
    state = request.app.state
    service: ClassificationService | None = state.service
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "classification model not loaded")
    if (
        service.supports_runtime_labels
        and signature(state.runtime_labels.overlay) != state.overlay_signature
    ):
        async with state.admin_lock:
            seen = signature(state.runtime_labels.overlay)
            if seen != state.overlay_signature:
                current: ClassificationService = state.service
                taxonomy = state.runtime_labels.load(state.load_base_taxonomy)
                state.service = await run_in_threadpool(current.with_taxonomy, taxonomy)
                state.overlay_signature = seen
        service = state.service
    return service


async def validated_image(
    image: Annotated[UploadFile, File(description="JPEG, PNG or WebP image")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ValidatedImage:
    try:
        data = await read_limited(image, settings.max_upload_bytes)
        return validate_image(data, settings.max_image_pixels)
    except UploadTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except InvalidImageError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc


@router.post(
    "/v1/images",
    status_code=status.HTTP_201_CREATED,
    tags=["images"],
    responses={
        **TOKEN_ERRORS,
        **_IMAGE_ERRORS,
        507: {"model": ErrorOut, "description": "Upload storage quota reached"},
    },
    dependencies=[Depends(require_widget_token)],
)
async def upload_image(
    image: Annotated[ValidatedImage, Depends(validated_image)],
    storage: Annotated[ImageStorage, Depends(get_storage)],
) -> ImageOut:
    """Store an image under a server-generated id.

    The upload folder has a byte and a file quota. When it is full, the answer is 507.
    """
    try:
        image_id = await run_in_threadpool(storage.save, image.data, image.extension)
    except StorageFullError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
    return ImageOut(
        id=image_id,
        content_type=f"image/{image.format.lower()}",
        size_bytes=len(image.data),
        width=image.width,
        height=image.height,
    )


@router.post(
    "/v1/classifications",
    tags=["classification"],
    responses={
        **TOKEN_ERRORS,
        **_IMAGE_ERRORS,
        503: {"model": ErrorOut, "description": "Model not loaded or token keys unavailable"},
    },
    dependencies=[Depends(require_widget_token)],
)
async def classify_image(
    request: Request,
    image: Annotated[ValidatedImage, Depends(validated_image)],
    service: Annotated[ClassificationService, Depends(get_service)],
) -> ClassificationOut:
    """Classify an image without storing it.

    Returns the top-k labels with their topic path (e.g. animal → mammal → dog → beagle) and
    the topic scores, where a topic's score is the summed probability of every label below it.
    """
    result = await run_in_threadpool(service.classify, image.data)
    correction = await run_in_threadpool(_correction, request, service, image.data, result)
    total_ms = round((time.perf_counter() - request.state.started) * 1000, 2)
    return ClassificationOut(
        request_id=request.state.request_id,
        image=ImageInfo(
            sha256=hashlib.sha256(image.data).hexdigest(),
            content_type=f"image/{image.format.lower()}",
            size_bytes=len(image.data),
            width=image.width,
            height=image.height,
        ),
        model=ModelInfo(name=service.model_name, taxonomy_version=service.taxonomy.version),
        primary=_label_out(result.labels[0]),
        topic=_topic_out(result.topic),
        labels=[_label_out(label) for label in result.labels],
        topics=[_topic_out(topic) for topic in result.topics],
        uncertain=result.uncertain,
        correction=correction,
        timings_ms={**result.timings_ms, "total": total_ms},
    )


def _correction(
    request: Request, service: ClassificationService, data: bytes, result: Classification
) -> CorrectionOut | None:
    store: FeedbackStore | None = request.app.state.feedback
    if store is None:
        return None
    store.refresh()
    if len(store) == 0:
        return None
    taxonomy = service.taxonomy
    recall = store.recall(dhash(data), {label.id for label in taxonomy.labels})
    if recall is None:
        return None
    label = taxonomy.label(recall.feedback.label_id)
    assert label is not None  # recall() only returns ids present in the taxonomy  # noqa: S101
    score = next((s.score for s in result.labels if s.label.id == label.id), 0.0)
    return CorrectionOut(
        feedback_id=recall.feedback.id,
        label=_label_out(ScoredLabel(label, score, taxonomy.label_path(label))),
        distance=recall.distance,
    )


def _label_out(scored: ScoredLabel) -> LabelOut:
    return LabelOut(
        id=scored.label.id,
        label=scored.label.name,
        score=scored.score,
        topic=scored.label.topic,
        path=scored.path,
    )


def _topic_out(scored: ScoredTopic) -> TopicOut:
    return TopicOut(id=scored.id, parent=scored.parent, depth=scored.depth, score=scored.score)
