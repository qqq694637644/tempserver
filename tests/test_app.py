from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from app.storage import (
    BLOB_SUFFIX,
    DATA_DIRECTORY_NAME,
    LEGACY_UPLOAD_TEMP_PREFIX,
    STALE_FILE_MAX_AGE_SECONDS,
    UPLOAD_TEMP_PREFIX,
)


CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
DEFAULT_PASSWORD = "correct-horse-battery-staple"
DEFAULT_SECRET = "test-session-secret-with-enough-random-characters-123"


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "admin_username": "admin",
        "admin_password": DEFAULT_PASSWORD,
        "storage_dir": tmp_path / "files",
        "session_secret": DEFAULT_SECRET,
        "session_cookie_secure": False,
        "login_max_attempts": 5,
        "login_window_seconds": 300,
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
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="FILE_STORAGE_DIR"):
        Settings.from_env()


def test_public_file_list_and_download(app_client) -> None:
    client, storage_dir = app_client
    (storage_dir / "说明.txt").write_bytes("公开内容".encode("utf-8"))

    page = client.get("/")
    assert page.status_code == 200
    assert "说明.txt" in page.text
    assert "12 B" in page.text
    assert 'class="admin-corner-entry"' in page.text

    download = client.get("/files/%E8%AF%B4%E6%98%8E.txt")
    assert download.status_code == 200
    assert download.content == "公开内容".encode("utf-8")
    assert "attachment" in download.headers["content-disposition"]


def test_admin_routes_require_login(app_client) -> None:
    client, _ = app_client

    dashboard = client.get("/admin", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers["location"].endswith("/admin/login")

    upload = client.post(
        "/admin/upload",
        data={"csrf_token": "not-valid"},
        files={"files": ("blocked.txt", b"blocked", "text/plain")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    assert upload.headers["location"].endswith("/admin/login")


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


def test_open_windows_download_does_not_block_overwrite_or_delete(app_client) -> None:
    client, storage_dir = app_client
    legacy_path = storage_dir / "busy.bin"
    legacy_path.write_bytes(b"legacy-version")
    _login(client)
    csrf_token = _csrf(client.get("/admin"))

    with legacy_path.open("rb") as legacy_download:
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
        with current_path.open("rb") as current_download:
            deletion = client.post(
                "/admin/files/busy.bin/delete",
                data={"csrf_token": csrf_token},
                follow_redirects=False,
            )
            assert deletion.status_code == 303
            assert client.get("/files/busy.bin").status_code == 404
            assert current_download.read() == b"new-version"

    assert legacy_path.exists()


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


def test_stale_temporary_and_orphan_files_are_cleaned_on_startup(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "files"
    data_dir = storage_dir / DATA_DIRECTORY_NAME
    data_dir.mkdir(parents=True)

    stale_root_temp = storage_dir / f"{LEGACY_UPLOAD_TEMP_PREFIX}stale"
    stale_upload_temp = data_dir / f"{UPLOAD_TEMP_PREFIX}stale"
    stale_orphan_blob = data_dir / f"orphan{BLOB_SUFFIX}"
    recent_upload_temp = data_dir / f"{UPLOAD_TEMP_PREFIX}recent"
    for path in (
        stale_root_temp,
        stale_upload_temp,
        stale_orphan_blob,
        recent_upload_temp,
    ):
        path.write_bytes(b"temporary")

    old_timestamp = time.time() - STALE_FILE_MAX_AGE_SECONDS - 60
    for path in (stale_root_temp, stale_upload_temp, stale_orphan_blob):
        os.utime(path, (old_timestamp, old_timestamp))

    create_app(_settings(tmp_path, storage_dir=storage_dir))

    assert not stale_root_temp.exists()
    assert not stale_upload_temp.exists()
    assert not stale_orphan_blob.exists()
    assert recent_upload_temp.exists()


def test_many_upload_errors_keep_session_cookie_small(app_client) -> None:
    client, _ = app_client
    _login(client)
    csrf_token = _csrf(client.get("/admin"))
    files = [
        (
            "files",
            (f"{'x' * 180}?{number}.txt", b"x", "text/plain"),
        )
        for number in range(40)
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
    assert "失败 40 个" in dashboard.text


def test_secure_cookie_setting_adds_secure_attribute(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, session_cookie_secure=True))

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/admin/login")

    assert "secure" in response.headers["set-cookie"].casefold()


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
