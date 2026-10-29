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
from PIL import Image

from synthwerk_vision.main import create_app
from synthwerk_vision.platform import auth
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
