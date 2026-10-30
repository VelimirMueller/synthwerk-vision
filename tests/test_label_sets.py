"""Label sets and corrections per org (synthwerk#44)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from synthwerk_vision.api.routes import OrgServices
from synthwerk_vision.feedback import FeedbackStore
from synthwerk_vision.label_sets import LabelSets
from synthwerk_vision.main import create_app, label_sets
from synthwerk_vision.platform import auth
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.service import ClassificationService
from synthwerk_vision.storage import LocalImageStorage
from synthwerk_vision.taxonomy import Taxonomy
from tests.conftest import (
    AUTH,
    OpenFakeClassifier,
    picture,
    widget_headers,
    widget_token,
)

ORG_A, ORG_B = "100000000000000001", "100000000000000002"


def org_headers(org_id: str) -> dict[str, str]:
    claims: dict[str, Any] = {auth.CLAIM_ORG_ID: org_id}
    return widget_headers(widget_token(**claims))


def classify_as(client: TestClient, org_id: str, data: bytes) -> dict[str, Any]:
    response = client.post(
        "/v1/classifications",
        headers=org_headers(org_id),
        files={"image": ("a.jpg", data, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def add(client: TestClient, path: str, name: str, topic: str = "landscape") -> None:
    response = client.post(path, headers=AUTH, json={"name": name, "topic": topic})
    assert response.status_code == 201, response.text


def names(client: TestClient, org_id: str) -> list[str]:
    body = client.get(f"/v1/orgs/{org_id}/labels", headers=AUTH).json()
    return [label["name"] for label in body["labels"]]


def hut_client(settings: Settings) -> Iterator[TestClient]:
    """An open-vocabulary app whose model puts most mass on a label named "hut"."""
    taxonomy = label_sets(settings).default.load(lambda: Taxonomy.load(settings.taxonomy_path))
    app = create_app(
        settings,
        storage=LocalImageStorage(
            settings.upload_dir, max_bytes=1 << 20, max_files=100, max_total_bytes=1 << 30
        ),
        classifier=OpenFakeClassifier(taxonomy, favourite="hut"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers=widget_headers()) as client:
        yield client


def test_two_orgs_get_different_labels_for_the_same_image(admin_settings: Settings) -> None:
    client = next(hut_client(admin_settings))
    photo = picture(11)

    add(client, f"/v1/orgs/{ORG_A}/labels", "hut")

    a, b = classify_as(client, ORG_A, photo), classify_as(client, ORG_B, photo)
    assert a["primary"]["label"] == "hut"
    assert b["primary"]["label"] != "hut"
    assert "hut" not in {label["label"] for label in b["labels"]}


def test_org_without_own_set_reads_the_default_set(admin_client: TestClient) -> None:
    add(admin_client, "/v1/labels", "cabin")

    body = admin_client.get(f"/v1/orgs/{ORG_A}/labels", headers=AUTH).json()

    assert body["org_id"] == ORG_A
    assert body["custom"] is False
    assert [label["id"] for label in body["labels"]] == ["runtime:cabin"]
    assert body["total_labels"] == 6
    assert {"id": "dog", "parent": "mammal", "depth": 2} in body["topics"]


def test_first_org_change_copies_the_default_set_then_both_change_alone(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    add(admin_client, "/v1/labels", "cabin")

    add(admin_client, f"/v1/orgs/{ORG_A}/labels", "hut")
    add(admin_client, "/v1/labels", "tent")

    assert names(admin_client, ORG_A) == ["cabin", "hut"]
    assert admin_client.get(f"/v1/orgs/{ORG_A}/labels", headers=AUTH).json()["custom"] is True
    assert names(admin_client, ORG_B) == ["cabin", "tent"], "an org without a set follows default"
    default = admin_client.get("/v1/labels", headers=AUTH).json()
    assert [label["name"] for label in default["labels"]] == ["cabin", "tent"]
    org_file = admin_settings.data_dir / "label-sets" / "orgs" / f"{ORG_A}.yaml"
    assert yaml.safe_load(org_file.read_text())["extends"] == str(
        admin_settings.taxonomy_path.resolve()  # type: ignore[union-attr]
    )


def test_org_delete_rules(admin_client: TestClient) -> None:
    add(admin_client, "/v1/labels", "cabin")
    base = f"/v1/orgs/{ORG_A}/labels"

    assert admin_client.delete(f"{base}/n1", headers=AUTH).status_code == 409
    assert admin_client.delete(f"{base}/runtime:nope", headers=AUTH).status_code == 404
    assert admin_client.delete(f"{base}/runtime:cabin", headers=AUTH).status_code == 204

    assert names(admin_client, ORG_A) == []
    assert names(admin_client, ORG_B) == ["cabin"], "the default set keeps the copied label"


def test_org_label_clash_is_refused(admin_client: TestClient) -> None:
    add(admin_client, f"/v1/orgs/{ORG_A}/labels", "hut")

    response = admin_client.post(
        f"/v1/orgs/{ORG_A}/labels", headers=AUTH, json={"name": "Hut", "topic": "dog"}
    )

    assert response.status_code == 409


@pytest.mark.parametrize("org_id", ["a.b", "a b", "x" * 65, "..%2Fetc"])
def test_org_routes_refuse_ids_that_are_not_safe_file_names(
    admin_client: TestClient, org_id: str
) -> None:
    response = admin_client.get(f"/v1/orgs/{org_id}/labels", headers=AUTH)

    assert response.status_code in (404, 422)
    assert not (admin_client.app.state.label_sets.root / "orgs").exists()  # type: ignore[attr-defined]


def test_org_routes_need_the_admin_token(admin_client: TestClient, client: TestClient) -> None:
    path = f"/v1/orgs/{ORG_A}/labels"
    assert admin_client.get(path).status_code == 401
    assert admin_client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers=AUTH).status_code == 404, "no admin token set: no route"


def test_corrections_are_stored_per_org(admin_client: TestClient) -> None:
    photo = picture(21)

    response = admin_client.post(
        f"/v1/orgs/{ORG_A}/feedback",
        headers=AUTH,
        files={"image": ("a.jpg", photo, "image/jpeg")},
        data={"label_id": "n3"},
    )

    assert response.status_code == 201, response.text
    assert classify_as(admin_client, ORG_A, photo)["correction"]["label"]["label"] == "tabby"
    assert classify_as(admin_client, ORG_B, photo)["correction"] is None


def test_own_correction_wins_over_a_default_correction(admin_client: TestClient) -> None:
    photo = picture(22)
    for path, label_id in (("/v1/feedback", "n5"), (f"/v1/orgs/{ORG_A}/feedback", "n3")):
        admin_client.post(
            path,
            headers=AUTH,
            files={"image": ("a.jpg", photo, "image/jpeg")},
            data={"label_id": label_id},
        )

    assert classify_as(admin_client, ORG_A, photo)["correction"]["label"]["label"] == "tabby"
    assert classify_as(admin_client, ORG_B, photo)["correction"]["label"]["label"] == "cliff"


def test_org_correction_needs_a_label_of_the_org_set(admin_client: TestClient) -> None:
    add(admin_client, f"/v1/orgs/{ORG_A}/labels", "hut")

    response = admin_client.post(
        f"/v1/orgs/{ORG_B}/feedback",
        headers=AUTH,
        files={"image": ("a.jpg", picture(23), "image/jpeg")},
        data={"label_id": "runtime:hut"},
    )

    assert response.status_code == 409


def test_org_set_written_by_another_worker_is_picked_up(admin_settings: Settings) -> None:
    worker_a = next(hut_client(admin_settings))
    worker_b = next(hut_client(admin_settings))
    photo = picture(31)
    assert classify_as(worker_b, ORG_A, photo)["primary"]["label"] != "hut"

    add(worker_a, f"/v1/orgs/{ORG_A}/labels", "hut")

    assert classify_as(worker_b, ORG_A, photo)["primary"]["label"] == "hut"
    cached = worker_b.app.state.org_services  # type: ignore[attr-defined]
    assert len(cached) == 1
    classify_as(worker_b, ORG_A, photo)
    assert len(cached) == 1, "an unchanged set is served from the cache"


def test_token_org_id_that_is_no_safe_file_name_uses_the_default_set(
    admin_client: TestClient,
) -> None:
    body = classify_as(admin_client, "../evil", picture(32))

    assert body["model"]["name"] == "open-fake"
    assert not (admin_client.app.state.label_sets.root / "orgs").exists()  # type: ignore[attr-defined]


def test_global_labels_move_into_the_default_set(admin_settings: Settings) -> None:
    base_ref = str(admin_settings.taxonomy_path.resolve())  # type: ignore[union-attr]
    legacy = admin_settings.data_dir / "labels.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "extends": base_ref,
                "topics": [],
                "labels": [{"id": "runtime:hut", "name": "hut", "topic": "landscape"}],
            }
        )
    )

    app = create_app(admin_settings, classifier=OpenFakeClassifier(Taxonomy.load(legacy)))
    app.dependency_overrides[get_settings] = lambda: admin_settings
    with TestClient(app, headers=widget_headers()) as client:
        listed = client.get("/v1/labels", headers=AUTH).json()
        org_names = names(client, ORG_A)

    assert not legacy.exists()
    assert (admin_settings.data_dir / "label-sets" / "default.yaml").is_file()
    assert org_names == ["hut"]
    assert [label["id"] for label in listed["labels"]] == ["runtime:hut"]


def test_migration_keeps_both_files_when_the_default_set_exists(tmp_path: Path) -> None:
    sets = LabelSets(tmp_path, "package:taxonomy.yaml")
    sets.root.mkdir(parents=True)
    sets.default.overlay.write_text("labels: []\n")
    (tmp_path / "labels.yaml").write_text("labels: []\n")

    assert sets.migrate() is False
    assert (tmp_path / "labels.yaml").is_file()
    assert LabelSets(tmp_path / "empty", "package:taxonomy.yaml").migrate() is False


def test_org_ids_must_be_safe_file_names(tmp_path: Path) -> None:
    sets = LabelSets(tmp_path, "package:taxonomy.yaml")

    assert sets.org("org_1-A").overlay.name == "org_1-A.yaml"
    for bad in ("", "../x", "a/b", "x" * 65):
        with pytest.raises(ValueError, match="ORG_ID_PATTERN"):
            sets.org(bad)


def test_feedback_database_of_an_older_version_gets_the_org_column(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT "
            "NULL, image_sha256 TEXT NOT NULL, dhash TEXT NOT NULL, label_id TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO feedback (created_at, image_sha256, dhash, label_id) "
            "VALUES ('2026-10-01', 'x', '00000000000000ff', 'n1')"
        )
    db.close()

    store = FeedbackStore(path)

    recall = store.recall("00000000000000ff", {"n1"}, ORG_A)
    assert recall is not None and recall.feedback.org_id == "", "old rows are default rows"


def test_org_services_drop_the_least_recently_used(admin_client: TestClient) -> None:
    service: ClassificationService = admin_client.app.state.service  # type: ignore[attr-defined]
    cache = OrgServices(limit=2)
    cache.put("a", service, (1, 1))
    cache.put("b", service, (1, 1))
    assert cache.get("a", (1, 1)) is service  # "a" is now the most recent
    cache.put("c", service, (1, 1))

    assert cache.get("b", (1, 1)) is None
    assert cache.get("a", (2, 2)) is None, "a changed file is a miss"
    assert cache.get("c", (1, 1)) is service
    cache.drop("c")
    assert len(cache) == 1
