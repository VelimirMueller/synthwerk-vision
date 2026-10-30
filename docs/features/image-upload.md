---
title: Image upload
slug: image-upload
status: beta
epic: E0
owner: vision
paths: ["src/synthwerk_vision/storage.py"]
api: ["POST /v1/images"]
events: []
mcp: []
updated: 2026-10-30
---

# Image upload

> **In 30 seconds**
> - The service stores one uploaded image for the caller's org and returns a new id.
> - Two backends: a local folder (default, development) or an S3-compatible bucket (Supabase Storage in dev, stg and prd).
> - Each org has a byte and a file quota, and all orgs together have a byte cap. When one is full, the answer is 507.
> - No endpoint reads the image back yet.

## What it does

- The service checks the image like a classification does: size, format and pixel count.
- The service stores the image under the key `<org>/<yyyy>/<mm>/<uuid4>.<ext>`.
  - `<org>` is the `org_id` of the widget token. With auth disabled (`local`, `ci`), it is `local`.
  - `<yyyy>/<mm>` is the upload month in UTC.
  - The extension comes from the decoded format. The service never uses the client file name.
- The service refuses an org id that is not 1 to 64 characters of `[A-Za-z0-9_-]` (500, nothing stored).
- The service keeps usage counters in each process: files and bytes per org, and bytes for all orgs.
  - A counter starts from one scan: local walks the folder, S3 lists the prefix `<org>/` or the whole bucket. One request per 1000 objects; the listing stops once a quota is reached. No usage table to keep in step.
  - The service scans again when a counter is older than 10 s.
  - Near a limit, the service scans again before each write: when the counter plus the upload plus a margin passes the limit. The margin is `MAX_UPLOAD_BYTES` × `UPLOAD_WRITERS` bytes and `UPLOAD_WRITERS` files.
  - Far from a limit, a save costs no scan.
  - Near a limit, each counter is scanned at most once per second, by one request at a time. Other requests reuse that scan. When a scan younger than 1 s already shows the limit reached, the answer is 507 without a new scan.
  - With N writers, a quota can go over by at most N − 1 uploads: each writer can pass the check at the same moment (the same rule as #19).
- Bytes of writes in flight have their own counter. A scan during a write does not lose or double-count them.
- When the new upload goes over the org quota or the total cap, the service stores nothing and answers 507.
- Each org has its own lock. A slow write for one org does not block the uploads of other orgs.
- A failed write gives its bytes back to the total counter.
- Local backend:
  - The service opens each file with `O_EXCL`, so an upload cannot overwrite a file.
  - When a write fails, the service removes the partial file.
- S3 backend:
  - The object gets the content type of the decoded image (`image/jpeg`, `image/png`, `image/webp`).
  - The service sets no ACL. The object is as private as the bucket.
  - The service uses path-style requests, so the endpoint can have a path (Supabase: `/storage/v1/s3`).
  - A request that fails or times out answers 503. The service tries each request at most 3 times, 10 s in total.
  - `/readyz` has a `storage` check. See [Health and telemetry](health-and-telemetry.md).
- Nothing deletes old uploads. Free space by hand, or raise the quota.

## How to use

1. Send the image as `multipart/form-data` in the field `image` to `POST /v1/images`, with a widget token. See [Widget token](widget-token-auth.md).
2. Keep the `id` from the response.

Switch to the S3 backend:

1. Create a private bucket. Create S3 access keys for it (Supabase: Project Settings, Storage, S3 access keys).
2. Set `SYNTHWERK_VISION_STORAGE_BACKEND=s3` and the `SYNTHWERK_VISION_S3_*` settings below.
3. Give each key as a value or as a file (`*_FILE`), not both. Never commit a key.
4. Check that `/readyz` shows `storage: ok`.

## Rules and limits

| Rule | Value |
|---|---|
| Backend | `local` (`SYNTHWERK_VISION_STORAGE_BACKEND`), or `s3` |
| Local folder | `uploads` (`SYNTHWERK_VISION_UPLOAD_DIR`) |
| S3 endpoint | `SYNTHWERK_VISION_S3_ENDPOINT`, `https` outside `local` and `ci`. Supabase: `https://<project>.storage.supabase.co/storage/v1/s3` |
| S3 region | `SYNTHWERK_VISION_S3_REGION`, the region of the project, such as `eu-central-1` |
| S3 bucket | `SYNTHWERK_VISION_S3_BUCKET`, private |
| S3 access key id | `SYNTHWERK_VISION_S3_ACCESS_KEY_ID` or `SYNTHWERK_VISION_S3_ACCESS_KEY_ID_FILE` |
| S3 secret access key | `SYNTHWERK_VISION_S3_SECRET_ACCESS_KEY` or `SYNTHWERK_VISION_S3_SECRET_ACCESS_KEY_FILE` |
| Image formats | JPEG, PNG, WebP |
| Maximum upload size | 5 MiB (`SYNTHWERK_VISION_MAX_UPLOAD_BYTES`) |
| Byte quota per org | 1 GiB (`SYNTHWERK_VISION_UPLOAD_QUOTA_BYTES`) |
| File quota per org | 10 000 (`SYNTHWERK_VISION_UPLOAD_QUOTA_FILES`) |
| Byte cap for all orgs | 5 GiB (`SYNTHWERK_VISION_UPLOAD_QUOTA_TOTAL_BYTES`). Keep it below the bucket or disk size (Supabase free plan: 1 GB) |
| Counter refresh | 10 s, and near a limit at most once per second |
| Writers for the margin | 2 (`SYNTHWERK_VISION_UPLOAD_WRITERS`): worker processes × instances on the same store |
| S3 timeouts | Connect 2 s, request 10 s, 2 retries |
| Read back by id | Not available |

- With `s3`, a missing or bad setting stops the start. The error names the setting, never a value.

## Events

| Type | When |
|---|---|
| None. | |

## Errors

| Status | Problem type | Cause |
|---|---|---|
| 401 | None (FastAPI `detail`) | No valid widget token. See [Widget token](widget-token-auth.md). |
| 403 | None (FastAPI `detail`) | `Origin` is not the origin of the widget token. |
| 413 | None (FastAPI `detail`) | The image is larger than the upload limit or the pixel limit. |
| 415 | None (FastAPI `detail`) | The bytes are not a JPEG, PNG or WebP image. |
| 422 | None (FastAPI `detail`) | The request has no `image` field, or the file is empty. |
| 500 | None (text `Internal Server Error`) | The org id of the token is not usable as a key. |
| 503 | None (FastAPI `detail`) | The token keys cannot be loaded, or the bucket does not answer or refuses the request. |
| 507 | None (FastAPI `detail`) | The org reached its byte or file quota, or all orgs together reached the byte cap. |

## Tests

- `tests/test_api.py` — `upload - valid image - stored under a generated id`
- `tests/test_api.py` — `upload - client file name - never reaches the file system`
- `tests/test_api.py` — `upload - quota reached - next upload refused with 507`
- `tests/test_storage.py` — `object key - org, year, month, id, extension`
- `tests/test_storage.py` — `object key - unsafe org id or extension - refused`
- `tests/test_storage.py` — `local - byte or file quota of an org reached - next save refused, other org stored`
- `tests/test_storage.py` — `local - files of other workers - counted in the quota`
- `tests/test_storage.py` — `local - write error - partial file removed`
- `tests/test_storage.py` — `local - file deleted during the scan - skipped`
- `tests/test_storage.py` — `local - total cap reached by all orgs - next save of any org refused`
- `tests/test_storage.py` — `local - failed write - reserved bytes given back`
- `tests/test_storage.py` — `local - counter older than 10 s - scanned again`
- `tests/test_storage.py` — `local - near the limit with a stale cache - scanned again and refused`
- `tests/test_storage.py` — `local - failed write during a rescan - total stays correct`
- `tests/test_storage.py` — `s3 - total near the limit with a stale cache - scanned again and refused`
- `tests/test_storage.py` — `s3 - 50 parallel saves near the limit - one scan per counter`
- `tests/test_storage.py` — `s3 - total cap reached by all orgs - next save refused`
- `tests/test_storage.py` — `s3 - slow put of one org - other org not blocked`
- `tests/test_storage.py` — `s3 - quota reached on the first page - listing stops`
- `tests/test_storage.py` — `upload - total cap reached - 507`
- `tests/test_storage.py` — `s3 - save - private object with the content type`
- `tests/test_storage.py` — `s3 - byte or file quota of an org reached - next save refused, other org stored`
- `tests/test_storage.py` — `s3 - objects of other instances over several pages - counted`
- `tests/test_storage.py` — `s3 - endpoint down or bucket missing - 503`
- `tests/test_storage.py` — `s3 - keys from files - read`
- `tests/test_storage.py` — `s3 - bad settings - start refused, no secret in the error`
- `tests/test_storage.py` — `upload - auth disabled - stored under org local`
- The S3 tests run against moto's S3 server in the test process. No Docker, no Supabase.

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | Rename to `synthwerk_vision`. The settings use the prefix `SYNTHWERK_VISION_`. |
| 2026-10-29 | Byte and file quota for the upload folder; 507 when it is full (#19). |
| 2026-10-29 | The route needs a widget token (#76). |
| 2026-10-30 | Keys per org (`<org>/<yyyy>/<mm>/<uuid4>.<ext>`), quota per org plus a byte cap for all orgs, usage counters with a lock per org, new S3 backend for Supabase Storage, `/readyz` check `storage`, 503 when the bucket fails (#77). |
