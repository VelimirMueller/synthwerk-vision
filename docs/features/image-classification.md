---
title: Image classification
slug: image-classification
status: stable
epic: E0
owner: vision
paths: ["src/synthwerk_vision/main.py", "src/synthwerk_vision/platform/config.py", "src/synthwerk_vision/__init__.py", "src/synthwerk_vision/py.typed", "src/synthwerk_vision/api/__init__.py", "src/synthwerk_vision/api/routes.py", "src/synthwerk_vision/schemas.py", "src/synthwerk_vision/service.py", "src/synthwerk_vision/classifier.py", "src/synthwerk_vision/siglip.py", "src/synthwerk_vision/taxonomy.py", "src/synthwerk_vision/images.py", "src/synthwerk_vision/data/**"]
api: ["POST /v1/classifications"]
events: []
mcp: []
updated: 2026-10-29
---

# Image classification

> **In 30 seconds**
> - The service gives one image a label and the label's place in a topic tree.
> - The default backend is SigLIP 2. A label is a text, so new labels need no training.
> - One image takes about 35 ms on an M-series CPU.

## What it does

- The service reads one uploaded image and checks its size, format and pixel count.
- The model gives a probability for each label in the taxonomy.
- The score of a topic is the sum of the scores of all labels below it.
- The response contains the best label, the best root topic, the top-k labels and the topics.
- The response sets `uncertain` to `true` when the best root topic scores below the limit.
- The service does not store the image.
- Every response has an `X-Request-ID` header, a 500 too. A classification response also has `request_id`.
- When the model files are missing, the service starts, and `/readyz` reports `down` (503). See [Health and telemetry](health-and-telemetry.md).
- When the taxonomy is broken, the service does not start.

## How to use

1. Send the image as `multipart/form-data` in the field `image` to `POST /v1/classifications`.
2. Read `primary` for the best label and `topic` for the best root topic.
3. Read `uncertain`. When it is `true`, show the result as a guess.
4. Send `X-Request-ID` to connect the response to your own logs.

## Rules and limits

| Rule | Value |
|---|---|
| Image formats | JPEG, PNG, WebP (from the decoded bytes, not from `Content-Type`) |
| Maximum upload size | 5 MiB (`SYNTHWERK_VISION_MAX_UPLOAD_BYTES`) |
| Maximum pixel count | 40 000 000 (`SYNTHWERK_VISION_MAX_IMAGE_PIXELS`) |
| Labels in the response | 5 (`SYNTHWERK_VISION_TOP_K`, 1 to 20) |
| Minimum topic score in `topics` | 0.05 (`SYNTHWERK_VISION_TOPIC_MIN_SCORE`) |
| `uncertain` limit | root topic score below 0.5 (`SYNTHWERK_VISION_UNCERTAIN_BELOW`) |
| Caller `X-Request-ID` | 1 to 64 characters of `[A-Za-z0-9_.-]`, else the service makes one |
| Packaged labels | 1068 (SigLIP 2), 1000 (MobileNetV2) |

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 413 | None (FastAPI `detail`) | The image is larger than the upload limit or the pixel limit. |
| 415 | None (FastAPI `detail`) | The bytes are not a JPEG, PNG or WebP image. |
| 422 | None (FastAPI `detail`) | The request has no `image` field, or the file is empty. |
| 500 | None (text `Internal Server Error`) | An unexpected error. Give the `X-Request-ID` to the operator. |
| 503 | None (FastAPI `detail`) | The model is not loaded. |

## Tests

- `tests/test_api.py` — `classification - valid image - returns labels, paths and topics`
- `tests/test_api.py` — `classification - no model - returns 503`
- `tests/test_api.py` — `request id - unsafe caller value - service makes a new one`
- `tests/test_taxonomy.py` — `rollup - any vector - sums every descendant label`
- `tests/test_taxonomy.py` — `startup - broken taxonomy - service does not start`
- `tests/test_siglip.py` — `text ids - any prompt - padded to 64 and end with EOS`
- `tests/test_units.py` — `ORT sessions - any backend - never busy-spin`

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | Rename to `synthwerk_vision`. The settings use the prefix `SYNTHWERK_VISION_`. |
| 2026-10-21 | `GET /health` is removed. `/readyz` reports the model state. The settings module moved to `platform/config.py` (#23). |
| 2026-10-29 | A 500 response has the `X-Request-ID` header (#27). |
