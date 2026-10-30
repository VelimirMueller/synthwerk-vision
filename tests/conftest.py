import io
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import numpy as np
import numpy.typing as npt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pydantic import SecretStr

from synthwerk_vision.main import create_app, runtime_labels
from synthwerk_vision.platform import auth
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.storage import LocalImageStorage
from synthwerk_vision.taxonomy import Taxonomy

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


# Widget token (synthwerk#30) test key and local key set: no network in any test.
ISSUER = "https://identity.test"
AUDIENCE = "synthwerk-api"
JWKS_URL = "https://identity.test/.well-known/jwks.json"
ORIGIN = "http://localhost:5173"
SIGNING_KEY = ec.generate_private_key(ec.SECP256R1())


def public_jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "alg": "ES256", "use": "sig"}


JWKS: dict[str, Any] = {"keys": [public_jwk(SIGNING_KEY, "test-key")]}


def widget_token(
    *,
    key: Any = SIGNING_KEY,
    kid: str = "test-key",
    algorithm: str = "ES256",
    typ: str = "at+jwt",
    lifetime: int = 600,
    issued: float | None = None,
    drop: tuple[str, ...] = (),
    **claims: Any,
) -> str:
    """A widget token like identity issues one; `claims` override or add, `drop` removes."""
    now = int(time.time() if issued is None else issued)
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUDIENCE],
        "sub": "user-1",
        auth.CLAIM_ORG_ID: "org-1",
        auth.CLAIM_CLIENT_ID: "app-1",
        auth.CLAIM_ORIGIN: ORIGIN,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "jti": str(uuid.uuid4()),
        **claims,
    }
    for name in drop:
        payload.pop(name)
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid, "typ": typ})


def widget_headers(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or widget_token()}", "Origin": ORIGIN}


@pytest.fixture(autouse=True)
def widget_auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every app trusts the local test issuer; the key set comes from memory, not the network.
    Returns the served key set: a test may change it (key rotation)."""
    served: dict[str, Any] = {"keys": list(JWKS["keys"])}

    async def fetch(url: str) -> dict[str, Any]:
        assert url == JWKS_URL
        return served

    monkeypatch.setenv("SYNTHWERK_VISION_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SYNTHWERK_VISION_JWKS_URL", JWKS_URL)
    monkeypatch.setattr(auth, "fetch_jwks", fetch)
    return served


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
    storage = LocalImageStorage(
        settings.upload_dir,
        max_bytes=settings.upload_quota_bytes,
        max_files=settings.upload_quota_files,
        max_total_bytes=settings.upload_quota_total_bytes,
    )
    app = create_app(settings, storage=storage, **kwargs)  # type: ignore[arg-type]
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers=widget_headers()) as client:
        yield client


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings, classifier=FakeClassifier())


@pytest.fixture
def client_without_model(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings)


# Admin routes and open-vocabulary models (tests/test_admin.py, tests/test_label_sets.py).
TOKEN = "s3cret-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class OpenFakeClassifier:
    """Open vocabulary stand-in: any label set, puts most mass on `favourite` if present."""

    model_name = "open-fake"

    def __init__(self, taxonomy: Taxonomy, favourite: str = "beagle") -> None:
        self.taxonomy = taxonomy
        self.favourite = favourite
        self.num_classes = len(taxonomy.labels)
        self.relabels = 0

    def predict(self, data: bytes) -> npt.NDArray[np.float32]:
        p = np.full(self.num_classes, 0.1 / max(self.num_classes - 1, 1), dtype=np.float32)
        for label in self.taxonomy.labels:
            if label.name == self.favourite:
                p[:] = 0.1 / max(self.num_classes - 1, 1)
                p[label.index] = 0.9
        return p / p.sum()

    def for_taxonomy(self, taxonomy: Taxonomy) -> "OpenFakeClassifier":
        new = OpenFakeClassifier(taxonomy, self.favourite)
        new.relabels = self.relabels + 1
        return new


def picture(seed: int, size: tuple[int, int] = (320, 240)) -> bytes:
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x, y = int(rng.integers(0, size[0])), int(rng.integers(0, size[1]))
        r, g, b = (int(c) for c in rng.integers(0, 255, 3))
        draw.rectangle((x, y, x + 60, y + 40), fill=(r, g, b))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


def resized(data: bytes, width: int) -> bytes:
    original = Image.open(io.BytesIO(data))
    image = original.resize((width, round(original.height * width / original.width)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=70)
    return out.getvalue()


@pytest.fixture
def admin_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"admin_token": SecretStr(TOKEN), "data_dir": tmp_path / "data"}
    )


def open_client(settings: Settings) -> Iterator[TestClient]:
    # Like build_service: the classifier is built for the taxonomy the app will load (overlay).
    taxonomy = runtime_labels(settings).load(lambda: Taxonomy.load(settings.taxonomy_path))
    app = create_app(
        settings,
        storage=LocalImageStorage(
            settings.upload_dir,
            max_bytes=settings.upload_quota_bytes,
            max_files=settings.upload_quota_files,
            max_total_bytes=settings.upload_quota_total_bytes,
        ),
        classifier=OpenFakeClassifier(taxonomy),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers=widget_headers()) as client:
        yield client


@pytest.fixture
def admin_client(admin_settings: Settings) -> Iterator[TestClient]:
    yield from open_client(admin_settings)


def classify(client: TestClient, data: bytes) -> dict[str, object]:
    response = client.post("/v1/classifications", files={"image": ("a.jpg", data, "image/jpeg")})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body
