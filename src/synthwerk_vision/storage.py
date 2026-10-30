"""Image storage. The API depends on the protocol; the backend comes from the settings.

Both backends store an image under the same key, `<org>/<yyyy>/<mm>/<uuid4>.<ext>`, and keep
a byte and a file quota per org plus a byte cap for the whole store.

- `LocalImageStorage`: files on the local disk. The default, for local development.
- `S3ImageStorage`: an S3-compatible bucket (Supabase Storage in dev, stg and prd, #77).
"""

import logging
import os
import re
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
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

        Raises StorageFullError when the data would exceed the org's quota or the total cap, and
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


@dataclass
class _Usage:
    files: int
    bytes: int
    measured_at: float


class _OrgLock:
    """A lock that a WeakValueDictionary can hold: it goes away when no save uses it."""

    __slots__ = ("__weakref__", "lock")

    def __init__(self) -> None:
        self.lock = threading.Lock()


class _QuotaStorage:
    """Quota logic shared by both backends: a byte and a file quota per org, and a byte cap
    for the whole store.

    - Usage lives in counters per process. A counter starts from one scan (a folder walk or a
      prefix listing) and is scanned again when it is older than `REFRESH_S`.
    - Near a limit, the counter is scanned again before every write: when the cached usage
      plus the upload plus a margin (`margin_bytes`, `margin_files`: one upload per other
      writer) passes the limit. So far from a limit a save costs no listing, and near a limit
      it sees what other workers and instances wrote.
    - Residual race: N writers can each pass the check at the same moment, so a quota can be
      exceeded by at most N - 1 uploads (as in #36).
    - Each org has its own lock, held across the scan and the write of that org only. The
      total cap is reserved under a short lock with no I/O, so a slow write for one org never
      blocks another org.
    - Reserved bytes of writes in flight live in their own counter, not in the scanned total.
      A rescan during a write therefore never drops or double-releases a reservation.
    """

    REFRESH_S = 10.0

    def __init__(
        self,
        *,
        max_bytes: int,
        max_files: int,
        max_total_bytes: int,
        margin_bytes: int = 0,
        margin_files: int = 0,
        clock: Clock = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._margin_bytes = margin_bytes
        self._margin_files = margin_files
        self._clock = clock
        self._monotonic = monotonic
        self._org_locks: weakref.WeakValueDictionary[str, _OrgLock] = weakref.WeakValueDictionary()
        self._org_usage: dict[str, _Usage] = {}
        self._total: _Usage | None = None  # scanned bytes plus completed writes
        self._in_flight = 0  # bytes reserved by writes that have not ended yet
        self._lock = threading.Lock()  # guards the dicts and counters; never held across I/O
        # Serialises the total scan, so two scans never race. Never held across a write.
        self._total_scan_lock = threading.Lock()

    def save(self, data: bytes, *, org_id: str, extension: str, content_type: str) -> str:
        image_id = uuid.uuid4().hex
        key = object_key(org_id, image_id, extension, self._clock())
        size = len(data)
        with self._lock:
            org_lock = self._org_locks.get(org_id)
            if org_lock is None:
                org_lock = self._org_locks[org_id] = _OrgLock()
        with org_lock.lock:
            org = self._org_counter(org_id, size)
            if org.files + 1 > self._max_files or org.bytes + size > self._max_bytes:
                raise StorageFullError("upload storage quota reached")
            self._reserve_total(size)
            try:
                self._put(key, data, content_type)
            finally:
                with self._lock:
                    self._in_flight -= size
            with self._lock:
                org.files += 1
                org.bytes += size
                if self._total is not None:
                    self._total.bytes += size
        return image_id

    def _org_near_limit(self, usage: _Usage, size: int) -> bool:
        return (
            usage.files + 1 + self._margin_files > self._max_files
            or usage.bytes + size + self._margin_bytes > self._max_bytes
        )

    def _org_counter(self, org_id: str, size: int) -> _Usage:
        """The org's counter; called with the org lock held, so one scan per org at a time."""
        with self._lock:
            usage = self._org_usage.get(org_id)
        if usage is None or self._stale(usage) or self._org_near_limit(usage, size):
            files, used = self._scan(f"{org_id}/", self._max_files, self._max_bytes)
            usage = _Usage(files, used, self._monotonic())
            with self._lock:
                # Drop counters of orgs with no upload for a long time; they are scanned again.
                for idle in [o for o, u in self._org_usage.items() if self._idle(u)]:
                    del self._org_usage[idle]
                self._org_usage[org_id] = usage
        return usage

    def _reserve_total(self, size: int) -> None:
        with self._lock:
            seen = self._total
            needs_scan = (
                seen is None
                or self._stale(seen)
                or seen.bytes + self._in_flight + size + self._margin_bytes > self._max_total_bytes
            )
        if needs_scan:
            with self._total_scan_lock:
                with self._lock:
                    replaced = self._total is not seen  # another thread scanned meanwhile
                if not replaced:
                    _, used = self._scan(None, None, self._max_total_bytes)
                    with self._lock:
                        self._total = _Usage(0, used, self._monotonic())
        with self._lock:
            total = self._total
            assert total is not None  # noqa: S101 (set above)
            if total.bytes + self._in_flight + size > self._max_total_bytes:
                raise StorageFullError("upload storage quota reached")
            self._in_flight += size

    def _stale(self, usage: _Usage) -> bool:
        return self._monotonic() - usage.measured_at > self.REFRESH_S

    def _idle(self, usage: _Usage) -> bool:
        return self._monotonic() - usage.measured_at > 60 * self.REFRESH_S

    def _scan(
        self, prefix: str | None, stop_files: int | None, stop_bytes: int
    ) -> tuple[int, int]:  # pragma: no cover - each backend overrides it
        raise NotImplementedError

    def _put(self, key: str, data: bytes, content_type: str) -> None:  # pragma: no cover
        raise NotImplementedError


class LocalImageStorage(_QuotaStorage):
    """Files on the local disk under `<root>/<key>`. The default, for local development.

    Files open with `O_EXCL`, so an upload never overwrites a file; a failed write leaves no
    partial file.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        max_files: int,
        max_total_bytes: int,
        margin_bytes: int = 0,
        margin_files: int = 0,
        clock: Clock = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            max_bytes=max_bytes,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            margin_bytes=margin_bytes,
            margin_files=margin_files,
            clock=clock,
            monotonic=monotonic,
        )
        self._root = root.resolve()

    def _scan(self, prefix: str | None, stop_files: int | None, stop_bytes: int) -> tuple[int, int]:
        folder = self._root / prefix if prefix else self._root
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
                if (stop_files is not None and files >= stop_files) or used >= stop_bytes:
                    return files, used  # the quota is reached; the rest cannot change it
        return files, used

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
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


class S3ImageStorage(_QuotaStorage):
    """Objects in an S3-compatible bucket (Supabase Storage in dev, stg and prd, #77).

    - Counters start from a listing (the org prefix, or the whole bucket for the total) and
      are listed again after `REFRESH_S` or near a limit, not on every save. No usage table
      to keep in step. A listing page holds 1000 keys and stops once the quota is reached.
    - Objects get the content type of the decoded image and no ACL: they stay as private as
      the bucket.
    """

    LIST_PAGE = 1000

    def __init__(
        self,
        store: S3Store,
        *,
        max_bytes: int,
        max_files: int,
        max_total_bytes: int,
        margin_bytes: int = 0,
        margin_files: int = 0,
        clock: Clock = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            max_bytes=max_bytes,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            margin_bytes=margin_bytes,
            margin_files=margin_files,
            clock=clock,
            monotonic=monotonic,
        )
        self._store = store

    def save(self, data: bytes, *, org_id: str, extension: str, content_type: str) -> str:
        try:
            return super().save(data, org_id=org_id, extension=extension, content_type=content_type)
        except (ObjectStoreError, OSError) as exc:
            logger.warning(
                "object store request failed",
                extra={"event.name": "vision.storage.failed", "error.type": type(exc).__name__},
            )
            raise StorageUnavailableError("upload storage is not available") from exc

    def _scan(self, prefix: str | None, stop_files: int | None, stop_bytes: int) -> tuple[int, int]:
        files = used = 0
        for chunk in obstore.list(self._store, prefix=prefix, chunk_size=self.LIST_PAGE):
            files += len(chunk)
            used += sum(meta["size"] for meta in chunk)
            if (stop_files is not None and files >= stop_files) or used >= stop_bytes:
                break  # the quota is reached; the rest cannot change the answer
        return files, used

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        obstore.put(
            self._store,
            key,
            data,
            attributes={"Content-Type": content_type},
            use_multipart=False,
        )

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
    # Near a limit, scan again before the write: one upload of margin per other writer.
    margin_bytes = settings.max_upload_bytes * settings.upload_writers
    margin_files = settings.upload_writers
    if settings.storage_backend == "s3":
        return S3ImageStorage(
            build_s3_store(settings),
            max_bytes=settings.upload_quota_bytes,
            max_files=settings.upload_quota_files,
            max_total_bytes=settings.upload_quota_total_bytes,
            margin_bytes=margin_bytes,
            margin_files=margin_files,
        )
    return LocalImageStorage(
        settings.upload_dir,
        max_bytes=settings.upload_quota_bytes,
        max_files=settings.upload_quota_files,
        max_total_bytes=settings.upload_quota_total_bytes,
        margin_bytes=margin_bytes,
        margin_files=margin_files,
    )
