"""Image storage. The API depends on the protocol; the backend comes from the settings.

Both backends store an image under the same key, `<org>/<yyyy>/<mm>/<uuid4>.<ext>`, and keep
a byte and a file quota per org.

- `LocalImageStorage`: files on the local disk. The default, for local development.
- `S3ImageStorage`: an S3-compatible bucket (Supabase Storage in dev, stg and prd, #77).
"""

import logging
import os
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import obstore
from obstore.exceptions import BaseError as ObjectStoreError
from obstore.store import S3Store

from synthwerk_vision.platform.config import Settings

logger = logging.getLogger("synthwerk_vision")

# Org ids come from a signed widget token. They become a path segment and a key prefix, so
# only a short, plain id is accepted: never a slash, a dot segment or a control character.
_ORG_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_EXTENSIONS = {"jpg", "png", "webp"}
_DEV_ONLY_ENVS = {"local", "ci"}

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StorageFullError(Exception):
    """The org has reached its byte or file quota; no upload is stored until space is freed."""


class StorageUnavailableError(Exception):
    """The object store did not answer or refused the request."""


class StorageConfigError(Exception):
    """The storage settings are missing or invalid; the service does not start."""


class ImageStorage(Protocol):
    def save(self, data: bytes, *, org_id: str, extension: str, content_type: str) -> str:
        """Persist data for the org and return the server-generated image id.

        Raises StorageFullError when the data would exceed the org's quota, and
        StorageUnavailableError when the backend cannot store it.
        """
        ...


def object_key(org_id: str, image_id: str, extension: str, now: datetime) -> str:
    """`<org>/<yyyy>/<mm>/<image_id>.<ext>`. Raises ValueError for an unsafe org id."""
    if not _ORG_ID.fullmatch(org_id):
        raise ValueError("org id is not usable as a storage key")
    if extension not in _EXTENSIONS:
        raise ValueError("unsupported extension")
    return f"{org_id}/{now:%Y}/{now:%m}/{image_id}.{extension}"


class LocalImageStorage:
    """Files on the local disk, bounded by a byte and a file quota per org.

    Usage is measured from the org folder on every save, so files written by other worker
    processes count too. Saves in one process are serialised; two processes can each pass
    the check at the same moment, so the quota can be exceeded by at most one upload per
    other worker.
    """

    def __init__(
        self, root: Path, *, max_bytes: int, max_files: int, clock: Clock = _utc_now
    ) -> None:
        self._root = root.resolve()
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._clock = clock
        self._lock = threading.Lock()

    def save(self, data: bytes, *, org_id: str, extension: str, content_type: str) -> str:
        image_id = uuid.uuid4().hex
        path = self._root / object_key(org_id, image_id, extension, self._clock())
        with self._lock:
            files, used = self._usage(self._root / org_id)
            if files + 1 > self._max_files or used + len(data) > self._max_bytes:
                raise StorageFullError("upload storage quota reached")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(path, data)
        return image_id

    @staticmethod
    def _usage(folder: Path) -> tuple[int, int]:
        files = used = 0
        if not folder.is_dir():
            return files, used
        for dirpath, _, names in os.walk(folder):
            for name in names:
                try:
                    stat = os.lstat(os.path.join(dirpath, name))
                except FileNotFoundError:  # deleted by another process during the walk
                    continue
                files += 1
                used += stat.st_size
        return files, used

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        # O_EXCL: never overwrite an existing file, even on a uuid collision.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise


class S3ImageStorage:
    """Objects in an S3-compatible bucket, bounded by a byte and a file quota per org.

    The quota is measured by listing the org prefix on every save: no usage table to keep in
    step, and uploads from every instance count. A listing page holds 1000 keys, so the check
    costs at most `max_files / 1000` requests. Saves in one process are serialised; two
    instances can each pass the check at the same moment, so the quota can be exceeded by at
    most one upload per other instance or worker.

    Objects get the content type of the decoded image and no ACL: they stay as private as
    the bucket.
    """

    LIST_PAGE = 1000

    def __init__(
        self, store: S3Store, *, max_bytes: int, max_files: int, clock: Clock = _utc_now
    ) -> None:
        self._store = store
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._clock = clock
        self._lock = threading.Lock()

    def save(self, data: bytes, *, org_id: str, extension: str, content_type: str) -> str:
        image_id = uuid.uuid4().hex
        key = object_key(org_id, image_id, extension, self._clock())
        with self._lock:
            try:
                files, used = self._usage(f"{org_id}/")
                if files + 1 > self._max_files or used + len(data) > self._max_bytes:
                    raise StorageFullError("upload storage quota reached")
                obstore.put(
                    self._store,
                    key,
                    data,
                    attributes={"Content-Type": content_type},
                    use_multipart=False,
                )
            except (ObjectStoreError, OSError) as exc:
                logger.warning(
                    "object store request failed",
                    extra={"event.name": "vision.storage.failed", "error.type": type(exc).__name__},
                )
                raise StorageUnavailableError("upload storage is not available") from exc
        return image_id

    def _usage(self, prefix: str) -> tuple[int, int]:
        """Objects and bytes under the prefix. Stops early once a quota is already reached."""
        files = used = 0
        for chunk in obstore.list(self._store, prefix=prefix, chunk_size=self.LIST_PAGE):
            files += len(chunk)
            used += sum(meta["size"] for meta in chunk)
            if files >= self._max_files or used >= self._max_bytes:
                break
        return files, used

    async def reachable(self) -> bool:
        """True when the bucket exists and the credentials may list it."""
        try:
            await obstore.list(self._store, prefix="_readyz/", chunk_size=1).collect_async()
        except (ObjectStoreError, OSError):
            return False
        return True


def _secret(settings: Settings, name: str) -> str:
    """A secret from `SYNTHWERK_VISION_<NAME>` or from the file named by `<NAME>_FILE`."""
    value = getattr(settings, name)
    path: Path | None = getattr(settings, f"{name}_file")
    env = f"SYNTHWERK_VISION_{name.upper()}"
    if value is not None and path is not None:
        raise StorageConfigError(f"set {env} or {env}_FILE, not both")
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise StorageConfigError(f"{env}_FILE cannot be read") from exc
    else:
        text = value.get_secret_value().strip() if value is not None else ""
    if not text:
        raise StorageConfigError(f"{env} or {env}_FILE required for the s3 storage backend")
    return text


def build_s3_store(settings: Settings) -> S3Store:
    """The S3 client for the settings. Raises StorageConfigError when a setting is missing."""
    missing = [
        f"SYNTHWERK_VISION_{name.upper()}"
        for name in ("s3_endpoint", "s3_region", "s3_bucket")
        if not getattr(settings, name)
    ]
    if missing:
        raise StorageConfigError(f"{' and '.join(missing)} required for the s3 storage backend")
    endpoint = str(settings.s3_endpoint)
    plain_http = endpoint.startswith("http://")
    if not (plain_http or endpoint.startswith("https://")):
        raise StorageConfigError("SYNTHWERK_VISION_S3_ENDPOINT must be an http(s) URL")
    if plain_http and settings.platform_env not in _DEV_ONLY_ENVS:
        raise StorageConfigError(
            f"SYNTHWERK_VISION_S3_ENDPOINT must use https in {settings.platform_env}"
        )
    return S3Store(
        settings.s3_bucket,
        endpoint=endpoint,
        region=settings.s3_region,
        access_key_id=_secret(settings, "s3_access_key_id"),
        secret_access_key=_secret(settings, "s3_secret_access_key"),
        # Supabase Storage serves the bucket as a path below the endpoint, not as a host.
        virtual_hosted_style_request=False,
        client_options={
            "allow_http": plain_http,
            "connect_timeout": timedelta(seconds=2),
            "timeout": timedelta(seconds=10),
        },
        retry_config={"max_retries": 2, "retry_timeout": timedelta(seconds=10)},
    )


def build_storage(settings: Settings) -> LocalImageStorage | S3ImageStorage:
    """The configured backend. Raises StorageConfigError for missing or bad s3 settings."""
    if settings.storage_backend == "s3":
        return S3ImageStorage(
            build_s3_store(settings),
            max_bytes=settings.upload_quota_bytes,
            max_files=settings.upload_quota_files,
        )
    return LocalImageStorage(
        settings.upload_dir,
        max_bytes=settings.upload_quota_bytes,
        max_files=settings.upload_quota_files,
    )
