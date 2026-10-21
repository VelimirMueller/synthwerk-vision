import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from synthwerk_vision.main import create_app
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.storage import LocalImageStorage

# A tiny tree whose roll-ups can be checked by hand:
#   animal ─ mammal ─ dog: beagle (0), husky (1)      vehicle ─ bicycle: mountain bike (3)
#          └ mammal: tabby (2)                         landscape: cliff (4)
SMALL_TAXONOMY: dict[str, Any] = {
    "version": 7,
    "topics": [
        {"id": "animal", "parent": None},
        {"id": "mammal", "parent": "animal"},
        {"id": "dog", "parent": "mammal"},
        {"id": "vehicle", "parent": None},
        {"id": "bicycle", "parent": "vehicle"},
        {"id": "landscape", "parent": None},
    ],
    "labels": [
        {"id": "n1", "name": "beagle", "topic": "dog", "index": 0},
        {"id": "n2", "name": "husky", "topic": "dog", "index": 1},
        {"id": "n3", "name": "tabby", "topic": "mammal", "index": 2},
        {"id": "n4", "name": "mountain bike", "topic": "bicycle", "index": 3},
        {"id": "n5", "name": "cliff", "topic": "landscape", "index": 4},
    ],
}


class FakeClassifier:
    model_name = "fake-model"
    num_classes = 5

    def __init__(self, probabilities: list[float] | None = None) -> None:
        self.probabilities = np.array(
            probabilities or [0.5, 0.3, 0.1, 0.06, 0.04], dtype=np.float32
        )

    def predict(self, data: bytes) -> npt.NDArray[np.float32]:
        return self.probabilities


def make_image(fmt: str = "PNG", size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 200, 120)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def taxonomy_file(tmp_path: Path) -> Path:
    path = tmp_path / "taxonomy.yaml"
    path.write_text(yaml.safe_dump(SMALL_TAXONOMY))
    return path


@pytest.fixture
def settings(tmp_path: Path, taxonomy_file: Path) -> Settings:
    return Settings(
        upload_dir=tmp_path / "uploads",
        max_upload_bytes=64 * 1024,
        model_path=tmp_path / "missing.onnx",
        siglip_dir=tmp_path / "missing-siglip",
        label_cache_dir=tmp_path / "label-cache",
        taxonomy_path=taxonomy_file,
        top_k=3,
    )


def _client(settings: Settings, **kwargs: object) -> Iterator[TestClient]:
    app = create_app(settings, storage=LocalImageStorage(settings.upload_dir), **kwargs)  # type: ignore[arg-type]
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings, classifier=FakeClassifier())


@pytest.fixture
def client_without_model(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings)
