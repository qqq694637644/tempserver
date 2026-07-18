from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import app.factory as factory_module
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from app.config import Settings
from app.factory import LoginRateLimiter, create_app
from app.storage import (
    BLOB_SUFFIX,
    DATA_DIRECTORY_NAME,
    MANIFEST_BACKUP_NAME,
    MANIFEST_NAME,
    LEGACY_UPLOAD_TEMP_PREFIX,
    STALE_FILE_MAX_AGE_SECONDS,
    UPLOAD_TEMP_PREFIX,
    FileStore,
)


CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
DEFAULT_PASSWORD = "correct-horse-battery-staple"
DEFAULT_SECRET = "N7vP2_xK9qLm4Rz8Tc1Bw5Yh0Ua3Se6DfGkJpVnM2Qr"


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "admin_username": "admin",
        "admin_password": DEFAULT_PASSWORD,
        "storage_dir": tmp_path / "files",
        "session_secret": DEFAULT_SECRET,
        "session_cookie_secure": False,
        "login_max_attempts": 5,
        "login_window_seconds": 300,
        "login_max_clients": 10_000,
        "session_max_age_seconds": 8 * 60 * 60,
        "max_upload_bytes": 100 * 1024 * 1024,
        "max_upload_request_bytes": 120 * 1024 * 1024,
        "max_files_per_upload": 20,
        "max_public_files": 500,
        "blob_gc_interval_seconds": 60,
        "blob_gc_grace_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def app_client(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        yield client, app.state.storage_dir


def _csrf(response) -> str:
    match = CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


def _login(
    client: TestClient,
    username: str = "admin",
    password: str = DEFAULT_PASSWORD,
) -> None:
    login_page = client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _csrf(login_page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin")


def test_example_configuration_cannot_start() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    values = dotenv_values(repository_root / ".env.example")

    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        Settings(
            admin_username=values.get("ADMIN_USERNAME") or "",
            admin_password=values.get("ADMIN_PASSWORD") or "",
            storage_dir=Path(values.get("FILE_STORAGE_DIR") or ""),
            session_secret=values.get("SESSION_SECRET") or "",
        )


def test_known_placeholders_and_weak_password_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="placeholder"):
        _settings(tmp_path, admin_password="change-this-password")

    with pytest.raises(RuntimeError, match="placeholder"):
        _settings(
            tmp_path,
            session_secret="replace-with-at-least-32-random-characters",
        )

    with pytest.raises(RuntimeError, match="at least 12"):
        _settings(tmp_path, admin_password="too-short")


@pytest.mark.parametrize(
    "predictable_secret",
    [
        "abcdefghabcdefghabcdefghabcdefghabcdefghabc",
        "1234567812345678123456781234567812345678123",
    ],
)
def test_predictable_session_secrets_are_rejected(
    tmp_path: Path,
    predictable_secret: str,
) -> None:
    with pytest.raises(RuntimeError, match="token_urlsafe"):
        _settings(tmp_path, session_secret=predictable_secret)


def test_runtime_gc_grace_period_cannot_be_disabled(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="between 30 and 86400"):
        _settings(tmp_path, blob_gc_grace_seconds=0)


def test_from_env_rejects_blank_storage_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "FILE_STORAGE_DIR",
        "SESSION_SECRET",
        "SESSION_COOKIE_SECURE",
        "LOGIN_MAX_ATTEMPTS",
        "LOGIN_WINDOW_SECONDS",
        "LOGIN_MAX_CLIENTS",
        "SESSION_MAX_AGE_SECONDS",
        "MAX_UPLOAD_BYTES",
        "MAX_UPLOAD_REQUEST_BYTES",
        "MAX_FILES_PER_UPLOAD",
        "MAX_PUBLIC_FILES",
        "BLOB_GC_INTERVAL_SECONDS",
        "BLOB_GC_GRACE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="FILE_STORAGE_DIR"):
        Settings.from_env()


def test_public_file_list_and_download(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))
    upload = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("说明.txt", "公开内容".encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    assert upload.status_code == 303

    page = client.get("/")
    assert page.status_code == 200
    assert "说明.txt" in page.text
    assert "12 B" in page.text
    assert 'class="admin-corner-entry"' in page.text

    download = client.get("/files/%E8%AF%B4%E6%98%8E.txt")
    assert download.status_code == 200
    assert download.content == "公开内容".encode("utf-8")
    assert "attachment" in download.headers["content-disposition"]


def test_admin_routes_require_login(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = app_client

    dashboard = client.get("/admin", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers["location"].endswith("/admin/login")

    async def fail_if_form_is_parsed(*args, **kwargs):
        raise AssertionError("anonymous upload must not parse multipart")

    monkeypatch.setattr(StarletteRequest, "form", fail_if_form_is_parsed)
    upload = client.post(
        "/admin/upload",
        data={"csrf_token": "not-valid"},
        files={"files": ("blocked.txt", b"blocked", "text/plain")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    assert upload.headers["location"].endswith("/admin/login")


def test_anonymous_large_multipart_creates_no_server_temp_file(app_client) -> None:
    client, _ = app_client
    multipart_dir = client.app.state.file_store.multipart_dir

    response = client.post(
        "/admin/upload",
        data={"csrf_token": "not-valid"},
        files={
            "files": (
                "anonymous.bin",
                b"x" * (2 * 1024 * 1024),
                "application/octet-stream",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/login")
    assert not list(multipart_dir.iterdir())


def test_upload_request_body_limit_rejects_before_multipart_parse(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        max_upload_bytes=1024 * 1024,
        max_upload_request_bytes=1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        response = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("too-big.bin", b"x" * (1024 * 1024), "application/octet-stream")},
        )

    assert response.status_code == 413
    assert "上传请求过大" in response.text
    assert not list(app.state.file_store.multipart_dir.iterdir())


def test_streamed_upload_without_content_length_is_still_limited(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        max_upload_bytes=1024 * 1024,
        max_upload_request_bytes=1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        boundary = "tempserver-boundary"
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
            f"{csrf_token}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="stream.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()

        response = client.post(
            "/admin/upload",
            content=iter([prefix, b"x" * 700_000, b"x" * 700_000, suffix]),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    assert response.status_code == 413
    assert not list(app.state.file_store.multipart_dir.iterdir())


def test_multipart_temp_directory_is_on_storage_drive(app_client) -> None:
    client, _ = app_client
    expected = str(client.app.state.file_store.multipart_dir)
    assert tempfile.gettempdir() == expected


def test_invalid_admin_password_is_rejected(app_client) -> None:
    client, _ = app_client
    login_page = client.get("/admin/login")

    response = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": "wrong-password",
            "csrf_token": _csrf(login_page),
        },
    )

    assert response.status_code == 401
    assert "用户名或密码错误" in response.text


def test_unicode_login_input_never_returns_500(app_client) -> None:
    client, _ = app_client
    login_page = client.get("/admin/login")

    response = client.post(
        "/admin/login",
        data={
            "username": "恶意用户💥",
            "password": "错误密码🔒",
            "csrf_token": _csrf(login_page),
        },
    )

    assert response.status_code == 401


def test_unicode_admin_credentials_are_supported(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        admin_username="管理员",
        admin_password="中文管理员密码-足够长-123",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        _login(client, username="管理员", password="中文管理员密码-足够长-123")
        assert client.get("/admin").status_code == 200


def test_login_attempts_are_rate_limited(app_client) -> None:
    client, _ = app_client
    login_page = client.get("/admin/login")
    csrf_token = _csrf(login_page)

    for _ in range(5):
        response = client.post(
            "/admin/login",
            data={
                "username": "admin",
                "password": "incorrect-password",
                "csrf_token": csrf_token,
            },
        )
        assert response.status_code == 401

    blocked = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": DEFAULT_PASSWORD,
            "csrf_token": csrf_token,
        },
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1


def test_login_rate_limiter_has_bounded_ttl_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(factory_module.time, "monotonic", lambda: now[0])
    limiter = LoginRateLimiter(max_attempts=5, window_seconds=10, max_clients=100)

    for number in range(250):
        limiter.record_failure(f"203.0.113.{number}")
    assert limiter.tracked_clients == 100

    now[0] += 20
    limiter.record_failure("198.51.100.1")
    assert limiter.tracked_clients == 1


def test_logout_revokes_replayed_session_cookie(app_client) -> None:
    client, _ = app_client
    _login(client)
    old_cookie = client.cookies.get("tempserver_session")
    assert old_cookie
    csrf_token = _csrf(client.get("/admin"))

    logout = client.post(
        "/admin/logout",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert logout.status_code == 303

    with TestClient(client.app) as replay_client:
        replay_client.cookies.set("tempserver_session", old_cookie)
        replay = replay_client.get("/admin", follow_redirects=False)

    assert replay.status_code == 303
    assert replay.headers["location"].endswith("/admin/login")


def test_admin_can_upload_overwrite_and_delete(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))

    first_upload = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("release.zip", b"first", "application/zip")},
        follow_redirects=False,
    )
    assert first_upload.status_code == 303
    assert client.get("/files/release.zip").content == b"first"

    second_upload = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("release.zip", b"second", "application/zip")},
        follow_redirects=False,
    )
    assert second_upload.status_code == 303
    assert client.get("/files/release.zip").content == b"second"

    deletion = client.post(
        "/admin/files/release.zip/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert deletion.status_code == 303
    assert client.get("/files/release.zip").status_code == 404
    assert "release.zip" not in client.get("/").text


def test_case_insensitive_unicode_normalized_name_overwrites(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))

    for name, content in (("Report.txt", b"first"), ("report.txt", b"second")):
        response = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": (name, content, "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 303

    page = client.get("/")
    assert page.text.count(">report.txt<") == 1
    assert "Report.txt" not in page.text
    assert client.get("/files/Report.txt").content == b"second"
    assert client.get("/files/report.txt").content == b"second"


def test_head_and_conditional_download_requests(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))
    content = b"conditional-content"
    client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("probe.bin", content, "application/octet-stream")},
        follow_redirects=False,
    )

    get_response = client.get("/files/probe.bin")
    head_response = client.head("/files/probe.bin")
    assert get_response.status_code == 200
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["content-length"] == str(len(content))
    assert head_response.headers["etag"] == get_response.headers["etag"]

    etag_response = client.get(
        "/files/probe.bin",
        headers={"If-None-Match": get_response.headers["etag"]},
    )
    modified_response = client.get(
        "/files/probe.bin",
        headers={"If-Modified-Since": get_response.headers["last-modified"]},
    )
    assert etag_response.status_code == 304
    assert etag_response.content == b""
    assert modified_response.status_code == 304


def test_duplicate_names_report_upload_items_and_unique_files(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))

    response = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files=[
            ("files", ("dup.txt", b"first", "text/plain")),
            ("files", ("dup.txt", b"second", "text/plain")),
        ],
        follow_redirects=False,
    )
    dashboard = client.get(response.headers["location"])

    assert "已处理 2 个上传项，最终涉及 1 个文件" in dashboard.text
    assert client.get("/files/dup.txt").content == b"second"


def test_file_and_batch_limits_have_chinese_admin_feedback(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        max_upload_bytes=1024 * 1024,
        max_upload_request_bytes=3 * 1024 * 1024,
        max_files_per_upload=2,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        too_many = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files=[
                ("files", ("one.txt", b"", "text/plain")),
                ("files", ("two.txt", b"", "text/plain")),
                ("files", ("three.txt", b"", "text/plain")),
            ],
            follow_redirects=False,
        )
        batch_feedback = client.get(too_many.headers["location"])
        assert "单次最多 2 个文件" in batch_feedback.text

        csrf_token = _csrf(client.get("/admin"))
        too_large = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={
                "files": (
                    "large.bin",
                    b"x" * (1024 * 1024 + 1),
                    "application/octet-stream",
                )
            },
            follow_redirects=False,
        )
        size_feedback = client.get(too_large.headers["location"])
        assert "文件超过单文件大小限制" in size_feedback.text


def test_public_file_count_limit_blocks_new_names_but_allows_overwrite(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path, max_public_files=2))
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        for name in ("one.txt", "two.txt"):
            response = client.post(
                "/admin/upload",
                data={"csrf_token": csrf_token},
                files={"files": (name, b"content", "text/plain")},
                follow_redirects=False,
            )
            assert response.status_code == 303

        blocked = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("three.txt", b"content", "text/plain")},
            follow_redirects=False,
        )
        blocked_page = client.get(blocked.headers["location"])
        assert "站点文件数量已达到上限" in blocked_page.text

        overwrite = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("one.txt", b"new", "text/plain")},
            follow_redirects=False,
        )
        assert overwrite.status_code == 303
        assert client.get("/files/one.txt").content == b"new"


def test_version_mapping_and_deletion_persist_across_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_app = create_app(settings)
    with TestClient(first_app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        response = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("persistent.bin", b"version-one", "application/octet-stream")},
            follow_redirects=False,
        )
        assert response.status_code == 303

    second_app = create_app(settings)
    with TestClient(second_app) as client:
        assert client.get("/files/persistent.bin").content == b"version-one"
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        response = client.post(
            "/admin/files/persistent.bin/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303

    third_app = create_app(settings)
    with TestClient(third_app) as client:
        assert client.get("/files/persistent.bin").status_code == 404


def test_primary_manifest_loss_recovers_from_atomic_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("critical.txt", b"critical", "text/plain")},
            follow_redirects=False,
        )

    manifest = settings.storage_dir / MANIFEST_NAME
    backup = settings.storage_dir / MANIFEST_BACKUP_NAME
    assert manifest.exists() and backup.exists()
    manifest.unlink()

    recovered_app = create_app(settings)
    with TestClient(recovered_app) as client:
        assert client.get("/files/critical.txt").content == b"critical"
    assert manifest.exists()


def test_corrupt_primary_manifest_recovers_from_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("recover.txt", b"recoverable", "text/plain")},
            follow_redirects=False,
        )

    manifest = settings.storage_dir / MANIFEST_NAME
    manifest.write_text("{not-json", encoding="utf-8")

    recovered_app = create_app(settings)
    with TestClient(recovered_app) as client:
        assert client.get("/files/recover.txt").content == b"recoverable"
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == 2


def test_newer_backup_generation_wins_over_stale_primary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_app = create_app(settings)
    with TestClient(first_app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("first.txt", b"first", "text/plain")},
            follow_redirects=False,
        )
    stale_primary = (settings.storage_dir / MANIFEST_NAME).read_bytes()

    second_app = create_app(settings)
    with TestClient(second_app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("second.txt", b"second", "text/plain")},
            follow_redirects=False,
        )

    (settings.storage_dir / MANIFEST_NAME).write_bytes(stale_primary)
    recovered_app = create_app(settings)
    with TestClient(recovered_app) as client:
        assert client.get("/files/first.txt").content == b"first"
        assert client.get("/files/second.txt").content == b"second"


def test_both_manifest_copies_missing_refuses_startup_and_preserves_blobs(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("critical.txt", b"critical", "text/plain")},
            follow_redirects=False,
        )

    blobs = list((settings.storage_dir / DATA_DIRECTORY_NAME).glob(f"*{BLOB_SUFFIX}"))
    assert len(blobs) == 1
    (settings.storage_dir / MANIFEST_NAME).unlink()
    (settings.storage_dir / MANIFEST_BACKUP_NAME).unlink()

    with pytest.raises(RuntimeError, match="manifest is missing"):
        create_app(settings)
    assert blobs[0].exists()


def test_deleted_legacy_file_does_not_reappear_after_primary_manifest_loss(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "files"
    storage_dir.mkdir()
    legacy_path = storage_dir / "published.txt"
    legacy_path.write_bytes(b"old-public-content")
    settings = _settings(tmp_path, storage_dir=storage_dir)

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/files/published.txt").status_code == 200
        _login(client)
        csrf_token = _csrf(client.get("/admin"))
        client.post(
            "/admin/files/published.txt/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert client.get("/files/published.txt").status_code == 404

    legacy_path.write_bytes(b"manually-restored-content")
    (storage_dir / MANIFEST_NAME).unlink()
    recovered_app = create_app(settings)
    with TestClient(recovered_app) as client:
        assert client.get("/files/published.txt").status_code == 404


def test_version_one_manifest_migrates_without_republishing_tombstones(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "files"
    data_dir = storage_dir / DATA_DIRECTORY_NAME
    data_dir.mkdir(parents=True)
    (data_dir / "mapped.blob").write_bytes(b"mapped")
    (storage_dir / "deleted.txt").write_bytes(b"must-stay-private")
    (storage_dir / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "Mapped.txt": {"blob": "mapped.blob", "size": 6}
                },
                "deleted": ["deleted.txt"],
            }
        ),
        encoding="utf-8",
    )

    app = create_app(_settings(tmp_path, storage_dir=storage_dir))
    with TestClient(app) as client:
        assert client.get("/files/mapped.txt").content == b"mapped"
        assert client.get("/files/deleted.txt").status_code == 404

    manifest = json.loads((storage_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 2


def test_storage_directory_allows_only_one_active_instance(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_app = create_app(settings)
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            create_app(settings)
    finally:
        first_app.state.file_store.close()

    third_app = create_app(settings)
    third_app.state.file_store.close()


def test_runtime_garbage_collection_retries_windows_open_blobs(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))
    store = client.app.state.file_store

    client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("gc.bin", b"version-one", "application/octet-stream")},
        follow_redirects=False,
    )
    first = store.resolve_download("gc.bin")
    assert first is not None

    with first.path.open("rb") as open_download:
        client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("gc.bin", b"version-two", "application/octet-stream")},
            follow_redirects=False,
        )
        second = store.resolve_download("gc.bin")
        assert second is not None
        client.post(
            "/admin/files/gc.bin/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        store.collect_garbage(0)
        assert open_download.read() == b"version-one"
        if os.name == "nt":
            assert first.path.exists()
        assert not second.path.exists()

    store.collect_garbage(0)
    assert not first.path.exists()


def test_manual_root_files_after_initialization_are_ignored(app_client) -> None:
    client, storage_dir = app_client
    manual = storage_dir / "manual.txt"
    manual.write_bytes(b"manual")

    assert "manual.txt" not in client.get("/").text
    assert client.get("/files/manual.txt").status_code == 404


def test_open_windows_download_does_not_block_overwrite_or_delete(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))

    initial = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("busy.bin", b"legacy-version", "application/octet-stream")},
        follow_redirects=False,
    )
    assert initial.status_code == 303
    legacy = client.app.state.file_store.resolve_download("busy.bin")
    assert legacy is not None

    with legacy.path.open("rb") as legacy_download:
        overwrite = client.post(
            "/admin/upload",
            data={"csrf_token": csrf_token},
            files={"files": ("busy.bin", b"new-version", "application/octet-stream")},
            follow_redirects=False,
        )
        assert overwrite.status_code == 303
        assert client.get("/files/busy.bin").content == b"new-version"
        assert legacy_download.read() == b"legacy-version"

        current_path = client.app.state.file_store.resolve_download("busy.bin")
        assert current_path is not None
        with current_path.path.open("rb") as current_download:
            deletion = client.post(
                "/admin/files/busy.bin/delete",
                data={"csrf_token": csrf_token},
                follow_redirects=False,
            )
            assert deletion.status_code == 303
            assert client.get("/files/busy.bin").status_code == 404
            assert current_download.read() == b"new-version"

    assert legacy.path.exists()


def test_upload_streams_files_larger_than_memory_chunk(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))
    content = b"x" * (2 * 1024 * 1024 + 17)

    response = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("large.bin", content, "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.get("/files/large.bin").content == content


def test_internal_temporary_files_are_hidden(app_client) -> None:
    client, storage_dir = app_client
    temporary_file = storage_dir / f"{LEGACY_UPLOAD_TEMP_PREFIX}in-progress"
    temporary_file.write_bytes(b"partial")

    page = client.get("/")
    download = client.get(f"/files/{temporary_file.name}")

    assert temporary_file.name not in page.text
    assert download.status_code == 404


def test_missing_manifest_with_internal_data_refuses_startup_without_deleting(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "files"
    data_dir = storage_dir / DATA_DIRECTORY_NAME
    data_dir.mkdir(parents=True)

    stale_orphan_blob = data_dir / f"orphan{BLOB_SUFFIX}"
    stale_orphan_blob.write_bytes(b"temporary")

    old_timestamp = time.time() - STALE_FILE_MAX_AGE_SECONDS - 60
    os.utime(stale_orphan_blob, (old_timestamp, old_timestamp))

    with pytest.raises(RuntimeError, match="manifest is missing"):
        create_app(_settings(tmp_path, storage_dir=storage_dir))

    assert stale_orphan_blob.exists()


def test_many_upload_errors_keep_session_cookie_small(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))
    files = [
        (
            "files",
            (f"{'x' * 180}?{number}.txt", b"x", "text/plain"),
        )
        for number in range(20)
    ]

    response = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(response.headers.get("set-cookie", "").encode("utf-8")) < 4096
    dashboard = client.get(response.headers["location"])
    assert "失败 20 个" in dashboard.text


def test_secure_cookie_setting_adds_secure_attribute(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, session_cookie_secure=True))

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/admin/login")

    assert "secure" in response.headers["set-cookie"].casefold()


def test_admin_pages_have_no_store_and_security_headers(app_client) -> None:
    client, _ = app_client
    login_page = client.get("/admin/login")
    assert login_page.headers["cache-control"] == "no-store"
    assert login_page.headers["x-frame-options"] == "DENY"
    assert login_page.headers["x-content-type-options"] == "nosniff"
    assert login_page.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in login_page.headers["content-security-policy"]

    _login(client)
    dashboard = client.get("/admin")
    assert dashboard.headers["cache-control"] == "no-store"
    assert "onsubmit=" not in dashboard.text
    assert "/static/admin.js" in dashboard.text


def test_storage_directory_disappearance_returns_503(app_client) -> None:
    client, storage_dir = app_client
    shutil.rmtree(client.app.state.file_store.data_dir)

    home = client.get("/")
    health = client.get("/healthz")

    assert home.status_code == 503
    assert "文件存储目录当前不可用" in home.text
    assert health.status_code == 503
    assert health.json() == {"status": "storage_unavailable"}


def test_storage_path_that_is_a_file_has_clear_startup_error(tmp_path: Path) -> None:
    storage_file = tmp_path / "not-a-directory"
    storage_file.write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError, match="FILE_STORAGE_DIR is not a directory"):
        create_app(_settings(tmp_path, storage_dir=storage_file))


def test_sensitive_env_file_in_storage_refuses_startup(tmp_path: Path) -> None:
    storage_dir = tmp_path / "files"
    storage_dir.mkdir()
    (storage_dir / ".env").write_text("SESSION_SECRET=leak", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Sensitive configuration file"):
        create_app(_settings(tmp_path, storage_dir=storage_dir))


def test_application_directory_cannot_be_public_storage(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    with pytest.raises(RuntimeError, match="dedicated directory"):
        create_app(_settings(tmp_path, storage_dir=repository_root))


def test_admin_mutations_require_valid_csrf(app_client) -> None:
    client, _ = app_client
    _login(client)

    response = client.post(
        "/admin/upload",
        data={"csrf_token": "中文伪造令牌"},
        files={"files": ("blocked.txt", b"blocked", "text/plain")},
    )

    assert response.status_code == 403
    assert client.get("/files/blocked.txt").status_code == 404


def test_path_traversal_filename_is_not_downloaded(app_client) -> None:
    client, storage_dir = app_client
    outside_file = storage_dir.parent / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    response = client.get("/files/..%5Csecret.txt")

    assert response.status_code == 404
    assert response.text != "secret"
