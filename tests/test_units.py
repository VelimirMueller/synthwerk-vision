import io
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from synthwerk_vision.classifier import ModelNotAvailableError, OnnxClassifier, preprocess, softmax
from synthwerk_vision.images import InvalidImageError, validate_image
from synthwerk_vision.service import ClassificationService
from synthwerk_vision.taxonomy import Taxonomy
from tests.conftest import make_image

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL = MODELS_DIR / "mobilenetv2-12.onnx"


@pytest.mark.parametrize("size", [(32, 24), (640, 480), (300, 1000)])
def test_preprocess_produces_normalized_nchw_tensor(size: tuple[int, int]) -> None:
    tensor = preprocess(make_image(size=size))

    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.dtype == np.float32


def test_softmax_is_a_probability_distribution() -> None:
    probabilities = softmax(np.array([1.0, 2.0, 1000.0], dtype=np.float32))

    assert probabilities.sum() == pytest.approx(1.0)
    assert int(probabilities.argmax()) == 2


def test_validate_image_rejects_pixel_bombs() -> None:
    with pytest.raises(InvalidImageError, match="dimensions"):
        validate_image(make_image(size=(200, 200)), max_pixels=10_000)


def test_classifier_reports_missing_model(tmp_path: Path) -> None:
    with pytest.raises(ModelNotAvailableError, match="fetch_model"):
        OnnxClassifier(tmp_path / "none.onnx")


needs_model = pytest.mark.skipif(not MODEL.is_file(), reason="run scripts/fetch_model.py first")


def test_preprocess_applies_exif_orientation() -> None:
    # Stored landscape: red left half, blue right half. EXIF 6 = display rotated 90 deg CW,
    # so after transposing, red must be on top and blue at the bottom.
    img = Image.new("RGB", (400, 100), (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, 200, 100))
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif)

    red = preprocess(buffer.getvalue())[0, 0]

    assert red[:50].mean() > red[-50:].mean()


@needs_model
def test_onnx_classifier_end_to_end_with_packaged_taxonomy() -> None:
    classifier = OnnxClassifier(MODEL)
    service = ClassificationService(
        classifier, Taxonomy.load(), top_k=5, topic_min_score=0.05, uncertain_below=0.5
    )

    result = service.classify(make_image("JPEG", (320, 240)))

    assert classifier.model_name == "mobilenetv2-12"
    assert classifier.num_classes == 1000
    assert len(result.labels) == 5
    assert [label.score for label in result.labels] == sorted(
        (label.score for label in result.labels), reverse=True
    )
    roots = [t for t in service.taxonomy.topics.values() if t.parent is None]
    rolled = service.taxonomy.rollup(classifier.predict(make_image("JPEG", (320, 240))))
    assert sum(rolled[t.id] for t in roots) == pytest.approx(1.0, abs=1e-4)


def test_sessions_never_busy_spin() -> None:
    from synthwerk_vision.classifier import session_options

    options = session_options(threads=3)

    assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    assert options.get_session_config_entry("session.inter_op.allow_spinning") == "0"
    assert options.intra_op_num_threads == 3


def test_feedback_refresh_loads_a_row_written_during_the_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthwerk_vision.feedback import FeedbackStore

    path = tmp_path / "feedback.sqlite3"
    worker_a, worker_b = FeedbackStore(path), FeedbackStore(path)
    worker_b.add("sha-1", "0" * 16, "label-1")
    connect = worker_a._connect

    class WriteAfterSelect:
        """Worker B writes right after worker A's SELECT, before A reads the signature."""

        def __init__(self) -> None:
            self._db = connect()

        def execute(self, sql: str) -> "WriteAfterSelect":
            self._rows = self._db.execute(sql).fetchall()
            worker_b.add("sha-2", "f" * 16, "label-2")
            return self

        def fetchall(self) -> list[tuple[int, str, str]]:
            return self._rows

        def close(self) -> None:
            self._db.close()

    monkeypatch.setattr(worker_a, "_connect", WriteAfterSelect)
    worker_a.refresh()
    monkeypatch.setattr(worker_a, "_connect", connect)
    assert len(worker_a) == 1, "the row written during the reload is not in this snapshot"

    worker_a.refresh()

    assert len(worker_a) == 2, "the next refresh() must load the row written during the reload"


def test_feedback_refresh_loads_a_row_written_right_after_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthwerk_vision.feedback import FeedbackStore

    path = tmp_path / "feedback.sqlite3"
    worker_a, worker_b = FeedbackStore(path), FeedbackStore(path)
    connect = worker_a._connect

    class WriteAfterCommit:
        """Worker B writes right after worker A's commit, before A's add() returns."""

        def __init__(self) -> None:
            self._db = connect()

        def __enter__(self) -> "WriteAfterCommit":
            self._db.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._db.__exit__(*exc)  # type: ignore[arg-type]
            worker_b.add("sha-2", "f" * 16, "label-2")

        def execute(self, sql: str, params: tuple[str, ...]) -> sqlite3.Cursor:
            return self._db.execute(sql, params)

        def close(self) -> None:
            self._db.close()

    monkeypatch.setattr(worker_a, "_connect", WriteAfterCommit)
    worker_a.add("sha-1", "0" * 16, "label-1")
    monkeypatch.setattr(worker_a, "_connect", connect)
    assert len(worker_a) == 1, "add() only appends its own row"

    worker_a.refresh()

    assert len(worker_a) == 2, "the next refresh() must load the row written right after add()"
