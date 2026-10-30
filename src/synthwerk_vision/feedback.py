"""Corrections from users, recalled when the same picture comes back.

A correction is keyed by the picture's 64-bit difference hash, not by a semantic embedding:
SigLIP embeddings cannot tell "the same photo, resized" (cosine down to 0.87 on the eval set)
from "another photo of the same kind of thing" (up to 0.90), so an embedding threshold would
either miss re-uploads or leak one image's correction onto other images. dHash separates the
two: re-encodes and resizes differ by at most 6 bits, distinct images by at least 15.

Corrections belong to one org (synthwerk#44). A row with the org id "" is a default correction:
it was written through the platform route `POST /v1/feedback`, or before org scoping existed.
An org sees its own corrections first and default corrections only when it has none for the
picture. It never sees the corrections of another org.
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
    label_id TEXT NOT NULL,
    org_id TEXT NOT NULL DEFAULT ''
)
"""
DEFAULT_ORG = ""


@dataclass(frozen=True)
class Feedback:
    id: int
    dhash: str
    label_id: str
    org_id: str = DEFAULT_ORG


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
            columns = {row[1] for row in db.execute("PRAGMA table_info(feedback)")}
            if "org_id" not in columns:  # a database of an older version: rows become default
                db.execute("ALTER TABLE feedback ADD COLUMN org_id TEXT NOT NULL DEFAULT ''")
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
                rows = db.execute(
                    "SELECT id, dhash, label_id, org_id FROM feedback ORDER BY id"
                ).fetchall()
            self._entries = tuple(Feedback(*row) for row in rows)
            self._signature = current

    def __len__(self) -> int:
        return len(self._entries)

    def add(
        self, image_sha256: str, dhash: str, label_id: str, org_id: str = DEFAULT_ORG
    ) -> Feedback:
        # One critical section for insert, commit and snapshot update, so a concurrent
        # refresh() can neither miss the new row nor hold it twice.
        with self._lock:
            with closing(self._connect()) as db, db:
                cursor = db.execute(
                    "INSERT INTO feedback (created_at, image_sha256, dhash, label_id, org_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (datetime.now(UTC).isoformat(), image_sha256, dhash, label_id, org_id),
                )
            entry = Feedback(int(cursor.lastrowid or 0), dhash, label_id, org_id)
            self._entries = (*self._entries, entry)
            # Force one reload on the next refresh(). A signature read here would also cover
            # a row that another worker wrote after our commit and hide it until the next write.
            self._signature = None
        return entry

    def recall(
        self, dhash: str, valid_label_ids: set[str], org_id: str = DEFAULT_ORG
    ) -> Recall | None:
        """The closest correction of the org for this picture; the newest one wins a tie.

        Without one, the closest default correction. Corrections for labels that are not in the
        org's label set (deleted runtime labels) are skipped.
        """
        own: Recall | None = None
        fallback: Recall | None = None
        for entry in self._entries:  # one snapshot for the whole scan
            if entry.org_id not in (org_id, DEFAULT_ORG) or entry.label_id not in valid_label_ids:
                continue
            distance = dhash_distance(dhash, entry.dhash)
            if distance > MAX_DISTANCE:
                continue
            if entry.org_id == org_id:
                if own is None or distance <= own.distance:
                    own = Recall(entry, distance)
            elif fallback is None or distance <= fallback.distance:
                fallback = Recall(entry, distance)
        return own or fallback

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)
