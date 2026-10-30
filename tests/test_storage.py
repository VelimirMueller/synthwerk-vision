"""Storage backends. The S3 tests run against moto's S3 server in this process: no Docker and
no real Supabase call."""

import asyncio
import os
import socket
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import obstore
import pytest
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from synthwerk_vision.main import create_app
from synthwerk_vision.platform.config import Settings, get_settings
from synthwerk_vision.storage import (
    LocalImageStorage,
    S3ImageStorage,
    StorageConfigError,
    StorageFullError,
    StorageUnavailableError,
    build_s3_store,
    build_storage,
    object_key,
)
from tests.conftest import FakeClassifier, make_image, widget_headers

REGION = "eu-central-1"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
PNG = {"extension": "png", "content_type": "image/png"}


def clock() -> datetime:
    return NOW


def files_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file())


# --- keys -------------------------------------------------------------------------------------


def test_object_key_org_year_month_id_extension() -> None:
    assert object_key("org-1", "abc", "png", NOW) == "org-1/2026/10/abc.png"


@pytest.mark.parametrize("org_id", ["", ".", "..", "../x", "a/b", "a\\b", "a b", "-a", "x" * 65])
def test_object_key_unsafe_org_id_refused(org_id: str) -> None:
    with pytest.raises(ValueError, match="org id"):
        object_key(org_id, "abc", "png", NOW)


def test_object_key_unknown_extension_refused() -> None:
    with pytest.raises(ValueError, match="extension"):
        object_key("org-1", "abc", "php", NOW)


# --- local backend ----------------------------------------------------------------------------


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def local(
    root: Path,
    *,
    max_bytes: int = 1024,
    max_files: int = 10,
    max_total_bytes: int = 4096,
    margin_bytes: int = 0,
    monotonic: FakeMonotonic | None = None,
) -> LocalImageStorage:
    return LocalImageStorage(
        root,
        max_bytes=max_bytes,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        margin_bytes=margin_bytes,
        clock=clock,
        monotonic=monotonic or FakeMonotonic(),
    )


def test_local_save_writes_under_org_key(tmp_path: Path) -> None:
    storage = local(tmp_path)

    first = storage.save(b"a", org_id="org-1", **PNG)
    second = storage.save(b"b", org_id="org-1", **PNG)

    assert first != second
    assert (tmp_path / "org-1" / "2026" / "10" / f"{first}.png").read_bytes() == b"a"


@pytest.mark.parametrize(
    ("max_bytes", "max_files", "second"),
    [(10, 100, b"bbbbbb"), (100, 1, b"b")],
    ids=["byte quota", "file quota"],
)
def test_local_quota_reached_next_save_of_org_refused(
    tmp_path: Path, max_bytes: int, max_files: int, second: bytes
) -> None:
    storage = local(tmp_path, max_bytes=max_bytes, max_files=max_files)
    storage.save(b"aaaaa", org_id="org-1", **PNG)

    with pytest.raises(StorageFullError, match="quota"):
        storage.save(second, org_id="org-1", **PNG)
    assert storage.save(second, org_id="org-2", **PNG)  # other orgs keep their own quota
    assert len(files_in(tmp_path)) == 2


def test_local_quota_counts_files_of_other_workers(tmp_path: Path) -> None:
    older = tmp_path / "org-1" / "2026" / "09"
    older.mkdir(parents=True)
    (older / "written-by-another-worker.png").write_bytes(b"x" * 8)
    storage = local(tmp_path, max_bytes=10)

    with pytest.raises(StorageFullError):
        storage.save(b"abc", org_id="org-1", **PNG)
    assert storage.save(b"ab", org_id="org-1", **PNG)


def test_local_total_cap_reached_save_of_any_org_refused(tmp_path: Path) -> None:
    storage = local(tmp_path, max_bytes=100, max_total_bytes=10)
    storage.save(b"aaaa", org_id="org-1", **PNG)
    storage.save(b"bbbb", org_id="org-2", **PNG)

    with pytest.raises(StorageFullError, match="quota"):
        storage.save(b"ccc", org_id="org-3", **PNG)  # each org is far below its own quota
    assert storage.save(b"cc", org_id="org-3", **PNG)
    assert len(files_in(tmp_path)) == 3


def test_local_failed_write_releases_total_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = local(tmp_path, max_total_bytes=4)
    real_fsync = os.fsync
    calls = iter([OSError("disk full")])

    def fail_once(fd: int) -> None:
        error = next(calls, None)
        if error is not None:
            raise error
        real_fsync(fd)

    monkeypatch.setattr("synthwerk_vision.storage.os.fsync", fail_once)

    with pytest.raises(OSError, match="disk full"):
        storage.save(b"aaaa", org_id="org-1", **PNG)
    assert storage.save(b"aaaa", org_id="org-1", **PNG)  # the failed bytes were given back


def test_local_counters_scanned_again_after_refresh_interval(tmp_path: Path) -> None:
    monotonic = FakeMonotonic()
    storage = local(tmp_path, max_bytes=10, monotonic=monotonic)
    storage.save(b"a", org_id="org-1", **PNG)
    other = tmp_path / "org-1" / "2026" / "09"
    other.mkdir(parents=True)
    (other / "from-another-instance.png").write_bytes(b"x" * 8)

    assert storage.save(b"a", org_id="org-1", **PNG)  # counter not stale: 2 of 10 bytes
    monotonic.now += LocalImageStorage.REFRESH_S + 1
    with pytest.raises(StorageFullError):
        storage.save(b"a", org_id="org-1", **PNG)  # rescanned: 10 of 10 bytes
    monotonic.now += 60 * LocalImageStorage.REFRESH_S + 1
    storage.save(b"a", org_id="org-2", **PNG)  # the idle counter of org-1 is dropped
    assert "org-1" not in storage._org_usage


def test_local_near_limit_stale_cache_scanned_again_and_refused(tmp_path: Path) -> None:
    monotonic = FakeMonotonic()
    storage = local(tmp_path, max_bytes=10, margin_bytes=4, monotonic=monotonic)
    storage.save(b"aa", org_id="org-1", **PNG)
    other = tmp_path / "org-1" / "2026" / "09"
    other.mkdir(parents=True)
    (other / "from-another-instance.png").write_bytes(b"x" * 6)
    assert storage.save(b"aa", org_id="org-1", **PNG)  # far from the limit: cache, no scan
    monotonic.now += LocalImageStorage.MIN_RESCAN_S

    with pytest.raises(StorageFullError):
        storage.save(b"aaa", org_id="org-1", **PNG)  # cache says 4 + 3; the scan says 10 + 3


def test_local_failed_write_during_rescan_leaves_total_correct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = FakeMonotonic()
    storage = local(tmp_path, max_total_bytes=100, monotonic=monotonic)
    entered, release = threading.Event(), threading.Event()
    real_put = LocalImageStorage._put

    def put(self: LocalImageStorage, key: str, data: bytes, content_type: str) -> None:
        if key.startswith("org-a/"):
            entered.set()
            assert release.wait(5)
            raise OSError("disk full")
        real_put(self, key, data, content_type)

    monkeypatch.setattr(LocalImageStorage, "_put", put)
    errors: list[BaseException] = []

    def save_a() -> None:
        try:
            storage.save(b"aaaaa", org_id="org-a", **PNG)
        except OSError as exc:
            errors.append(exc)

    slow = threading.Thread(target=save_a)
    slow.start()
    try:
        assert entered.wait(5)
        monotonic.now += LocalImageStorage.REFRESH_S + 1  # the next save scans the total again
        storage.save(b"bbb", org_id="org-b", **PNG)
    finally:
        release.set()
        slow.join(5)

    assert len(errors) == 1
    assert storage._total is not None
    on_disk = sum(p.stat().st_size for p in files_in(tmp_path))
    assert (storage._total.bytes, storage._in_flight, on_disk) == (3, 0, 3)


def test_local_quota_file_deleted_during_scan_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = tmp_path / "org-1" / "2026" / "09"
    older.mkdir(parents=True)
    (older / "deleted-by-another-worker.png").write_bytes(b"x")
    real_lstat = os.lstat

    def gone(path: str) -> os.stat_result:
        if path.endswith("deleted-by-another-worker.png"):
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr("synthwerk_vision.storage.os.lstat", gone)

    assert local(tmp_path, max_files=1).save(b"b", org_id="org-1", **PNG)  # not counted


def test_local_write_error_partial_file_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("synthwerk_vision.storage.os.fsync", fail)

    with pytest.raises(OSError, match="disk full"):
        local(tmp_path).save(b"a", org_id="org-1", **PNG)
    assert files_in(tmp_path) == []


def test_local_unsafe_org_id_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="org id"):
        local(tmp_path / "uploads").save(b"a", org_id="../escape", **PNG)
    assert files_in(tmp_path) == []


# --- s3 backend (moto) ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def s3_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3(s3_endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        region_name=REGION,
        aws_access_key_id="test-key-id",
        aws_secret_access_key="test-secret",
    )


@pytest.fixture
def bucket(s3: Any) -> str:
    name = f"uploads-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
    return name


@pytest.fixture
def s3_settings(settings: Settings, s3_endpoint: str, bucket: str) -> Settings:
    return settings.model_copy(
        update={
            "platform_env": "ci",
            "storage_backend": "s3",
            "s3_endpoint": s3_endpoint,
            "s3_region": REGION,
            "s3_bucket": bucket,
            "s3_access_key_id": SecretStr("test-key-id"),
            "s3_secret_access_key": SecretStr("test-secret"),
        }
    )


def s3_storage(
    settings: Settings,
    *,
    max_bytes: int = 1024,
    max_files: int = 10,
    max_total_bytes: int = 4096,
    margin_bytes: int = 0,
    monotonic: FakeMonotonic | None = None,
) -> S3ImageStorage:
    return S3ImageStorage(
        build_s3_store(settings),
        max_bytes=max_bytes,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        margin_bytes=margin_bytes,
        clock=clock,
        monotonic=monotonic or FakeMonotonic(),
    )


def keys(s3: Any, bucket: str) -> list[str]:
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", []))


def test_s3_save_puts_private_object_with_content_type(
    s3_settings: Settings, s3: Any, bucket: str
) -> None:
    image_id = s3_storage(s3_settings).save(
        b"webp-bytes", org_id="org-1", extension="webp", content_type="image/webp"
    )

    key = f"org-1/2026/10/{image_id}.webp"
    assert keys(s3, bucket) == [key]
    head = s3.head_object(Bucket=bucket, Key=key)
    assert (head["ContentType"], head["ContentLength"]) == ("image/webp", 10)
    grants = s3.get_object_acl(Bucket=bucket, Key=key)["Grants"]
    assert [g["Grantee"]["Type"] for g in grants] == ["CanonicalUser"]  # owner only, no public


@pytest.mark.parametrize(
    ("max_bytes", "max_files", "second"),
    [(10, 100, b"bbbbbb"), (100, 1, b"b")],
    ids=["byte quota", "file quota"],
)
def test_s3_quota_reached_next_save_of_org_refused(
    s3_settings: Settings, s3: Any, bucket: str, max_bytes: int, max_files: int, second: bytes
) -> None:
    storage = s3_storage(s3_settings, max_bytes=max_bytes, max_files=max_files)
    storage.save(b"aaaaa", org_id="org-1", **PNG)

    with pytest.raises(StorageFullError, match="quota"):
        storage.save(second, org_id="org-1", **PNG)
    assert storage.save(second, org_id="org-2", **PNG)  # other orgs keep their own quota
    assert len(keys(s3, bucket)) == 2


def test_s3_quota_counts_objects_of_other_instances_over_several_pages(
    s3_settings: Settings, s3: Any, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S3ImageStorage, "LIST_PAGE", 2)
    for month in ("08", "09"):
        for n in range(2):
            s3.put_object(Bucket=bucket, Key=f"org-1/2026/{month}/other-{n}.png", Body=b"x" * 2)
    s3.put_object(Bucket=bucket, Key="org-10/2026/10/not-org-1.png", Body=b"x" * 50)
    storage = s3_storage(s3_settings, max_bytes=100, max_files=5)

    assert storage.save(b"a", org_id="org-1", **PNG)  # 5th object: org-10 is not counted
    with pytest.raises(StorageFullError):
        storage.save(b"a", org_id="org-1", **PNG)


def test_s3_listing_stops_once_the_quota_is_reached(
    s3_settings: Settings, s3: Any, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S3ImageStorage, "LIST_PAGE", 2)
    for n in range(5):
        s3.put_object(Bucket=bucket, Key=f"org-1/2026/09/other-{n}.png", Body=b"x")
    pages: list[int] = []
    real_list = obstore.list

    def counting_list(store: Any, prefix: str | None = None, **kwargs: Any) -> Any:
        for chunk in real_list(store, prefix=prefix, **kwargs):
            pages.append(len(chunk))
            yield chunk

    monkeypatch.setattr("synthwerk_vision.storage.obstore.list", counting_list)

    with pytest.raises(StorageFullError):
        s3_storage(s3_settings, max_files=2).save(b"a", org_id="org-1", **PNG)
    assert pages == [2]  # one page of 2 reached the file quota; 3 objects never listed


def test_s3_total_cap_reached_save_of_any_org_refused(
    s3_settings: Settings, s3: Any, bucket: str
) -> None:
    s3.put_object(Bucket=bucket, Key="org-9/2026/10/old.png", Body=b"x" * 6)
    storage = s3_storage(s3_settings, max_bytes=100, max_total_bytes=10)
    storage.save(b"aaa", org_id="org-1", **PNG)

    with pytest.raises(StorageFullError, match="quota"):
        storage.save(b"bb", org_id="org-2", **PNG)
    assert len(keys(s3, bucket)) == 2


def test_s3_total_near_limit_stale_cache_scanned_again_and_refused(
    s3_settings: Settings, s3: Any, bucket: str
) -> None:
    monotonic = FakeMonotonic()
    storage = s3_storage(
        s3_settings, max_bytes=100, max_total_bytes=20, margin_bytes=16, monotonic=monotonic
    )
    storage.save(b"aa", org_id="org-1", **PNG)
    s3.put_object(Bucket=bucket, Key="org-9/2026/10/other-instance.png", Body=b"x" * 15)
    monotonic.now += S3ImageStorage.MIN_RESCAN_S

    with pytest.raises(StorageFullError):
        storage.save(b"aaaa", org_id="org-2", **PNG)  # cache says 2 + 4; the listing 17 + 4
    assert len(keys(s3, bucket)) == 2


def test_s3_fifty_parallel_near_limit_saves_cause_at_most_one_scan_per_counter(
    s3_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = FakeMonotonic()
    storage = s3_storage(
        s3_settings,
        max_bytes=45,
        max_files=100,
        max_total_bytes=1000,
        margin_bytes=1000,  # every save is near both limits
        monotonic=monotonic,
    )
    storage.save(b"a", org_id="org-1", **PNG)  # first scans of the org and the total
    monotonic.now += S3ImageStorage.MIN_RESCAN_S  # near the limit: one rescan is allowed
    scans: list[str | None] = []
    real_scan = S3ImageStorage._scan

    def counting_scan(
        self: S3ImageStorage, prefix: str | None, stop_files: int | None, stop_bytes: int
    ) -> tuple[int, int]:
        scans.append(prefix)
        return real_scan(self, prefix, stop_files, stop_bytes)

    monkeypatch.setattr(S3ImageStorage, "_scan", counting_scan)
    results: list[str] = []

    def save() -> None:
        try:
            storage.save(b"a", org_id="org-1", **PNG)
            results.append("stored")
        except StorageFullError:
            results.append("full")

    threads = [threading.Thread(target=save) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(map(str, scans)) == [
        "None",
        "org-1/",
    ]
    assert (results.count("stored"), results.count("full")) == (44, 6)  # 1 + 44 = 45 bytes


def test_s3_slow_put_of_one_org_does_not_block_another_org(
    s3_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = s3_storage(s3_settings)
    entered, release = threading.Event(), threading.Event()
    real_put = obstore.put

    def put(store: Any, key: str, data: bytes, **kwargs: Any) -> Any:
        if key.startswith("org-a/"):
            entered.set()
            assert release.wait(5)
        return real_put(store, key, data, **kwargs)

    monkeypatch.setattr("synthwerk_vision.storage.obstore.put", put)
    slow = threading.Thread(target=lambda: storage.save(b"a", org_id="org-a", **PNG))
    slow.start()
    try:
        assert entered.wait(5)
        finished: list[str] = []
        fast = threading.Thread(
            target=lambda: finished.append(storage.save(b"b", org_id="org-b", **PNG))
        )
        fast.start()
        fast.join(5)
        assert finished, "org-b waited for the slow put of org-a"
    finally:
        release.set()
        slow.join(5)


def test_s3_endpoint_down_save_unavailable(s3_settings: Settings) -> None:
    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    down = s3_settings.model_copy(update={"s3_endpoint": f"http://127.0.0.1:{port}"})

    with pytest.raises(StorageUnavailableError, match="not available"):
        s3_storage(down).save(b"a", org_id="org-1", **PNG)


def test_s3_reachable_only_for_existing_bucket(s3_settings: Settings) -> None:
    missing = s3_settings.model_copy(update={"s3_bucket": "no-such-bucket"})

    assert asyncio.run(s3_storage(s3_settings).reachable()) is True
    assert asyncio.run(s3_storage(missing).reachable()) is False


# --- settings ---------------------------------------------------------------------------------


def test_build_storage_default_is_local(settings: Settings) -> None:
    assert isinstance(build_storage(settings), LocalImageStorage)


def test_build_storage_s3_reads_keys_from_files(
    s3_settings: Settings, tmp_path: Path, s3: Any, bucket: str
) -> None:
    (tmp_path / "key-id").write_text("test-key-id\n")
    (tmp_path / "secret").write_text("test-secret\n")
    from_files = s3_settings.model_copy(
        update={
            "s3_access_key_id": None,
            "s3_secret_access_key": None,
            "s3_access_key_id_file": tmp_path / "key-id",
            "s3_secret_access_key_file": tmp_path / "secret",
        }
    )

    storage = build_storage(from_files)

    assert isinstance(storage, S3ImageStorage)
    storage.save(b"a", org_id="org-1", **PNG)
    assert len(keys(s3, bucket)) == 1


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"s3_endpoint": None}, "S3_ENDPOINT required"),
        ({"s3_region": None, "s3_bucket": None}, "S3_REGION and SYNTHWERK_VISION_S3_BUCKET"),
        ({"s3_endpoint": "ftp://storage.test"}, "http"),
        ({"s3_endpoint": "http://storage.test", "platform_env": "prd"}, "https in prd"),
        ({"s3_access_key_id": None}, "S3_ACCESS_KEY_ID or"),
        ({"s3_secret_access_key": SecretStr("  ")}, "S3_SECRET_ACCESS_KEY or"),
        ({"s3_access_key_id_file": Path("key-id")}, "not both"),
        (
            {"s3_secret_access_key": None, "s3_secret_access_key_file": Path("/no/such/file")},
            "cannot be read",
        ),
    ],
)
def test_build_storage_bad_s3_settings_refused(
    s3_settings: Settings, update: dict[str, Any], message: str
) -> None:
    with pytest.raises(StorageConfigError, match=message):
        build_storage(s3_settings.model_copy(update=update))


def test_s3_secret_never_in_config_error(s3_settings: Settings) -> None:
    bad = s3_settings.model_copy(update={"s3_access_key_id": None})

    with pytest.raises(StorageConfigError) as caught:
        build_storage(bad)
    assert "test-secret" not in str(caught.value)


def test_start_with_bad_s3_settings_refused(s3_settings: Settings) -> None:
    with pytest.raises(StorageConfigError):
        create_app(s3_settings.model_copy(update={"s3_bucket": None}))


# --- API with the s3 backend ------------------------------------------------------------------


def s3_client(settings: Settings) -> TestClient:
    app = create_app(settings, classifier=FakeClassifier())  # storage built from the settings
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, headers=widget_headers())


def test_upload_s3_backend_stored_under_token_org(
    s3_settings: Settings, s3: Any, bucket: str
) -> None:
    with s3_client(s3_settings) as client:
        response = client.post(
            "/v1/images", files={"image": ("photo.jpg", make_image("JPEG"), "image/png")}
        )

    assert response.status_code == 201
    [key] = keys(s3, bucket)
    assert key.startswith("org-1/") and key.endswith(f"/{response.json()['id']}.jpg")
    assert s3.head_object(Bucket=bucket, Key=key)["ContentType"] == "image/jpeg"
    assert not s3_settings.upload_dir.exists()


def test_upload_total_cap_reached_507(s3_settings: Settings) -> None:
    image = make_image()
    capped = s3_settings.model_copy(update={"upload_quota_total_bytes": len(image) + 1})

    with s3_client(capped) as client:
        first = client.post("/v1/images", files={"image": ("a.png", image, "image/png")})
        second = client.post("/v1/images", files={"image": ("b.png", image, "image/png")})

    assert (first.status_code, second.status_code) == (201, 507)
    assert second.json() == {"detail": "upload storage quota reached"}


def test_upload_s3_backend_down_503(s3_settings: Settings) -> None:
    missing = s3_settings.model_copy(update={"s3_bucket": "no-such-bucket"})

    with s3_client(missing) as client:
        response = client.post("/v1/images", files={"image": ("a.png", make_image(), "image/png")})

    assert response.status_code == 503
    assert response.json() == {"detail": "upload storage is not available"}


def test_readyz_s3_backend_reports_bucket(s3_settings: Settings) -> None:
    missing = s3_settings.model_copy(update={"s3_bucket": "no-such-bucket"})

    with s3_client(s3_settings) as ok, s3_client(missing) as broken:
        ready, degraded = ok.get("/readyz"), broken.get("/readyz")

    checks = {c["name"]: c for c in ready.json()["checks"]}
    assert (ready.status_code, checks["storage"]["status"]) == (200, "ok")
    storage = next(c for c in degraded.json()["checks"] if c["name"] == "storage")
    assert (degraded.status_code, degraded.json()["status"]) == (200, "degraded")
    assert storage["detail"] == "upload bucket not reachable"


def test_readyz_slow_bucket_degraded_not_down(
    s3_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(self: S3ImageStorage) -> bool:
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(S3ImageStorage, "reachable", slow)

    with s3_client(s3_settings) as client:
        response = client.get("/readyz")

    storage = next(c for c in response.json()["checks"] if c["name"] == "storage")
    assert (response.status_code, storage["status"]) == (200, "degraded")
    assert storage["detail"] == "upload bucket timeout"


def test_readyz_local_backend_has_no_storage_check(client: TestClient) -> None:
    names = [c["name"] for c in client.get("/readyz").json()["checks"]]

    assert "storage" not in names


def test_upload_auth_disabled_stored_under_local_org(settings: Settings) -> None:
    local_settings = settings.model_copy(update={"auth_disabled": True, "oidc_issuer": None})
    app = create_app(local_settings, classifier=FakeClassifier())

    with TestClient(app) as client:
        response = client.post("/v1/images", files={"image": ("a.png", make_image(), "image/png")})

    assert response.status_code == 201
    [path] = files_in(local_settings.upload_dir)
    assert path.relative_to(local_settings.upload_dir).parts[0] == "local"
