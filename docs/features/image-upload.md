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
updated: 2026-10-29
---

# Image upload

> **In 30 seconds**
> - The service stores one uploaded image on the local disk and returns a new id.
> - No endpoint reads the image back yet.
> - A byte and a file quota bound the upload folder. When it is full, the answer is 507.

## What it does

- The service checks the image like a classification does: size, format and pixel count.
- The service names the file `<uuid4>.<ext>`. The extension comes from the decoded format.
- The service never uses the client file name.
- The service opens each file with `O_EXCL`, so an upload cannot overwrite a file.
- When a write fails, the service removes the partial file.
- Before each write, the service adds up the files and bytes in the upload folder.
- When the new upload goes over a quota, the service stores nothing and answers 507.
- Files from other worker processes count too. Each other worker can add one upload over the quota.
- Nothing deletes old uploads. Free space by hand, or raise the quota.

## How to use

1. Send the image as `multipart/form-data` in the field `image` to `POST /v1/images`, with a widget token. See [Widget token](widget-token-auth.md).
2. Keep the `id` from the response.

## Rules and limits

| Rule | Value |
|---|---|
| Storage folder | `uploads` (`SYNTHWERK_VISION_UPLOAD_DIR`) |
| Image formats | JPEG, PNG, WebP |
| Maximum upload size | 5 MiB (`SYNTHWERK_VISION_MAX_UPLOAD_BYTES`) |
| Byte quota for the folder | 1 GiB (`SYNTHWERK_VISION_UPLOAD_QUOTA_BYTES`) |
| File quota for the folder | 10 000 (`SYNTHWERK_VISION_UPLOAD_QUOTA_FILES`) |
| Read back by id | Not available |

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
| 503 | None (FastAPI `detail`) | The token keys cannot be loaded. |
| 507 | None (FastAPI `detail`) | The upload folder reached its byte or file quota. |

## Tests

- `tests/test_api.py` — `upload - valid image - stored under a generated id`
- `tests/test_api.py` — `upload - client file name - never reaches the file system`
- `tests/test_units.py` — `storage - write error - partial file removed`
- `tests/test_api.py` — `upload - quota reached - next upload refused with 507`
- `tests/test_units.py` — `storage - byte or file quota reached - next save refused`
- `tests/test_units.py` — `storage - files of other workers - counted in the quota`

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | Rename to `synthwerk_vision`. The settings use the prefix `SYNTHWERK_VISION_`. |
| 2026-10-29 | Byte and file quota for the upload folder; 507 when it is full (#19). |
| 2026-10-29 | The route needs a widget token (#76). |
