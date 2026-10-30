from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synthwerk_vision.main import create_app
from synthwerk_vision.platform.config import Settings, get_settings
from tests.conftest import FakeClassifier, make_image, widget_headers


def files_in(upload_dir: Path) -> list[Path]:
    return sorted(p for p in upload_dir.rglob("*") if p.is_file())


def test_readyz_reports_model_state(client: TestClient, client_without_model: TestClient) -> None:
    ready = client.get("/readyz")
    not_ready = client_without_model.get("/readyz")

    assert (ready.status_code, ready.json()["status"]) == (200, "ok")
    assert [(c["name"], c["status"]) for c in ready.json()["checks"]] == [("model", "ok")]
    assert (not_ready.status_code, not_ready.json()["status"]) == (503, "down")
    assert not_ready.json()["checks"][0]["detail"] == "model not loaded"


def test_upload_stores_image_under_generated_id(client: TestClient, settings: Settings) -> None:
    response = client.post("/v1/images", files={"image": ("photo.png", make_image(), "image/png")})

    assert response.status_code == 201
    body = response.json()
    assert body["content_type"] == "image/png"
    assert (body["width"], body["height"]) == (32, 24)
    [path] = files_in(settings.upload_dir)
    assert path.name == f"{body['id']}.png"
    assert path.relative_to(settings.upload_dir).parts[0] == "org-1"  # the token's org


def test_upload_quota_reached_next_upload_refused_507(settings: Settings) -> None:
    image = make_image()
    full = settings.model_copy(update={"upload_quota_bytes": len(image) * 2})

    app = create_app(full, classifier=FakeClassifier())  # storage built from the settings
    app.dependency_overrides[get_settings] = lambda: full
    with TestClient(app, headers=widget_headers()) as client:
        stored = [client.post("/v1/images", files={"image": ("a.png", image, "image/png")})]
        stored.append(client.post("/v1/images", files={"image": ("b.png", image, "image/png")}))
        refused = client.post("/v1/images", files={"image": ("c.png", image, "image/png")})

    assert [r.status_code for r in stored] == [201, 201]
    assert refused.status_code == 507
    assert refused.json() == {"detail": "upload storage quota reached"}
    assert len(files_in(settings.upload_dir)) == 2


@pytest.mark.parametrize(
    "filename", ["../../evil.png", "/etc/passwd", "..\\..\\evil.png", "uploads/../../x.py"]
)
def test_client_filename_never_reaches_the_filesystem(
    client: TestClient, settings: Settings, tmp_path: Path, taxonomy_file: Path, filename: str
) -> None:
    response = client.post("/v1/images", files={"image": (filename, make_image(), "image/png")})

    assert response.status_code == 201
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p != taxonomy_file]
    assert written == files_in(settings.upload_dir)
    assert [p.name for p in written] == [f"{response.json()['id']}.png"]


def test_extension_comes_from_decoded_format_not_from_client(
    client: TestClient, settings: Settings
) -> None:
    jpeg = make_image("JPEG")
    response = client.post("/v1/images", files={"image": ("shell.php", jpeg, "image/png")})

    assert response.status_code == 201
    assert response.json()["content_type"] == "image/jpeg"
    assert [p.name for p in files_in(settings.upload_dir)] == [f"{response.json()['id']}.jpg"]


def test_non_image_is_rejected(client: TestClient, settings: Settings) -> None:
    payload = b"<?php system($_GET['c']); ?>"
    response = client.post("/v1/images", files={"image": ("a.png", payload, "image/png")})

    assert response.status_code == 415
    assert not settings.upload_dir.exists()


def test_unsupported_format_is_rejected(client: TestClient) -> None:
    gif = make_image("GIF")
    response = client.post("/v1/images", files={"image": ("a.gif", gif, "image/gif")})

    assert response.status_code == 415
    assert "unsupported image format" in response.json()["detail"]


def test_empty_upload_is_rejected(client: TestClient) -> None:
    response = client.post("/v1/images", files={"image": ("a.png", b"", "image/png")})

    assert response.status_code == 415


def test_oversized_upload_is_rejected(client: TestClient, settings: Settings) -> None:
    payload = b"\x89PNG" + b"\0" * settings.max_upload_bytes
    response = client.post("/v1/images", files={"image": ("big.png", payload, "image/png")})

    assert response.status_code == 413
    assert not settings.upload_dir.exists()


def test_classification_returns_labels_paths_and_topics(
    client: TestClient, settings: Settings
) -> None:
    response = client.post(
        "/v1/classifications", files={"image": ("a.webp", make_image("WEBP"), "image/webp")}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["primary"] == {
        "id": "n1",
        "label": "beagle",
        "score": 0.5,
        "topic": "dog",
        "path": ["animal", "mammal", "dog", "beagle"],
    }
    assert [label["label"] for label in body["labels"]] == ["beagle", "husky", "tabby"]
    # animal = beagle + husky + tabby; dog = beagle + husky; landscape (0.04) is below 0.05.
    assert body["topic"] == {"id": "animal", "parent": None, "depth": 0, "score": 0.9}
    assert {t["id"]: t["score"] for t in body["topics"]} == {
        "animal": 0.9,
        "mammal": 0.9,
        "dog": 0.8,
        "vehicle": 0.06,
        "bicycle": 0.06,
    }
    assert body["uncertain"] is False
    assert body["model"] == {"name": "fake-model", "taxonomy_version": 7}
    assert len(body["image"]["sha256"]) == 64
    assert set(body["timings_ms"]) == {"inference", "rollup", "total"}
    assert not settings.upload_dir.exists(), "classification must not persist the image"


def test_topics_are_sorted_best_first_with_parents_before_equal_children(
    client: TestClient,
) -> None:
    body = client.post(
        "/v1/classifications", files={"image": ("a.png", make_image(), "image/png")}
    ).json()

    assert [t["id"] for t in body["topics"]] == ["animal", "mammal", "dog", "vehicle", "bicycle"]


def test_classification_without_model_is_503(client_without_model: TestClient) -> None:
    response = client_without_model.post(
        "/v1/classifications", files={"image": ("a.png", make_image(), "image/png")}
    )

    assert response.status_code == 503


def test_cors_allows_only_configured_origins(client: TestClient) -> None:
    allowed = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    denied = client.get("/healthz", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers
    assert "access-control-allow-credentials" not in allowed.headers


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    ok = client.post("/v1/classifications", files={"image": ("a.png", make_image(), "image/png")})
    rejected = client.post("/v1/images", files={"image": ("a.png", b"nope", "image/png")})

    assert ok.json()["request_id"] == ok.headers["x-request-id"]
    assert len(rejected.headers["x-request-id"]) == 32


@pytest.mark.parametrize(
    ("incoming", "kept"), [("trace-123.abc", True), ("bad id\r\nx", False), ("x" * 65, False)]
)
def test_caller_request_id_is_kept_only_when_safe(
    client: TestClient, incoming: str, kept: bool
) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": incoming})

    assert (response.headers["x-request-id"] == incoming) is kept


def test_mobilenet_backend_without_weights_degrades_to_503(settings: Settings) -> None:
    from synthwerk_vision.main import create_app

    mobilenet = settings.model_copy(update={"backend": "mobilenet", "taxonomy_path": None})
    with TestClient(create_app(mobilenet)) as client:
        assert client.get("/readyz").status_code == 503
