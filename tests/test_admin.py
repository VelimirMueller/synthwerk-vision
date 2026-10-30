import time

import pytest
from fastapi.testclient import TestClient

from synthwerk_vision.main import create_app, runtime_labels
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.taxonomy import Taxonomy
from tests.conftest import AUTH, TOKEN, classify, open_client, picture, resized


def test_admin_endpoints_do_not_exist_without_a_token(client: TestClient) -> None:
    assert client.get("/v1/labels", headers=AUTH).status_code == 404
    assert client.post("/v1/labels", json={"name": "x", "topic": "dog"}).status_code == 404


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
def test_admin_endpoints_reject_bad_tokens(
    admin_client: TestClient, headers: dict[str, str]
) -> None:
    response = admin_client.get("/v1/labels", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_runtime_label_is_scored_immediately_and_survives_restart(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    response = admin_client.post(
        "/v1/labels",
        headers=AUTH,
        json={
            "name": "aurora corona",
            "topic": "aurora photo",
            "new_topic": True,
            "topic_parent": "landscape",
            "prompt": "the corona of an aurora",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "id": "runtime:aurora-corona",
        "name": "aurora corona",
        "topic": "aurora photo",
        "prompt": "the corona of an aurora",
        "path": ["landscape", "aurora photo", "aurora corona"],
    }
    listed = admin_client.get("/v1/labels", headers=AUTH).json()
    assert [label["id"] for label in listed["labels"]] == ["runtime:aurora-corona"]
    assert listed["total_labels"] == 6

    overlay = (admin_settings.data_dir / "label-sets" / "default.yaml").read_text()
    assert "extends:" in overlay and "aurora corona" in overlay
    restarted = next(open_client(admin_settings))
    assert restarted.get("/v1/labels", headers=AUTH).json()["total_labels"] == 6


def test_label_change_swaps_in_a_new_service(admin_client: TestClient) -> None:
    before = admin_client.app.state.service  # type: ignore[attr-defined]

    admin_client.post("/v1/labels", headers=AUTH, json={"name": "hut", "topic": "landscape"})

    after = admin_client.app.state.service  # type: ignore[attr-defined]
    assert after is not before
    assert after.classifier.relabels == 1
    assert len(before.taxonomy.labels) == 5, "the old service is never mutated"


def test_bad_label_requests_change_nothing(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    before = admin_client.app.state.service  # type: ignore[attr-defined]

    unknown_topic = admin_client.post(
        "/v1/labels", headers=AUTH, json={"name": "x", "topic": "nope"}
    )
    admin_client.post("/v1/labels", headers=AUTH, json={"name": "hut", "topic": "landscape"})
    duplicate = admin_client.post("/v1/labels", headers=AUTH, json={"name": "Hut", "topic": "dog"})
    no_slug = admin_client.post("/v1/labels", headers=AUTH, json={"name": "!!!", "topic": "dog"})

    assert unknown_topic.status_code == 409 and "unknown topic" in unknown_topic.json()["detail"]
    assert duplicate.status_code == 409 and "already exists" in duplicate.json()["detail"]
    assert no_slug.status_code == 409
    assert before.taxonomy.labels == admin_client.app.state.service.taxonomy.labels[:5]  # type: ignore[attr-defined]
    staged = list((admin_settings.data_dir / "label-sets").glob(".labels-*"))
    assert not staged, "staged files are cleaned up"


def test_delete_only_runtime_labels(admin_client: TestClient) -> None:
    admin_client.post("/v1/labels", headers=AUTH, json={"name": "hut", "topic": "landscape"})

    assert admin_client.delete("/v1/labels/n1", headers=AUTH).status_code == 409
    assert admin_client.delete("/v1/labels/runtime:nope", headers=AUTH).status_code == 404
    assert admin_client.delete("/v1/labels/runtime:hut", headers=AUTH).status_code == 204
    assert admin_client.get("/v1/labels", headers=AUTH).json()["total_labels"] == 5


def test_fixed_label_models_refuse_runtime_labels(admin_settings: Settings) -> None:
    from tests.conftest import FakeClassifier

    app = create_app(admin_settings, classifier=FakeClassifier())
    app.dependency_overrides[get_settings] = lambda: admin_settings
    with TestClient(app) as client:
        response = client.post("/v1/labels", headers=AUTH, json={"name": "hut", "topic": "dog"})

    assert response.status_code == 409
    assert "fixed label set" in response.json()["detail"]


def test_feedback_corrects_the_same_picture_but_not_others(admin_client: TestClient) -> None:
    photo, other = picture(1), picture(2)
    assert classify(admin_client, photo)["correction"] is None

    response = admin_client.post(
        "/v1/feedback",
        headers=AUTH,
        files={"image": ("a.jpg", photo, "image/jpeg")},
        data={"label_id": "n3"},
    )

    assert response.status_code == 201, response.text
    again = classify(admin_client, resized(photo, 200))
    assert again["correction"]["label"]["label"] == "tabby"  # type: ignore[index]
    assert again["correction"]["label"]["path"] == ["animal", "mammal", "tabby"]  # type: ignore[index]
    assert again["primary"]["label"] == "beagle", "model output stays untouched"  # type: ignore[index]
    assert classify(admin_client, other)["correction"] is None


def test_feedback_for_unknown_label_is_rejected(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/v1/feedback",
        headers=AUTH,
        files={"image": ("a.jpg", picture(3), "image/jpeg")},
        data={"label_id": "nope"},
    )

    assert response.status_code == 409


def test_corrections_to_deleted_labels_are_ignored(admin_client: TestClient) -> None:
    photo = picture(4)
    admin_client.post("/v1/labels", headers=AUTH, json={"name": "hut", "topic": "landscape"})
    admin_client.post(
        "/v1/feedback",
        headers=AUTH,
        files={"image": ("a.jpg", photo, "image/jpeg")},
        data={"label_id": "runtime:hut"},
    )
    assert classify(admin_client, photo)["correction"] is not None

    admin_client.delete("/v1/labels/runtime:hut", headers=AUTH)

    assert classify(admin_client, photo)["correction"] is None


def test_label_and_feedback_written_by_another_worker_are_picked_up(
    admin_settings: Settings,
) -> None:
    worker_a = next(open_client(admin_settings))
    worker_b = next(open_client(admin_settings))
    photo = picture(7)

    worker_a.post("/v1/labels", headers=AUTH, json={"name": "hut", "topic": "landscape"})
    worker_a.post(
        "/v1/feedback",
        headers=AUTH,
        files={"image": ("a.jpg", photo, "image/jpeg")},
        data={"label_id": "runtime:hut"},
    )

    assert worker_b.get("/v1/labels", headers=AUTH).json()["total_labels"] == 6
    assert classify(worker_b, photo)["correction"]["label"]["label"] == "hut"  # type: ignore[index]
    before = worker_a.app.state.service  # type: ignore[attr-defined]
    classify(worker_a, photo)
    assert worker_a.app.state.service is before, "a worker does not reload its own write"  # type: ignore[attr-defined]


def test_concurrent_label_writes_from_two_processes_keep_both(admin_settings: Settings) -> None:
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    names = [f"label {i}" for i in range(6)]
    with ctx.Pool(3) as pool:
        pool.starmap(_add_label_in_process, [(admin_settings, name) for name in names])

    labels = runtime_labels(admin_settings).labels()
    assert sorted(item["name"] for item in labels) == names, "no write may be lost"


def _add_label_in_process(settings: Settings, name: str) -> None:
    def build(taxonomy: Taxonomy) -> Taxonomy:
        time.sleep(0.05)  # widen the read-modify-write window, like a real embedding would
        return taxonomy

    runtime_labels(settings).add(name, "landscape", None, None, build)
