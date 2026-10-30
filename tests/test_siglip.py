from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from synthwerk_vision.classifier import ModelNotAvailableError
from synthwerk_vision.service import ClassificationService
from synthwerk_vision.siglip import (
    PROMPT_TEMPLATE,
    LabelEmbeddingCache,
    SiglipClassifier,
    TextEncoder,
    _fingerprint,
    preprocess,
)
from synthwerk_vision.taxonomy import Taxonomy, TaxonomyError
from tests.conftest import make_image

SIGLIP_DIR = Path(__file__).resolve().parent.parent / "models" / "siglip2-base-patch16-224"
needs_siglip = pytest.mark.skipif(
    not (SIGLIP_DIR / "vision_model.onnx").is_file(), reason="run scripts/fetch_model.py siglip"
)


class FakeSession:
    def __init__(self, output: npt.NDArray[np.float32]) -> None:
        self.output = output
        self.calls: list[dict[str, npt.NDArray[np.generic]]] = []

    def run(
        self, output_names: list[str] | None, input_feed: dict[str, npt.NDArray[np.generic]]
    ) -> list[npt.NDArray[np.float32]]:
        self.calls.append(input_feed)
        batch = next(iter(input_feed.values())).shape[0]
        return [np.repeat(self.output[np.newaxis, :], batch, axis=0)]


def tiny_tokenizer() -> Tokenizer:
    vocab = {"<pad>": 0, "<eos>": 1, "<unk>": 2, "a": 3, "photo": 4, "of": 5, "dog": 6}
    tokenizer = Tokenizer(models.WordLevel(vocab, unk_token="<unk>"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="$A <eos>", special_tokens=[("<eos>", 1)]
    )
    return tokenizer


def test_preprocess_resizes_without_crop_into_minus_one_to_one() -> None:
    tensor = preprocess(make_image(size=(640, 100)))

    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.min() >= -1.0
    assert tensor.max() <= 1.0


def test_text_ids_are_lowercased_padded_to_64_and_end_with_eos() -> None:
    encoder = TextEncoder(tiny_tokenizer(), lambda: FakeSession(np.ones(4, dtype=np.float32)))

    ids = encoder.token_ids(["A PHOTO of a dog", "dog " * 100])

    assert ids.shape == (2, 64)
    assert list(ids[0][:6]) == [3, 4, 5, 3, 6, 1]
    assert set(ids[0][6:]) == {0}
    assert ids[1][-1] == 1, "truncation must keep the EOS the model pools on"


def test_text_tower_loads_lazily_and_embeds_in_batches() -> None:
    created: list[FakeSession] = []

    def factory() -> FakeSession:
        created.append(FakeSession(np.array([3.0, 4.0], dtype=np.float32)))
        return created[-1]

    encoder = TextEncoder(tiny_tokenizer(), factory)
    assert created == []

    embeddings = encoder.embed(["dog"] * 70)

    assert len(created) == 1
    assert [call["input_ids"].shape[0] for call in created[0].calls] == [32, 32, 6]
    assert embeddings.shape == (70, 2)
    assert np.allclose(embeddings[0], [0.6, 0.8]), "embeddings are L2-normalized"


def test_label_cache_round_trip_and_invalidation(tmp_path: Path) -> None:
    cache = LabelEmbeddingCache(tmp_path / "cache")
    texts = ["this is a photo of a dog.", "this is a photo of a hut."]
    embeddings = np.eye(2, dtype=np.float32)

    assert cache.load(texts) is None
    cache.store(texts, embeddings)

    assert np.array_equal(cache.load(texts), embeddings)  # type: ignore[arg-type]
    assert cache.load([*texts, "new label"]) is None, "a changed label list is a cache miss"
    assert list((tmp_path / "cache").glob("*.tmp.npy")) == []


def test_label_cache_ignores_corrupt_files(tmp_path: Path) -> None:
    cache = LabelEmbeddingCache(tmp_path)
    texts = ["x"]
    (tmp_path / f"{cache.key(texts)}.npy").write_bytes(b"not numpy")

    assert cache.load(texts) is None


@pytest.mark.parametrize("shape", [(3, 768), (2,), (2, 2, 768), (2, 512)])
def test_label_cache_rejects_wrong_shapes(tmp_path: Path, shape: tuple[int, ...]) -> None:
    cache = LabelEmbeddingCache(tmp_path)
    np.save(tmp_path / f"{cache.key(['a', 'b'])}.npy", np.ones(shape, dtype=np.float32))

    assert cache.load(["a", "b"], width=768) is None


def test_label_cache_key_depends_on_model_fingerprint(tmp_path: Path) -> None:
    texts = ["x"]

    assert LabelEmbeddingCache(tmp_path, "tok-a").key(texts) != LabelEmbeddingCache(
        tmp_path, "tok-b"
    ).key(texts)


def test_predict_is_softmax_over_scaled_cosine() -> None:
    labels = np.array([[1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071]], dtype=np.float32)
    classifier = SiglipClassifier(FakeSession(np.array([0.0, 2.0], dtype=np.float32)), labels, 10.0)

    probabilities = classifier.predict(make_image())

    assert classifier.num_classes == 3
    assert probabilities.sum() == pytest.approx(1.0)
    assert int(probabilities.argmax()) == 1
    expected = np.exp(10 * np.array([0.0, 1.0, 0.7071]))
    assert probabilities == pytest.approx(expected / expected.sum(), rel=1e-4)


def test_duplicate_label_texts_are_a_taxonomy_error(tmp_path: Path) -> None:
    mine = tmp_path / "mine.yaml"
    mine.write_text(
        "extends: package:taxonomy_open.yaml\n"
        "labels:\n  - {id: 'my:hut', name: wooden hut, topic: building, prompt: hut}\n"
    )

    with pytest.raises(TaxonomyError, match="share a text"):
        SiglipClassifier.from_dir(tmp_path, Taxonomy.load(mine), tmp_path)


def test_from_dir_reports_missing_files(tmp_path: Path) -> None:
    with pytest.raises(ModelNotAvailableError, match=r"vision_model\.onnx"):
        SiglipClassifier.from_dir(tmp_path, Taxonomy.load(packaged="taxonomy_open.yaml"), tmp_path)


def test_packaged_open_taxonomy_adds_open_vocabulary_labels() -> None:
    taxonomy = Taxonomy.load(packaged="taxonomy_open.yaml")
    by_name = {label.name: label for label in taxonomy.labels}

    assert len(taxonomy.labels) > 1000
    assert taxonomy.label_path(by_name["hut"]) == ["building and structure", "building", "hut"]
    assert taxonomy.label_path(by_name["aurora borealis"]) == [
        "landscape",
        "sky",
        "aurora borealis",
    ]
    assert by_name["mouse"].text == "computer mouse"
    assert by_name["tabby"].text == "tabby"
    assert len({label.text for label in taxonomy.labels}) == len(taxonomy.labels), (
        "every label needs a distinct text, or two labels get the same embedding"
    )


@needs_siglip
def test_siglip_end_to_end_with_label_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taxonomy = Taxonomy.load(packaged="taxonomy_open.yaml")
    texts = [PROMPT_TEMPLATE.format(label.text) for label in taxonomy.labels]

    classifier = SiglipClassifier.from_dir(SIGLIP_DIR, taxonomy, tmp_path)
    assert LabelEmbeddingCache(tmp_path, _fingerprint(SIGLIP_DIR)).load(texts, 768) is not None

    def no_text_tower(*args: object) -> None:
        raise AssertionError("cache hit must not run the text tower")

    monkeypatch.setattr(TextEncoder, "embed", no_text_tower)
    cached = SiglipClassifier.from_dir(SIGLIP_DIR, taxonomy, tmp_path)

    service = ClassificationService(
        cached, taxonomy, top_k=5, topic_min_score=0.05, uncertain_below=0.5
    )
    result = service.classify(make_image("JPEG", (320, 240)))
    probabilities = classifier.predict(make_image("JPEG", (320, 240)))

    assert classifier.num_classes == len(taxonomy.labels)
    assert probabilities.sum() == pytest.approx(1.0, abs=1e-4)
    assert len(result.labels) == 5


def test_for_taxonomy_embeds_only_new_texts_and_leaves_the_original(tmp_path: Path) -> None:
    import copy

    from tests.conftest import SMALL_TAXONOMY

    def taxonomy_with(extra: list[dict[str, object]]) -> Taxonomy:
        document = copy.deepcopy(SMALL_TAXONOMY)
        document["labels"] += extra
        return Taxonomy(document["topics"], document["labels"], 1)

    base = taxonomy_with([])
    texts = [PROMPT_TEMPLATE.format(label.text) for label in base.labels]
    embedded: list[list[str]] = []

    def embed(new: list[str]) -> npt.NDArray[np.float32]:
        embedded.append(new)
        return np.eye(len(new), 4, dtype=np.float32)

    cache = LabelEmbeddingCache(tmp_path)
    original = SiglipClassifier(
        FakeSession(np.ones(4, dtype=np.float32)),
        np.eye(5, 4, dtype=np.float32),
        10.0,
        texts=texts,
        embed_texts=embed,
        cache=cache,
    )

    grown = original.for_taxonomy(
        taxonomy_with([{"id": "r1", "name": "hut", "topic": "landscape", "index": 5}])
    )

    assert embedded == [[PROMPT_TEMPLATE.format("hut")]], "known texts are reused"
    assert (original.num_classes, grown.num_classes) == (5, 6)
    assert cache.load([*texts, PROMPT_TEMPLATE.format("hut")], width=4) is not None
    assert original.for_taxonomy(base).num_classes == 5
    assert len(embedded) == 1, "a label set of known texts needs no text tower"


def test_for_taxonomy_reads_a_label_set_another_worker_built(tmp_path: Path) -> None:
    import copy

    from tests.conftest import SMALL_TAXONOMY

    document = copy.deepcopy(SMALL_TAXONOMY)
    texts = [PROMPT_TEMPLATE.format(label["name"]) for label in document["labels"]]
    document["labels"].append({"id": "r1", "name": "hut", "topic": "landscape", "index": 5})
    grown = Taxonomy(document["topics"], document["labels"], 1)
    cache = LabelEmbeddingCache(tmp_path)
    stored = np.eye(6, 4, dtype=np.float32)
    cache.store([*texts, PROMPT_TEMPLATE.format("hut")], stored)

    def no_tower(new: list[str]) -> npt.NDArray[np.float32]:
        raise AssertionError("a cached label set must not run the text tower")

    worker = SiglipClassifier(
        FakeSession(np.ones(4, dtype=np.float32)),
        np.eye(5, 4, dtype=np.float32),
        10.0,
        texts=texts,
        embed_texts=no_tower,
        cache=cache,
    )

    assert worker.for_taxonomy(grown).num_classes == 6


def test_for_taxonomy_without_text_tower_cannot_add_labels() -> None:
    import copy

    from tests.conftest import SMALL_TAXONOMY

    document = copy.deepcopy(SMALL_TAXONOMY)
    document["labels"].append({"id": "r1", "name": "hut", "topic": "landscape", "index": 5})
    classifier = SiglipClassifier(
        FakeSession(np.ones(4, dtype=np.float32)), np.eye(5, 4, dtype=np.float32), 10.0
    )

    with pytest.raises(ModelNotAvailableError, match="text tower"):
        classifier.for_taxonomy(Taxonomy(document["topics"], document["labels"], 1))
