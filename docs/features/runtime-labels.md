---
title: Runtime labels
slug: runtime-labels
status: beta
epic: E0
owner: vision
paths: ["src/synthwerk_vision/api/admin.py", "src/synthwerk_vision/runtime_labels.py"]
api: ["GET /v1/labels", "POST /v1/labels", "DELETE /v1/labels/{label_id}"]
events: []
mcp: []
updated: 2026-10-21
---

# Runtime labels

> **In 30 seconds**
> - An admin adds or removes a text label while the service runs.
> - The service does not retrain. It embeds only the new label text.
> - Adding a label takes about 30 s, mostly to load the fp32 text tower.

## What it does

- The service writes runtime labels to `$SYNTHWERK_VISION_DATA_DIR/labels.yaml`.
- This file `extends` the configured base taxonomy, so it survives a restart.
- The service stages and validates each change first. A failed change changes nothing.
- The service builds the new model state completely, then swaps it in with one assignment.
- Other worker processes see the changed file and rebuild from the label cache.
- A file lock between processes keeps concurrent writes from losing labels.

## How to use

1. Set `SYNTHWERK_VISION_ADMIN_TOKEN`. Without it, the admin endpoints do not exist.
2. Send `Authorization: Bearer <token>` with each admin request.
3. Send `POST /v1/labels` with `{"name", "topic", "prompt"?, "new_topic"?, "topic_parent"?}`.
4. Send `DELETE /v1/labels/{label_id}` to remove a runtime label.

## Rules and limits

| Rule | Value |
|---|---|
| Backend | `siglip` only. `mobilenet` has a fixed label set. |
| Time to add one label | about 30 s on CPU |
| Memory peak while adding | about 3.2 GB |
| Packaged labels | Cannot be removed |
| Label text | Must be unique in the taxonomy |

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 401 | None (FastAPI `detail`) | The bearer token is wrong. |
| 404 | None (FastAPI `detail`) | No admin token is set, or the runtime label does not exist. |
| 409 | None (FastAPI `detail`) | The backend has fixed labels, the label clashes, or the label is packaged. |

## Tests

- `tests/test_admin.py` — `admin endpoints - no token set - return 404`
- `tests/test_admin.py` — `runtime label - added - scored at once and kept after restart`
- `tests/test_admin.py` — `delete - packaged label - refused`
- `tests/test_admin.py` — `label writes - two processes at once - both kept`

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | Rename to `synthwerk_vision`. The settings use the prefix `SYNTHWERK_VISION_`. |
| 2026-10-21 | No behaviour change. The settings import moved to `platform/config.py` (#23). With an admin token, `/readyz` checks that the data directory is writable. |
