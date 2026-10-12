# synthwerk-vision — reference

Everything from the v1 README that did not fit the v2 front page. Commands, flags, env
vars and links here are the source of truth; the README only condenses them.

## Where it fits

```mermaid
flowchart LR
  widgets[synthwerk-widgets<br/>vision widget] --> svc[synthwerk-vision]
  studio[synthwerk-studio] --> svc
  svc --> ort[ONNX Runtime<br/>SigLIP 2 / MobileNetV2]
  svc --> disk[(local disk<br/>uploads, labels, corrections)]
```

- Ecosystem map: [synthwerk](https://github.com/VelimirMueller/synthwerk).

## API and events

- API spec: FastAPI serves it at `/docs` and `/openapi.json`. The contracts spec is planned (E1) in [synthwerk-contracts](https://github.com/VelimirMueller/synthwerk-contracts).
- Events: none yet.

| Method | Path                  | Purpose                                     | Success | Errors        |
|--------|-----------------------|---------------------------------------------|---------|---------------|
| GET    | `/health`             | Liveness, and whether the model is loaded   | 200     |               |
| POST   | `/v1/images`          | Store an image, return its generated id     | 201     | 413, 415, 422 |
| POST   | `/v1/classifications` | Classify an image (not stored)              | 200     | 413, 415, 422, 503 |
| *admin* | `/v1/labels`, `/v1/feedback` | See [Runtime labels and corrections](#runtime-labels-and-corrections) | | 401, 404, 409 |

- Today the service has only `/health`. `/healthz` and `/readyz` replace it in E1 (change list step 9).
- Both POST endpoints take `multipart/form-data` with one field, `image` (JPEG, PNG or WebP).

Example: a photo of a wooden hut in a rice field (real output, `top_k=3`, ids shortened):

```json
{
  "request_id": "da71efe1…",
  "image": { "sha256": "99645f6c6ed4…", "content_type": "image/jpeg", "size_bytes": 149115, "width": 960, "height": 480 },
  "model": { "name": "siglip2-base-patch16-224", "taxonomy_version": 1 },
  "primary": {
    "id": "open:hut", "label": "hut", "score": 0.3229,
    "topic": "building", "path": ["building and structure", "building", "hut"]
  },
  "topic": { "id": "building and structure", "parent": null, "depth": 0, "score": 0.815 },
  "labels": [
    { "id": "open:hut", "label": "hut", "score": 0.3229, "topic": "building", "path": ["building and structure", "building", "hut"] },
    { "id": "n02859443", "label": "boathouse", "score": 0.1376, "topic": "building", "path": ["…"] },
    { "id": "n02793495", "label": "barn", "score": 0.1131, "topic": "building", "path": ["…"] }
  ],
  "topics": [
    { "id": "building and structure", "parent": null, "depth": 0, "score": 0.815 },
    { "id": "building", "parent": "building and structure", "depth": 1, "score": 0.7964 },
    { "id": "landscape", "parent": null, "depth": 0, "score": 0.1428 }
  ],
  "uncertain": false,
  "timings_ms": { "inference": 31.45, "rollup": 0.14, "total": 35.72 }
}
```

Spot checks on six Wikimedia Commons photos (2026-10-02); the real numbers are in
[Evaluation](#evaluation):

| Photo | `primary.path` (label score) | `topic` |
|---|---|---|
| Labrador | animal › mammal › dog › Labrador retriever (0.89) | animal 0.99 |
| Cat in snow | animal › mammal › cat › tabby (0.43) | animal 0.99 |
| Bicycle | vehicle › bicycle › bicycle-built-for-two (0.94, wrong leaf) | vehicle 0.98 |
| Wooden hut | building and structure › building › hut (0.32) | building and structure 0.82 |
| Skyscraper | building and structure › building › skyscraper (0.58) | building and structure 0.99 |
| Aurora (a painting) | landscape › sky › aurora borealis (0.57) | landscape 0.65 |

| Field | Meaning |
|---|---|
| `primary` | The most likely label, with the path from its root topic |
| `topic` | The most likely root ("meta") topic, such as `animal`, `vehicle` or `building and structure` |
| `labels` | Top-k labels (`SYNTHWERK_VISION_TOP_K`) |
| `topics` | Every topic scoring at least `SYNTHWERK_VISION_TOPIC_MIN_SCORE`, best first |
| `uncertain` | `true` when the root topic score is below `SYNTHWERK_VISION_UNCERTAIN_BELOW` |
| `correction` | An admin correction for this picture, or `null` (see [Runtime labels and corrections](#runtime-labels-and-corrections)) |

Every response, errors included, carries an `X-Request-ID` header. A caller-supplied
`X-Request-ID` is kept when it is 1–64 characters of `[A-Za-z0-9_.-]`; otherwise the server
generates one. Classification responses repeat it as `request_id`.

## Taxonomy

Two packaged taxonomies, one per backend:

| File | Backend | Topics | Labels |
|---|---|---|---|
| [`taxonomy_open.yaml`](src/synthwerk_vision/data/taxonomy_open.yaml) | `siglip` | 57 (22 roots) | 1068: extends the ImageNet file with 68 more |
| [`taxonomy.yaml`](src/synthwerk_vision/data/taxonomy.yaml) | `mobilenet` | 53 (20 roots) | 1000, `index` = model output |

```yaml
topics:
  - {id: building, parent: building and structure}
labels:
  - {id: n03793489, name: mouse, topic: electronics, index: 673}
```

`taxonomy_open.yaml` does not repeat the 1000 ImageNet labels. It `extends`
`taxonomy.yaml` and adds `prompts`, 4 topics and 68 labels. Your own file can do the same,
which is the easiest way to add labels: a few lines on top of the packaged tree.

```yaml
# my-taxonomy.yaml, used with SYNTHWERK_VISION_TAXONOMY_PATH=my-taxonomy.yaml
extends: package:taxonomy_open.yaml
topics:
  - {id: aurora photo, parent: sky}
labels:
  - {id: my:corona, name: aurora corona, topic: aurora photo}
```

`extends` takes `package:<file>` or a path relative to the extending file, up to 4 levels.
Extending labels get their `index` automatically (after the base labels), and `prompts` may
only name labels that exist.

For SigLIP, a label is scored by the text `this is a photo of a {prompt or name}.`. The
`prompt` field disambiguates names a text model would misread ("mouse" is the computer
mouse). Every label text must be unique, or two labels get the same embedding.

`scripts/build_taxonomy.py` generates both files from WordNet: each ImageNet label goes
under the topic whose WordNet anchor is its *closest* hypernym. Its `TOPICS`, `OVERRIDES`,
`PROMPTS` and `OPEN_LABELS` tables are the source of truth: edit them and rerun. For your
own deployment you can also edit a copy of the YAML and set `SYNTHWERK_VISION_TAXONOMY_PATH`. On startup the service checks the tree
(unknown parents, cycles, duplicate ids, gaps in the indices, label count against the model's
outputs) and refuses to start if it is broken. This holds even when the model is missing: a
missing model is an ops state (503), while a broken taxonomy is a config bug.
If the model files are missing, the service still starts: `/health` reports
`"model_loaded": false` and classification returns 503.

### Backends

| `SYNTHWERK_VISION_BACKEND` | Model | Labels | Inference (CPU) | Weights |
|---|---|---|---|---|
| `siglip` (default) | SigLIP 2 base/16, 224 px, fp32 image and text towers | any text | ~30 ms | ~1.5 GB |
| `mobilenet` | MobileNetV2-12 | 1000 ImageNet classes | ~5 ms | 14 MB |

The build-time label cache covers the packaged taxonomy. With a custom
`SYNTHWERK_VISION_TAXONOMY_PATH`, the first container start embeds its labels (seconds) into
`SYNTHWERK_VISION_LABEL_CACHE_DIR`, so mount that directory as a volume to keep the cache across restarts.
The text tower runs only when label texts change (a cache miss): about 9 s for 1068 labels
with a ~3.2 GB memory peak (the fp32 Gemma vocabulary table alone is 786 MB), and then it is
dropped. Docker images ship with the cache built, so containers never load it unless you use a
custom taxonomy. In that case, give the first start about 4 GB of memory and mount the cache
directory. The SigLIP image is 3.48 GB. The container is ready in 1.3 s
(label cache built in) and uses ~790 MiB of RAM after the first classification (measured
2026-10-05, Docker 29.4 on an M-series Mac).

Both towers are **fp32 on purpose**:
- In a spike, the int8 image tower misread an aurora painting as a boat.
- The int8 text tower is accurate on ARM (cosine 0.996 to fp32) but not on x86 servers
  without VNNI (mean 0.959, min 0.872). On the x86 CI runner that dropped root-topic accuracy
  from 93.1 % to 83.0 %.
- fp32 gives identical embeddings on every architecture.

## Runtime labels and corrections

Two admin endpoints change what the server knows while it runs. They need
`Authorization: Bearer $SYNTHWERK_VISION_ADMIN_TOKEN` and **do not exist (404) when no token is set**.

| Method | Path | Does |
|---|---|---|
| `POST` | `/v1/labels` | Add a text label: `{"name", "topic", "prompt"?, "new_topic"?, "topic_parent"?}` |
| `GET` | `/v1/labels` | List runtime labels |
| `DELETE` | `/v1/labels/{id}` | Remove a runtime label (packaged labels cannot be removed: 409) |
| `POST` | `/v1/feedback` | `image` + `label_id`: the correct label for this picture |

```bash
curl -H "Authorization: Bearer $SYNTHWERK_VISION_ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"name": "rice paddy", "topic": "landscape", "prompt": "a green rice paddy field"}' \
  http://localhost:8000/v1/labels
```

- **No retraining.** A new label is one more text to embed; known texts keep their cached
  embedding. Adding a label takes about 30 s on CPU, mostly loading the fp32 text tower, which
  is dropped afterwards. The running service keeps answering meanwhile. The new service is
  built completely and then swapped in with one assignment, so no request sees a half-updated
  model.
- **Persistence.** Runtime labels live in `$SYNTHWERK_VISION_DATA_DIR/labels.yaml`, an ordinary taxonomy
  file that `extends` the configured base. It survives restarts and can be reviewed and edited.
  A change is staged and validated first; a failed change leaves file and service as they were.
- **Corrections** are stored in `$SYNTHWERK_VISION_DATA_DIR/feedback.sqlite3`, keyed by the picture's
  difference hash. When the same picture comes back (re-encoded or resized), the response
  carries `correction` next to the unchanged model output. Clients should prefer it.
  Different photos are never affected. Semantic embeddings cannot do this safely: on the eval
  set a resized copy of one photo can be *less* similar (0.87) than two different photos (0.90).
  Generalising from corrections (few-shot prototypes) is future work.
- Runtime labels need an open-vocabulary backend; `mobilenet` answers 409.

## Evaluation

[`eval/manifest.yaml`](eval/manifest.yaml) is a frozen set of **58 freely licensed Wikimedia
Commons photos** across 17 root topics. Every entry records the source page, author, license
and SHA-256. Images were picked by search query and license only, never by model output, then
checked by eye. Mixed images accept more than one topic (a girl hugging her dog: `person` or
`animal`). The metric is **root-topic top-1 accuracy**: is `topic` one of the accepted topics?

| Backend | Root-topic accuracy | 95 % interval (Wilson) | In top 3 | Latency/image |
|---|---|---|---|---|
| `siglip` | **93.1 %** (54/58) | 83.6–97.3 % | 98.3 % | 32 ms (M-series), 227 ms (4-vCPU x86 CI runner) |
| `mobilenet` | 70.7 % (41/58) | 58.0–80.8 % | 84.5 % | 7 ms (M-series) |

Measured 2026-10-03. SigLIP scores the same on ARM (M-series) and x86 (GitHub runner), with the
same four misses. That took the fp32 text tower: with int8 it fell to 83.0 % on x86 (see
[Backends](#backends)). Latency depends heavily on the CPU. The intervals do not overlap, so the difference holds
even on 58 images. CI runs the eval on every PR and on pushes to `main` and fails below 90 % for SigLIP. The
report is in the job summary.

```bash
uv run python scripts/evaluate.py                         # siglip
SYNTHWERK_VISION_BACKEND=mobilenet uv run python scripts/evaluate.py
```

What the misses show:
- **The catch-all bias of summed scores.** A sunflower became `object` although its top label
  was `daisy` (a plant). ImageNet has no sunflower, so probability spread across many weak
  labels, and the 113-label `object` topic collected more of it than `plant`. Broader label
  coverage fixes this better than any score trick. It is **not** patched here, because tuning
  the taxonomy on eval misses would make the eval meaningless.
- **Defensible disagreements**: a marina → `dock`, a waterfall with a dam → `dam`.
- MobileNet reads painted portraits as clothing and maps as jigsaw puzzles (1.00). Those are
  the limits of a closed 1000-class model.

Images are cached in `eval/images/` (in CI: an Actions cache keyed by the manifest). Wikimedia
renders thumbnails per data centre, so GitHub's runners receive other JPEG bytes for 5 of the 58
pictures than a client in Europe. Every entry therefore carries both a SHA-256 and a 64-bit
difference hash. A download is accepted when the bytes match, or when the dHash is within 8 bits
(re-encodes differ by at most 6, distinct images by at least 15). Re-encoded images are listed in
the report. Downloads are paced and retried on 429/5xx. Anything else, such as a deleted file,
is reported as *unavailable* with its reason, and the gate fails once more than 10 % are
unavailable. The fix is then to rebuild the set: `scripts/build_eval_manifest.py
--force` followed by a new visual review, because Commons search order drifts between runs.

## Configuration

- All settings are environment variables with the prefix `SYNTHWERK_VISION_`. See [`.env.example`](.env.example).
- **Env vars with any other prefix are ignored.** This includes the prefixes of the old project name. Rename them in every `.env` and deployment.
- A bare `SYNTHWERK_` prefix (without `VISION_`) is also ignored. Each Synthwerk service has its own prefix.
- The service does not warn about old names. It uses the default value instead.
- Names only. Secret values (`SYNTHWERK_VISION_ADMIN_TOKEN`) live in the environment, never in the repo.

| Variable                     | Default                                        |
|------------------------------|------------------------------------------------|
| `SYNTHWERK_VISION_CORS_ORIGINS`     | `["http://localhost:5173","http://localhost:8080"]` |
| `SYNTHWERK_VISION_UPLOAD_DIR`       | `uploads`                                      |
| `SYNTHWERK_VISION_DATA_DIR`         | `data` (runtime labels + corrections)          |
| `SYNTHWERK_VISION_ADMIN_TOKEN`      | unset: admin endpoints disabled                |
| `SYNTHWERK_VISION_MAX_UPLOAD_BYTES` | `5242880` (5 MiB)                              |
| `SYNTHWERK_VISION_MAX_IMAGE_PIXELS` | `40000000`                                     |
| `SYNTHWERK_VISION_BACKEND`          | `siglip` (or `mobilenet`)                      |
| `SYNTHWERK_VISION_SIGLIP_DIR`       | `models/siglip2-base-patch16-224`              |
| `SYNTHWERK_VISION_LABEL_CACHE_DIR`  | `models/label_cache`                           |
| `SYNTHWERK_VISION_MODEL_PATH`       | `models/mobilenetv2-12.onnx` (mobilenet)       |
| `SYNTHWERK_VISION_TAXONOMY_PATH`    | unset (packaged taxonomy for the backend)      |
| `SYNTHWERK_VISION_ORT_THREADS`      | `0` (ONNX Runtime default; spin-waiting is off) |
| `SYNTHWERK_VISION_TOP_K`            | `5`                                            |
| `SYNTHWERK_VISION_TOPIC_MIN_SCORE`  | `0.05`                                         |
| `SYNTHWERK_VISION_UNCERTAIN_BELOW`  | `0.5`                                          |

## Development

```text
src/synthwerk_vision/
  main.py         app factory, CORS, request id, lifespan (loads storage + classifier)
  api/routes.py   HTTP layer and dependency wiring
  api/admin.py    runtime labels and corrections (bearer token)
  images.py       bounded reads, image validation, dHash
  storage.py      ImageStorage protocol + local disk implementation
  classifier.py   Classifier protocol (image -> probability vector) + MobileNetV2
  siglip.py       SigLIP 2: image/text towers, tokenizer, label-embedding cache
  taxonomy.py     topic tree, label paths, roll-up of probabilities into topic scores
  runtime_labels.py  labels.yaml overlay + cross-process lock
  feedback.py     corrections in SQLite, keyed by dHash
  service.py      classifier + taxonomy -> labels, topics, uncertainty, timings
  evaluation.py   manifest loading, cached downloads, metrics, markdown/JSON reports
  data/taxonomy_open.yaml, data/taxonomy.yaml
  config.py       pydantic-settings (prefix SYNTHWERK_VISION_)
scripts/fetch_model.py           pinned model download
scripts/build_taxonomy.py        regenerate both taxonomies from WordNet (dev only)
scripts/warm_label_cache.py      embed label texts ahead of time (Docker build)
scripts/evaluate.py              eval gate (root-topic accuracy, per-topic table, misses)
scripts/build_eval_manifest.py   rebuild the eval set from Commons (needs re-review)
tests/                           unit tests with fakes, real-model tests (skip without weights),
                                 rename guard (test_naming.py: the old brand name must not return)
```

| Check | Command |
|---|---|
| Tests + coverage gate (90 %) | `uv run pytest` |
| Lint and format | `uv run ruff check . && uv run ruff format --check .` |
| Types (strict) | `uv run mypy` |
| Eval gate | `uv run python scripts/evaluate.py --min-topic-accuracy 0.9` |
| Git hooks | `uv run pre-commit install` |

- Do not run two ONNX Runtime processes at once for a benchmark. They slow each other down.
- CI: [`ci.yml`](.github/workflows/ci.yml) calls the blueprint workflow
  [`py-ci.yml@v1`](https://github.com/VelimirMueller/synthwerk-blueprint/blob/main/.github/workflows/py-ci.yml)
  (ruff, mypy strict, pytest without weights). Two local jobs add the rest:
  - `model`: fetches the weights, runs all tests with the real models, and runs the eval gate.
  - `docker-smoke`: builds the MobileNet image and checks `/health`.
  - `gate` passes only when all jobs pass.
- [`deliver.yml`](.github/workflows/deliver.yml) calls the blueprint
  [`image.yml@v1`](https://github.com/VelimirMueller/synthwerk-blueprint/blob/main/.github/workflows/image.yml)
  for the default SigLIP image: build, SBOM, Trivy scan.
- Dependabot watches Python, Actions and Docker dependencies.
- Workflow runs: [ci](https://github.com/VelimirMueller/synthwerk-vision/actions/workflows/ci.yml) · [deliver](https://github.com/VelimirMueller/synthwerk-vision/actions/workflows/deliver.yml).

### Branches

- The repo is trunk-based (decision D-04). Work on `feat/*` or `chore/*`, open a PR to `main`.
- **The `dev` branch is retired.** Do not open PRs to `dev`. CI does not run on pushes to `dev`.

## Deploy

- No deployment exists yet.
- A push to `main` or a `v*` tag pushes the image to `ghcr.io/velimirmueller/synthwerk-vision`
  and signs it with cosign.
- Target (D-04): merge to `main` deploys to **dev**. A `vX.Y.Z` tag deploys to **stg**.
  **prd** gets the same image digest after a manual approval.
- Infrastructure: [synthwerk-infra](https://github.com/VelimirMueller/synthwerk-infra).

## Features

| Feature | Status |
|---|---|
| [Image classification](docs/features/image-classification.md) | stable |
| [Image upload](docs/features/image-upload.md) | beta |
| [Runtime labels](docs/features/runtime-labels.md) | beta |
| [Picture corrections](docs/features/picture-corrections.md) | beta |
| [Classification eval gate](docs/features/classification-eval.md) | stable |

## Model and data

| Item    | Source                                                                 | License      |
|---------|------------------------------------------------------------------------|--------------|
| SigLIP 2 weights + tokenizer | [google/siglip2-base-patch16-224](https://huggingface.co/google/siglip2-base-patch16-224), ONNX export by [onnx-community](https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX) (revision `ba1f3b0`) | Apache-2.0 |
| MobileNetV2 weights | [ONNX Model Zoo, MobileNetV2-12](https://github.com/onnx/models/tree/main/validated/vision/classification/mobilenet) | Apache-2.0   |
| Labels  | [pytorch/hub `imagenet_classes.txt`](https://github.com/pytorch/hub)   | BSD-3-Clause |
| Synset ids | [Keras `imagenet_class_index.json`](https://storage.googleapis.com/download.tensorflow.org/data/imagenet_class_index.json) | Apache-2.0 |
| Eval images | 58 Wikimedia Commons files, per-file author + license in `eval/manifest.yaml` (CC BY / CC BY-SA / CC0 / public domain); not redistributed, downloaded on demand | per file |
| Topic tree | [WordNet 3.0](https://wordnet.princeton.edu/license-and-commercial-use) via NLTK | WordNet license (permissive) |

The weights were trained by their publishers (SigLIP 2 on WebLI, MobileNetV2 on
ImageNet-1k). This repository contains no
training data and no scraped images. Any future fine-tuning dataset will be documented
here with its source and license before it is used.

## Security

- Uploaded files are named `<uuid4>.<ext>`. The extension comes from the decoded format,
  and files open with `O_EXCL`, so an upload can never overwrite an existing file.
- CORS allows only the configured origins, without credentials.
- Endpoints that change server state (`/v1/labels`, `/v1/feedback`) need a bearer token
  (`SYNTHWERK_VISION_ADMIN_TOKEN`, compared in constant time) and are absent without one.
- Classification and uploads are unauthenticated. Put the service behind a gateway with auth
  and rate limits before you expose it publicly.
- Report a vulnerability through a private GitHub security advisory on this repo.

## Roadmap

- Few-shot labels from example images (needs calibrating image-image vs image-text scores).
- An S3-compatible `ImageStorage`, plus a `GET /v1/images/{id}`.
- API keys and rate limiting.

## License

- [MIT](LICENSE)
