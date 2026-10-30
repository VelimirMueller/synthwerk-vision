---
title: Label sets per org
slug: label-sets
status: beta
epic: E5
owner: vision
paths: ["src/synthwerk_vision/label_sets.py", "src/synthwerk_vision/api/admin.py", "src/synthwerk_vision/api/routes.py", "src/synthwerk_vision/feedback.py"]
api: ["GET /v1/orgs/{org_id}/labels", "POST /v1/orgs/{org_id}/labels", "DELETE /v1/orgs/{org_id}/labels/{label_id}", "POST /v1/orgs/{org_id}/feedback"]
events: []
mcp: []
updated: 2026-10-30
---

# Label sets per org

> **In 30 seconds**
> - Each org can have its own label set and its own corrections (synthwerk#44).
> - A classification uses the set and the corrections of the widget token's org.
> - An org without its own set uses the default set. The first change copies the default set into the org.
> - The studio BFF calls the org routes with the admin token, after it checked that the user is an org owner or admin.

## What it does

- The default set is `$SYNTHWERK_VISION_DATA_DIR/label-sets/default.yaml`. The platform routes `/v1/labels` change it.
- An org set is `$SYNTHWERK_VISION_DATA_DIR/label-sets/orgs/<org id>.yaml`. Each file `extends` the base taxonomy.
- The org id is the Zitadel org id. The widget token carries it in `urn:zitadel:iam:user:resourceowner:id`.
- The first change of an org copies the labels and topics of the default set into the org file. After that, a change of the default set does not reach the org.
- `POST /v1/classifications` reads the org from the verified widget token. The org file exists: the org set scores the image. Else the default set does.
- Each worker keeps the services of up to 64 org sets. It rebuilds a set when its file changed on disk, for example after a write by another worker.
- A rebuild reads the label embeddings from `SYNTHWERK_VISION_LABEL_CACHE_DIR`. Only a text that no worker embedded yet loads the text tower.
- Corrections have an org id. An org gets its own correction for a picture first. Without one, it gets a default correction. It never gets the correction of another org.
- One lock file (`label-sets/.lock`) serialises every write, so the copy reads a stable default set.

### Migration

- Older versions kept one global set in `$SYNTHWERK_VISION_DATA_DIR/labels.yaml`.
- At startup, the service moves that file to `label-sets/default.yaml`. Every org starts on it.
- When `label-sets/default.yaml` exists already, the service keeps both files and logs `vision.labels.migration_skipped`.
- Old rows in `feedback.sqlite3` get the org id `""`. They become default corrections.

## How to use

1. Set `SYNTHWERK_VISION_ADMIN_TOKEN`. Give the same value to the studio as `NUXT_VISION_ADMIN_TOKEN`.
2. Send `Authorization: Bearer <token>` with each admin request.
3. Read a set: `GET /v1/orgs/{org_id}/labels`. The answer has `custom`, `labels`, `topics` and `total_labels`.
4. Add a label: `POST /v1/orgs/{org_id}/labels` with `{"name", "topic", "prompt"?, "new_topic"?, "topic_parent"?}`.
5. Remove an added label: `DELETE /v1/orgs/{org_id}/labels/{label_id}`.
6. Store a correction for one org: `POST /v1/orgs/{org_id}/feedback` with the field `image` and the form field `label_id`.

```bash
curl -H "Authorization: Bearer $SYNTHWERK_VISION_ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"name": "rice paddy", "topic": "landscape"}' \
  http://localhost:8000/v1/orgs/283746192837461928/labels
```

## Rules and limits

| Rule | Value |
|---|---|
| Who calls | A holder of the admin token: an operator, or the studio BFF for an org owner or admin. Vision trusts the org id in the path. |
| Org id | `^[A-Za-z0-9_-]{1,64}$`. Else 422. A widget token with another org id gets the default set. |
| Backend | `siglip` only. `mobilenet` has a fixed label set (409). |
| Packaged labels | Cannot be removed (409). |
| Org services per worker | 64, least recently used out |
| Copy of the default set | Once, at the first change of the org |

## Events

| Type | When |
|---|---|
| None. The label-set-changed fact of synthwerk#44 has no contract yet. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 401 | None (FastAPI `detail`) | The bearer token is wrong. |
| 404 | None (FastAPI `detail`) | No admin token is set, or the runtime label is not in the org set. |
| 409 | None (FastAPI `detail`) | Fixed-label backend, label clash, unknown topic, packaged label, or a correction for a label outside the org set. |
| 422 | None (FastAPI `detail`) | The org id or the body is not valid. |
| 503 | None (FastAPI `detail`) | The model is not loaded, or the feedback store is not available. |

## Tests

- `tests/test_label_sets.py` — `two orgs - same image - different labels`
- `tests/test_label_sets.py` — `first org change - copies the default set - then both change alone`
- `tests/test_label_sets.py` — `corrections - stored per org - other org gets none`
- `tests/test_label_sets.py` — `own correction - wins over a default correction`
- `tests/test_label_sets.py` — `org set - written by another worker - picked up`
- `tests/test_label_sets.py` — `global labels.yaml - at startup - moved into the default set`
- `tests/test_label_sets.py` — `feedback database of an older version - gets the org column`
- `tests/test_siglip.py` — `for_taxonomy - label set another worker built - no text tower`

## Open points

- No `label-set-changed` event yet. Vision has no outbox. The event name and schema go into `synthwerk-contracts` first.
- No delete of a whole org set ("back to default") yet.
- A user deleted from an org keeps the org's corrections. They belong to the org, not to the user.

## Changes

| Date | Change |
|---|---|
| 2026-10-30 | First version: default set, org sets, corrections per org, migration of `labels.yaml` (synthwerk#44). |
| 2026-10-30 | `routes.py` stores uploads under the widget token org (#77). Label sets and corrections do not change. |
