from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app


CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def app_client(tmp_path: Path):
    settings = Settings(
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        storage_dir=tmp_path / "files",
        session_secret="test-session-secret-that-is-long-enough",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, settings.storage_dir


def _csrf(response) -> str:
    match = CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


def _login(client: TestClient) -> None:
    login_page = client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": "correct-horse-battery-staple",
            "csrf_token": _csrf(login_page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin")


def test_public_file_list_and_download(app_client):
    client, storage_dir = app_client
    storage_dir.mkdir(parents=True, exist_ok=True)
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


def test_admin_routes_require_login(app_client):
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


def test_invalid_admin_password_is_rejected(app_client):
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


def test_admin_can_upload_overwrite_and_delete(app_client):
    client, storage_dir = app_client
    _login(client)
    dashboard = client.get("/admin")
    csrf_token = _csrf(dashboard)

    first_upload = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("release.zip", b"first", "application/zip")},
        follow_redirects=False,
    )
    assert first_upload.status_code == 303
    assert (storage_dir / "release.zip").read_bytes() == b"first"

    second_upload = client.post(
        "/admin/upload",
        data={"csrf_token": csrf_token},
        files={"files": ("release.zip", b"second", "application/zip")},
        follow_redirects=False,
    )
    assert second_upload.status_code == 303
    assert (storage_dir / "release.zip").read_bytes() == b"second"
    assert client.get("/files/release.zip").content == b"second"

    deletion = client.post(
        "/admin/files/release.zip/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert deletion.status_code == 303
    assert not (storage_dir / "release.zip").exists()


def test_upload_streams_files_larger_than_memory_chunk(app_client):
    client, storage_dir = app_client
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
    assert (storage_dir / "large.bin").read_bytes() == content


def test_internal_temporary_files_are_hidden(app_client):
    client, storage_dir = app_client
    temporary_file = storage_dir / ".tempserver-upload-in-progress"
    temporary_file.write_bytes(b"partial")

    page = client.get("/")
    download = client.get("/files/.tempserver-upload-in-progress")

    assert temporary_file.name not in page.text
    assert download.status_code == 404


def test_admin_mutations_require_valid_csrf(app_client):
    client, storage_dir = app_client
    _login(client)

    response = client.post(
        "/admin/upload",
        data={"csrf_token": "invalid"},
        files={"files": ("blocked.txt", b"blocked", "text/plain")},
    )

    assert response.status_code == 403
    assert not (storage_dir / "blocked.txt").exists()


def test_path_traversal_filename_is_not_downloaded(app_client):
    client, storage_dir = app_client
    outside_file = storage_dir.parent / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    response = client.get("/files/..%5Csecret.txt")

    assert response.status_code == 404
    assert response.text != "secret"

