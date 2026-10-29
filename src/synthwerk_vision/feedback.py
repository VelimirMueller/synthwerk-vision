"""Corrections from users, recalled when the same picture comes back.

A correction is keyed by the picture's 64-bit difference hash, not by a semantic embedding:
SigLIP embeddings cannot tell "the same photo, resized" (cosine down to 0.87 on the eval set)
from "another photo of the same kind of thing" (up to 0.90), so an embedding threshold would
either miss re-uploads or leak one image's correction onto other images. dHash separates the
two: re-encodes and resizes differ by at most 6 bits, distinct images by at least 15.
"""

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from synthwerk_vision.images import dhash_distance
from synthwerk_vision.runtime_labels import signature

MAX_DISTANCE = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    dhash TEXT NOT NULL,
    label_id TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Feedback:
    id: int
    dhash: str
    label_id: str


@dataclass(frozen=True)
class Recall:
    feedback: Feedback
    distance: int


class FeedbackStore:
    """SQLite on disk, hashes in memory; safe to share between request threads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(_SCHEMA)
        # Copy-on-write: readers iterate an immutable snapshot, add() swaps in a new one.
        self._entries: tuple[Feedback, ...] = ()
        self._signature: tuple[int, int] | None = None
        self.refresh()

    def refresh(self) -> None:
        """Reload when the database changed on disk, e.g. written by another worker process."""
        current = signature(self._path)
        if current == self._signature:
            return
        with self._lock:
            # Take the signature before the query: a row written in between then only
            # costs one extra reload, instead of hiding behind an already-current signature.
            current = signature(self._path)
            with closing(self._connect()) as db:
                rows = db.execute("SELECT id, dhash, label_id FROM feedback ORDER BY id").fetchall()
            self._entries = tuple(Feedback(*row) for row in rows)
            self._signature = current

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, image_sha256: str, dhash: str, label_id: str) -> Feedback:
        # One critical section for insert, commit and snapshot update, so a concurrent
        # refresh() can neither miss the new row nor hold it twice.
        with self._lock:
            with closing(self._connect()) as db, db:
                cursor = db.execute(
                    "INSERT INTO feedback (created_at, image_sha256, dhash, label_id) "
                    "VALUES (?, ?, ?, ?)",
                    (datetime.now(UTC).isoformat(), image_sha256, dhash, label_id),
                )
            entry = Feedback(int(cursor.lastrowid or 0), dhash, label_id)
            self._entries = (*self._entries, entry)
            # Force one reload on the next refresh(). A signature read here would also cover
            # a row that another worker wrote after our commit and hide it until the next write.
            self._signature = None
        return entry

    def recall(self, dhash: str, valid_label_ids: set[str]) -> Recall | None:
        """The closest correction for this picture; the newest one wins a tie.

        Corrections for labels that no longer exist (deleted runtime labels) are skipped.
        """
        best: Recall | None = None
        for entry in self._entries:  # one snapshot for the whole scan
            if entry.label_id not in valid_label_ids:
                continue
            distance = dhash_distance(dhash, entry.dhash)
            if distance <= MAX_DISTANCE and (best is None or distance <= best.distance):
                best = Recall(entry, distance)
        return best

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)
