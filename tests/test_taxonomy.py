import copy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from synthwerk_vision.service import ClassificationService
from synthwerk_vision.taxonomy import Taxonomy, TaxonomyError
from tests.conftest import SMALL_TAXONOMY, FakeClassifier


def small(**changes: Any) -> Taxonomy:
    document = copy.deepcopy(SMALL_TAXONOMY)
    document.update(changes)
    return Taxonomy(document["topics"], document["labels"], document["version"])


def test_paths_run_from_root_topic_to_label() -> None:
    taxonomy = small()

    assert taxonomy.label_path(taxonomy.labels[0]) == ["animal", "mammal", "dog", "beagle"]
    assert taxonomy.label_path(taxonomy.labels[3]) == ["vehicle", "bicycle", "mountain bike"]
    assert taxonomy.topics["dog"].depth == 2


def test_rollup_sums_every_descendant_label() -> None:
    scores = small().rollup(np.array([0.5, 0.3, 0.1, 0.06, 0.04], dtype=np.float32))

    assert scores["dog"] == pytest.approx(0.8)
    assert scores["mammal"] == pytest.approx(0.9)
    assert scores["animal"] == pytest.approx(0.9)
    assert scores["vehicle"] == pytest.approx(0.06)
    assert scores["landscape"] == pytest.approx(0.04)


def test_rollup_rejects_wrong_vector_size() -> None:
    with pytest.raises(TaxonomyError, match="expected 5"):
        small().rollup(np.zeros(3, dtype=np.float32))


@pytest.mark.parametrize(
    ("topics", "labels", "message"),
    [
        ([{"id": "a"}, {"id": "a"}], None, "duplicate topic"),
        ([{"id": "a", "parent": "missing"}], None, "unknown parent"),
        ([{"id": "a", "parent": "b"}, {"id": "b", "parent": "a"}], None, "cycle"),
        (None, [], "no labels"),
        (None, [{"id": "x", "name": "x", "topic": "dog", "index": 3}], "indices"),
        (None, [{"id": "x", "name": "x", "topic": "nope", "index": 0}], "unknown topic"),
        (None, [{"id": "x", "name": "x", "topic": "dog", "index": 0.0}], "integers"),
        (
            None,
            [
                {"id": "x", "name": "x", "topic": "dog", "index": 0},
                {"id": "x", "name": "y", "topic": "dog", "index": 1},
            ],
            "duplicate label",
        ),
    ],
)
def test_invalid_taxonomies_are_rejected(
    topics: list[dict[str, Any]] | None, labels: list[dict[str, Any]] | None, message: str
) -> None:
    changes: dict[str, Any] = {}
    if topics is not None:
        changes["topics"] = topics
    if labels is not None:
        changes["labels"] = labels

    with pytest.raises(TaxonomyError, match=message):
        small(**changes)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("- just\n- a list\n", "mapping"),
        ("version: 1\n", "mapping"),
        ("topics: [unclosed\n", "cannot read"),
        ("topics: [{parent: null}]\nlabels: []\n", "malformed"),
        ("topics: []\nlabels: [{id: x, name: x, topic: t}]\n", "malformed"),
    ],
)
def test_load_turns_every_file_problem_into_taxonomy_error(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(content)

    with pytest.raises(TaxonomyError, match=message):
        Taxonomy.load(path)


def test_load_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyError, match="cannot read"):
        Taxonomy.load(tmp_path / "nope.yaml")


def test_packaged_taxonomy_covers_imagenet() -> None:
    taxonomy = Taxonomy.load()
    paths = {label.name: taxonomy.label_path(label) for label in taxonomy.labels}

    assert len(taxonomy.labels) == 1000
    assert paths["tabby"] == ["animal", "mammal", "cat", "tabby"]
    assert paths["mountain bike"] == ["vehicle", "bicycle", "mountain bike"]
    assert paths["boathouse"] == ["building and structure", "building", "boathouse"]
    assert paths["container ship"] == ["vehicle", "watercraft", "container ship"]
    assert paths["espresso"] == ["food and drink", "drink", "espresso"]
    assert paths["castle"] == ["building and structure", "building", "castle"]
    assert paths["microwave"] == ["household appliance", "microwave"]
    assert paths["tile roof"] == ["building and structure", "roof", "tile roof"]
    assert paths["lampshade"][0] != "clothing", "covering.n.02 must not leak into clothing"
    assert paths["whippet"] == ["animal", "mammal", "dog", "whippet"]
    assert paths["school bus"] == ["vehicle", "bus", "school bus"]


def test_packaged_taxonomy_file_is_valid_yaml() -> None:
    root = Path(__file__).resolve().parent.parent
    document = yaml.safe_load((root / "src/synthwerk_vision/data/taxonomy.yaml").read_text())

    assert document["version"] == 1


def test_service_rejects_taxonomy_model_size_mismatch() -> None:
    classifier = FakeClassifier()
    classifier.num_classes = 4

    with pytest.raises(TaxonomyError, match="outputs 4"):
        ClassificationService(classifier, small(), top_k=3, topic_min_score=0, uncertain_below=0.5)


def test_service_flags_uncertain_results() -> None:
    spread = FakeClassifier([0.2, 0.1, 0.1, 0.3, 0.3])  # animal 0.4, vehicle 0.3, landscape 0.3
    service = ClassificationService(
        spread, small(), top_k=3, topic_min_score=0, uncertain_below=0.5
    )

    result = service.classify(b"")

    assert result.topic.id == "animal"
    assert result.topic.score == pytest.approx(0.4)
    assert result.uncertain is True


def test_uncertainty_uses_the_unrounded_score() -> None:
    # animal = 0.49996, which rounds to 0.5 for display but is still below the 0.5 threshold.
    edge = FakeClassifier([0.49996, 0.0, 0.0, 0.25002, 0.25002])
    service = ClassificationService(edge, small(), top_k=1, topic_min_score=0, uncertain_below=0.5)

    result = service.classify(b"")

    assert result.topic.score == 0.5
    assert result.uncertain is True


def test_broken_taxonomy_stops_startup(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from synthwerk_vision.main import create_app
    from synthwerk_vision.platform.config import Settings

    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump({"topics": [{"id": "a", "parent": "zzz"}], "labels": []}))
    app = create_app(Settings(taxonomy_path=broken), classifier=FakeClassifier())

    with pytest.raises(TaxonomyError, match="unknown parent"), TestClient(app):
        pass


def write_yaml(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(document))
    return path


def test_user_file_can_extend_the_packaged_open_taxonomy(tmp_path: Path) -> None:
    mine = write_yaml(
        tmp_path / "mine.yaml",
        {
            "extends": "package:taxonomy_open.yaml",
            "prompts": {"n02123045": "tabby cat"},
            "topics": [{"id": "aurora photo", "parent": "sky"}],
            "labels": [{"id": "my:corona", "name": "aurora corona", "topic": "aurora photo"}],
        },
    )

    taxonomy = Taxonomy.load(mine)
    by_name = {label.name: label for label in taxonomy.labels}

    assert len(taxonomy.labels) == 1069
    assert by_name["aurora corona"].index == 1068
    assert taxonomy.label_path(by_name["aurora corona"]) == [
        "landscape",
        "sky",
        "aurora photo",
        "aurora corona",
    ]
    assert by_name["tabby"].text == "tabby cat"
    assert by_name["mouse"].text == "computer mouse", "prompts of the base file are kept"


def test_extends_resolves_paths_relative_to_the_file(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    write_yaml(tmp_path / "base.yaml", SMALL_TAXONOMY)
    child = write_yaml(
        tmp_path / "conf" / "child.yaml",
        {"extends": "../base.yaml", "labels": [{"id": "n6", "name": "hut", "topic": "landscape"}]},
    )

    assert [label.name for label in Taxonomy.load(child).labels][-1] == "hut"


@pytest.mark.parametrize(
    ("extension", "message"),
    [
        ({"extends": 5}, "must be a file name"),
        ({"extends": "missing.yaml"}, "cannot read"),
        ({"extends": "base.yaml", "prompts": ["x"]}, "must map label ids"),
        ({"extends": "base.yaml", "prompts": {"nope": "x"}}, "unknown label ids"),
        (
            {
                "extends": "base.yaml",
                "labels": [{"id": "x", "name": "x", "topic": "dog", "index": 9}],
            },
            "index automatically",
        ),
        ({"extends": "base.yaml", "labels": [{"name": "no id"}]}, "malformed"),
        (
            {"extends": "base.yaml", "labels": [{"id": "n1", "name": "dup", "topic": "dog"}]},
            "duplicate label id",
        ),
        (
            {"extends": "base.yaml", "labels": [{"id": "x", "name": "x", "topic": "unknown"}]},
            "unknown topic",
        ),
    ],
)
def test_broken_extensions_are_rejected(
    tmp_path: Path, extension: dict[str, Any], message: str
) -> None:
    write_yaml(tmp_path / "base.yaml", SMALL_TAXONOMY)

    with pytest.raises(TaxonomyError, match=message):
        Taxonomy.load(write_yaml(tmp_path / "ext.yaml", extension))


def test_extends_cycles_and_deep_chains_are_rejected(tmp_path: Path) -> None:
    write_yaml(tmp_path / "a.yaml", {"extends": "b.yaml"})
    write_yaml(tmp_path / "b.yaml", {"extends": "a.yaml"})
    with pytest.raises(TaxonomyError, match="cycle"):
        Taxonomy.load(tmp_path / "a.yaml")

    write_yaml(tmp_path / "t0.yaml", SMALL_TAXONOMY)
    for i in range(1, 6):
        write_yaml(tmp_path / f"t{i}.yaml", {"extends": f"t{i - 1}.yaml"})
    with pytest.raises(TaxonomyError, match="deeper than"):
        Taxonomy.load(tmp_path / "t5.yaml")
