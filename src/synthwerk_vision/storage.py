"""Image storage. The API depends on the protocol, so an object store can replace the disk."""

import os
import threading
import uuid
from pathlib import Path
from typing import Protocol


class StorageFullError(Exception):
    """The store has reached its byte or file quota; no upload is stored until space is freed."""


class ImageStorage(Protocol):
    def save(self, data: bytes, extension: str) -> str:
        """Persist data and return the server-generated image id.

        Raises StorageFullError when the data would exceed the store's quota.
        """
        ...


class LocalImageStorage:
    """Files on the local disk, bounded by a byte and a file quota for the whole folder.

    Usage is measured from the folder on every save, so files written by other worker
    processes count too. Saves in one process are serialised; two processes can each pass
    the check at the same moment, so the quota can be exceeded by at most one upload per
    other worker.
    """

    def __init__(self, root: Path, *, max_bytes: int, max_files: int) -> None:
        self._root = root.resolve()
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._lock = threading.Lock()

    def save(self, data: bytes, extension: str) -> str:
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            files, used = self._usage()
            if files + 1 > self._max_files or used + len(data) > self._max_bytes:
                raise StorageFullError("upload storage quota reached")
            return self._write(data, extension)

    def _usage(self) -> tuple[int, int]:
        files = used = 0
        with os.scandir(self._root) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    files += 1
                    used += entry.stat(follow_symlinks=False).st_size
        return files, used

    def _write(self, data: bytes, extension: str) -> str:
        image_id = uuid.uuid4().hex
        path = self._root / f"{image_id}.{extension}"
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
        return image_id
