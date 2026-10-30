"""Labels added while the server runs, persisted as a taxonomy overlay that `extends` the base.

The overlay is an ordinary taxonomy file, so it survives restarts, can be reviewed and edited
by hand, and goes through the same validation as every other taxonomy. Every change is staged
in a temporary file and only replaces the overlay after the new taxonomy loaded and the new
classifier was built, so a failed change leaves both the file and the running service as is.
"""

import fcntl
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import yaml

from synthwerk_vision.taxonomy import Taxonomy, TaxonomyError

ID_PREFIX = "runtime:"
T = TypeVar("T")


def signature(path: Path) -> tuple[int, int] | None:
    """Cheap change marker for state shared between worker processes: (mtime_ns, size)."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


class RuntimeLabelError(Exception):
    """A request that cannot be applied (unknown label, packaged label, clash)."""


class RuntimeLabels:
    """One label set: an overlay file on top of the base taxonomy.

    A set with a `seed` (an org set seeded by the default set) reads the seed while it has no
    file of its own. Its first change copies the seed's labels into its own file; later changes
    of the seed do not reach it.
    """

    def __init__(
        self,
        overlay: Path,
        base_ref: str,
        *,
        lock: Path | None = None,
        seed: "RuntimeLabels | None" = None,
    ) -> None:
        self.overlay = overlay
        self.base_ref = base_ref  # "package:<file>" or an absolute path
        self.lock = lock or overlay.parent / ".labels.lock"
        self.seed = seed

    @property
    def own(self) -> bool:
        """True when this set has its own file (an org set that was changed at least once)."""
        return self.overlay.is_file()

    def load(self, base: Callable[[], Taxonomy]) -> Taxonomy:
        """The overlay on top of its base. Without a file: the seed, or the base alone."""
        if not self.overlay.is_file():
            return self.seed.load(base) if self.seed else base()
        extends = self._document().get("extends")
        if extends != self.base_ref:
            raise TaxonomyError(
                f"{self.overlay} extends {extends!r} but the configured base is "
                f"{self.base_ref!r}; move or delete the overlay"
            )
        return Taxonomy.load(self.overlay)

    def labels(self) -> list[dict[str, Any]]:
        if self.overlay.is_file():
            return list(self._document().get("labels") or [])
        return self.seed.labels() if self.seed else []

    def add(
        self,
        name: str,
        topic: str,
        prompt: str | None,
        parent: str | None,
        apply: Callable[[Taxonomy], T],
    ) -> tuple[dict[str, Any], T]:
        """Add a label (and its topic, when `parent` is given) and apply the new taxonomy."""
        with self._exclusive():
            return self._add(name, topic, prompt, parent, apply)

    def _add(
        self,
        name: str,
        topic: str,
        prompt: str | None,
        parent: str | None,
        apply: Callable[[Taxonomy], T],
    ) -> tuple[dict[str, Any], T]:
        document = self._start()
        label: dict[str, Any] = {"id": ID_PREFIX + _slug(name), "name": name, "topic": topic}
        if prompt:
            label["prompt"] = prompt
        if any(item["id"] == label["id"] for item in document["labels"]):
            raise RuntimeLabelError(f"label {label['id']!r} already exists")
        if parent is not None:
            document["topics"].append({"id": topic, "parent": parent or None})
        document["labels"].append(label)
        return label, self._stage(document, apply)

    def remove(self, label_id: str, apply: Callable[[Taxonomy], T]) -> T:
        if not label_id.startswith(ID_PREFIX):
            raise RuntimeLabelError(f"{label_id!r} is a packaged label; only runtime labels can go")
        with self._exclusive():
            return self._remove(label_id, apply)

    def _remove(self, label_id: str, apply: Callable[[Taxonomy], T]) -> T:
        document = self._start()
        remaining = [item for item in document["labels"] if item["id"] != label_id]
        if len(remaining) == len(document["labels"]):
            raise KeyError(label_id)
        document["labels"] = remaining
        return self._stage(document, apply)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise read-modify-write of the overlay across worker *processes*; without it two
        workers adding labels at once would each replace the file and one label would be lost."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _stage(self, document: dict[str, Any], apply: Callable[[Taxonomy], T]) -> T:
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.overlay.parent, prefix=".labels-", suffix=".yaml")
        staged = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(document, fh, sort_keys=False, allow_unicode=True)
                fh.flush()
                os.fsync(fh.fileno())  # the rename below must never publish a partial file
            result = apply(Taxonomy.load(staged))  # validates + builds before anything changes
            staged.replace(self.overlay)
            return result
        finally:
            staged.unlink(missing_ok=True)

    def _document(self) -> dict[str, Any]:
        document = yaml.safe_load(self.overlay.read_text(encoding="utf-8")) or {}
        document.setdefault("topics", [])
        document.setdefault("labels", [])
        return document

    def _start(self) -> dict[str, Any]:
        """The document a change starts from: own file, else a copy of the seed, else empty."""
        if self.overlay.is_file():
            return self._document()
        document = self._empty()
        if self.seed is not None and self.seed.overlay.is_file():
            seeded = self.seed._document()
            document["topics"] = list(seeded["topics"])
            document["labels"] = list(seeded["labels"])
        return document

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": "labels added at runtime through POST /v1/labels",
            "extends": self.base_ref,
            "topics": [],
            "labels": [],
        }


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise RuntimeLabelError("label name needs at least one letter or digit")
    return slug
