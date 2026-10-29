---
title: Picture corrections
slug: picture-corrections
status: beta
epic: E0
owner: vision
paths: ["src/synthwerk_vision/feedback.py"]
api: ["POST /v1/feedback"]
events: []
mcp: []
updated: 2026-10-29
---

# Picture corrections

> **In 30 seconds**
> - An admin tells the service the correct label for one picture.
> - When the same picture comes back, the response has a `correction` field.
> - Other pictures are not affected.

## What it does

- The service stores each correction in `$SYNTHWERK_VISION_DATA_DIR/feedback.sqlite3`.
- The key is the 64-bit difference hash (dHash) of the picture.
- A resized or re-encoded copy of the picture matches the same correction.
- The response keeps the model output and adds `correction` next to it.
- The service ignores a correction when its label was deleted.
- Each worker process reloads the corrections when `feedback.sqlite3` changes on disk.
- After its own write, a worker reloads once more. It then also sees a row from another worker.

## How to use

1. Set `SYNTHWERK_VISION_ADMIN_TOKEN` and send it as a bearer token.
2. Send `POST /v1/feedback` with the field `image` and the form field `label_id`.
3. Read `correction` in later classification responses. Prefer it to `primary` when it is set.

## Rules and limits

| Rule | Value |
|---|---|
| Match distance | 8 bits of dHash or less |
| Match key | dHash only, not an embedding |
| Label id | Must exist in the current taxonomy |

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 401 | None (FastAPI `detail`) | The bearer token is wrong. |
| 404 | None (FastAPI `detail`) | No admin token is set. |
| 409 | None (FastAPI `detail`) | The label id is unknown. |
| 503 | None (FastAPI `detail`) | The feedback store is not available. |

## Tests

- `tests/test_admin.py` — `feedback - same picture - corrected, other pictures not`
- `tests/test_admin.py` — `feedback - unknown label - rejected`
- `tests/test_admin.py` — `correction - label deleted - ignored`
- `tests/test_units.py` — `feedback - row written during a reload - loaded on the next refresh`
- `tests/test_units.py` — `feedback - row written right after add - loaded on the next refresh`

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | Rename to `synthwerk_vision`. The settings use the prefix `SYNTHWERK_VISION_`. |
| 2026-10-29 | `add()` forces one reload, so a row from another worker is not hidden (#26). |
