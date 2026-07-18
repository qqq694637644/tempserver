from __future__ import annotations

import errno
import math
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings
from app.storage import FileStore


BASE_DIR = Path(__file__).resolve().parent
MAX_FLASH_MESSAGE_LENGTH = 420
MAX_ERROR_FILENAME_LENGTH = 80


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, client_key: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[client_key]
            self._prune(attempts, now)
            if len(attempts) < self.max_attempts:
                if not attempts:
                    self._attempts.pop(client_key, None)
                return None
            return max(1, math.ceil(self.window_seconds - (now - attempts[0])))

    def record_failure(self, client_key: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[client_key]
            self._prune(attempts, now)
            attempts.append(now)

    def reset(self, client_key: str) -> None:
        with self._lock:
            self._attempts.pop(client_key, None)

    def _prune(self, attempts: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()


def _constant_time_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _valid_csrf(request: Request, submitted_token: str) -> bool:
    stored_token = request.session.get("csrf_token")
    return (
        isinstance(stored_token, str)
        and bool(stored_token)
        and _constant_time_equal(stored_token, submitted_token)
    )


def _is_admin(request: Request) -> bool:
    return request.session.get("is_admin") is True


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url_for("admin_login"), status_code=303)


def _client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def _set_flash(request: Request, kind: str, message: str) -> None:
    request.session["flash"] = {
        "kind": kind,
        "message": message[:MAX_FLASH_MESSAGE_LENGTH],
    }


def _short_filename(filename: str | None) -> str:
    value = filename or "未命名文件"
    if len(value) <= MAX_ERROR_FILENAME_LENGTH:
        return value
    return value[: MAX_ERROR_FILENAME_LENGTH - 1] + "…"


def _friendly_storage_error(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "存储目录暂时被占用或没有写入权限，请稍后重试"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "磁盘空间不足"
    if isinstance(exc, ValueError):
        return str(exc)
    return "存储操作失败，请稍后重试"


def create_app(settings: Settings) -> FastAPI:
    file_store = FileStore(settings.storage_dir)
    login_limiter = LoginRateLimiter(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
    )

    app = FastAPI(title="tempserver", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage_dir = file_store.storage_dir
    app.state.file_store = file_store
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tempserver_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.get("/", name="home")
    async def home(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"files": file_store.list_files()},
        )

    @app.get("/files/{filename}", name="download_file")
    async def download_file(filename: str):
        try:
            path = file_store.resolve_download(filename)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="File not found") from exc
        if path is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            path=path,
            filename=filename,
            media_type="application/octet-stream",
        )

    @app.get("/admin/login", name="admin_login")
    async def admin_login(request: Request):
        if _is_admin(request):
            return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf_token": _csrf_token(request), "error": None},
        )

    @app.post("/admin/login", name="admin_login_submit")
    async def admin_login_submit(
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()],
    ):
        if not _valid_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

        client_key = _client_key(request)
        retry_after = login_limiter.retry_after(client_key)
        if retry_after is not None:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": _csrf_token(request),
                    "error": f"登录尝试过多，请在 {retry_after} 秒后重试",
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        username_matches = _constant_time_equal(username, settings.admin_username)
        password_matches = _constant_time_equal(password, settings.admin_password)
        if not (username_matches and password_matches):
            login_limiter.record_failure(client_key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": _csrf_token(request),
                    "error": "用户名或密码错误",
                },
                status_code=401,
            )

        login_limiter.reset(client_key)
        request.session.clear()
        request.session["is_admin"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)

    @app.get("/admin", name="admin_dashboard")
    async def admin_dashboard(request: Request):
        if not _is_admin(request):
            return _login_redirect(request)
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={
                "files": file_store.list_files(),
                "csrf_token": _csrf_token(request),
                "flash": flash,
            },
        )

    @app.post("/admin/upload", name="upload_files")
    async def upload_files(
        request: Request,
        files: Annotated[list[UploadFile], File()],
        csrf_token: Annotated[str, Form()],
    ):
        if not _is_admin(request):
            return _login_redirect(request)
        if not _valid_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

        uploaded_count = 0
        failed_count = 0
        first_error: str | None = None
        for upload in files:
            try:
                await file_store.save_upload(upload)
                uploaded_count += 1
            except (OSError, RuntimeError, ValueError) as exc:
                failed_count += 1
                if first_error is None:
                    first_error = (
                        f"{_short_filename(upload.filename)}："
                        f"{_friendly_storage_error(exc)}"
                    )

        if failed_count:
            message = f"成功 {uploaded_count} 个，失败 {failed_count} 个"
            if first_error:
                message += f"。首个错误：{first_error}"
            _set_flash(request, "error", message)
        else:
            _set_flash(request, "success", f"已上传或覆盖 {uploaded_count} 个文件")
        return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)

    @app.post("/admin/files/{filename}/delete", name="delete_file")
    async def delete_file(
        request: Request,
        filename: str,
        csrf_token: Annotated[str, Form()],
    ):
        if not _is_admin(request):
            return _login_redirect(request)
        if not _valid_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

        try:
            file_store.delete(filename)
            _set_flash(request, "success", f"已删除 {_short_filename(filename)}")
        except FileNotFoundError:
            _set_flash(request, "error", "文件不存在或已经删除")
        except (OSError, RuntimeError, ValueError) as exc:
            _set_flash(request, "error", _friendly_storage_error(exc))
        return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)

    @app.post("/admin/logout", name="admin_logout")
    async def admin_logout(
        request: Request,
        csrf_token: Annotated[str, Form()],
    ):
        if _is_admin(request) and not _valid_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        request.session.clear()
        return RedirectResponse(request.url_for("home"), status_code=303)

    return app
