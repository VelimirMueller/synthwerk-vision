"""Label sets per org (synthwerk#44): a default set and one set per org, on the same base.

    $DATA_DIR/label-sets/default.yaml          the default set (was $DATA_DIR/labels.yaml)
    $DATA_DIR/label-sets/orgs/<org id>.yaml    the own set of one org

- An org without a file uses the default set.
- The first change of an org copies the default set into the org file. From then on, the org
  set and the default set change independently.
- One lock file serialises every write, so the copy reads a default set that does not change
  under it.
- The org id is the Zitadel org id of the caller (`urn:zitadel:iam:user:resourceowner:id`). It
  becomes a file name, so it must match `ORG_ID_PATTERN`.
"""

import fcntl
import logging
import os
import re
from pathlib import Path

from synthwerk_vision.runtime_labels import RuntimeLabels

logger = logging.getLogger("synthwerk_vision")

ORG_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_ORG_ID = re.compile(ORG_ID_PATTERN)


class LabelSets:
    def __init__(self, data_dir: Path, base_ref: str) -> None:
        self.data_dir = data_dir
        self.root = data_dir / "label-sets"
        self.base_ref = base_ref
        self.lock = self.root / ".lock"
        self.default = RuntimeLabels(self.root / "default.yaml", base_ref, lock=self.lock)

    def org(self, org_id: str) -> RuntimeLabels:
        """The set of one org. Raises ValueError for an id that is not a safe file name."""
        if not _ORG_ID.fullmatch(org_id):
            raise ValueError("org id does not match ORG_ID_PATTERN")
        return RuntimeLabels(
            self.root / "orgs" / f"{org_id}.yaml", self.base_ref, lock=self.lock, seed=self.default
        )

    def migrate(self) -> bool:
        """Move the global overlay of older versions ($DATA_DIR/labels.yaml) to the default set.

        Runs at startup in every worker; the lock and the existence checks make it run once.
        Returns True when this call moved the file.
        """
        legacy = self.data_dir / "labels.yaml"
        if not legacy.is_file():
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if not legacy.is_file():
                    return False
                if self.default.overlay.exists():
                    logger.warning(
                        "legacy labels.yaml kept: the default label set exists already",
                        extra={"event.name": "vision.labels.migration_skipped"},
                    )
                    return False
                os.replace(legacy, self.default.overlay)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        logger.info(
            "global runtime labels moved to the default label set",
            extra={"event.name": "vision.labels.migrated"},
        )
        return True
