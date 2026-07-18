from __future__ import annotations

import errno
import math
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import MEBIBYTE, Settings
from app.storage import (
    FileStore,
    PublicFileLimitError,
    StorageUnavailableError,
    UploadTooLargeError,
)


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
MAX_FLASH_MESSAGE_LENGTH = 420
MAX_ERROR_FILENAME_LENGTH = 80
SMALL_FORM_MAX_BYTES = 64 * 1024


class RequestBodyTooLarge(OSError):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, upload_max_bytes: int) -> None:
        self.app = app
        self.upload_max_bytes = upload_max_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/admin/upload":
            limit = self.upload_max_bytes
        elif path.startswith("/admin/"):
            limit = SMALL_FORM_MAX_BYTES
        else:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await PlainTextResponse(
                    "无效的 Content-Length",
                    status_code=400,
                    headers=_basic_security_headers(),
                )(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge("request body limit exceeded")
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse(
            "上传请求过大",
            status_code=413,
            headers=_basic_security_headers(),
        )(scope, receive, send)


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


class AdminSessionRegistry:
    def __init__(self, max_age_seconds: int) -> None:
        self.max_age_seconds = max_age_seconds
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        session_id = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + self.max_age_seconds
        with self._lock:
            self._prune_locked()
            self._sessions[session_id] = expires_at
        return session_id

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            self._prune_locked()
            expires_at = self._sessions.get(session_id)
            return expires_at is not None and expires_at > time.monotonic()

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, expires_at in self._sessions.items()
            if expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


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
    if isinstance(exc, StorageUnavailableError):
        return "文件存储目录当前不可用"
    if isinstance(exc, UploadTooLargeError):
        return str(exc)
    if isinstance(exc, PublicFileLimitError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "存储目录暂时被占用或没有写入权限，请稍后重试"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "磁盘空间不足"
    if isinstance(exc, ValueError):
        return str(exc)
    return "存储操作失败，请稍后重试"


def _basic_security_headers() -> dict[str, str]:
    return {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def create_app(settings: Settings) -> FastAPI:
    file_store = FileStore(
        settings.storage_dir,
        protected_paths=(BASE_DIR, PROJECT_DIR),
    )
    # Starlette spools multipart files larger than 1 MiB through tempfile. Keep
    # those bounded temporary files on the configured storage drive, not %TEMP%.
    tempfile.tempdir = str(file_store.multipart_dir)

    login_limiter = LoginRateLimiter(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
    )
    admin_sessions = AdminSessionRegistry(settings.session_max_age_seconds)

    def is_admin(request: Request) -> bool:
        session_id = request.session.get("admin_session_id")
        return isinstance(session_id, str) and admin_sessions.is_active(session_id)

    app = FastAPI(title="tempserver", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage_dir = file_store.storage_dir
    app.state.file_store = file_store
    app.state.admin_sessions = admin_sessions
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tempserver_session",
        max_age=settings.session_max_age_seconds,
        same_site="strict",
        https_only=settings.session_cookie_secure,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        upload_max_bytes=settings.max_upload_request_bytes,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        if settings.session_cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000",
            )
        return response

    def storage_error_response(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "title": "存储暂时不可用",
                "message": "文件存储目录当前不可用，请稍后重试。",
            },
            status_code=503,
            headers={"Retry-After": "30"},
        )

    @app.get("/healthz", name="health")
    async def health():
        available = await run_in_threadpool(file_store.is_available)
        return JSONResponse(
            {"status": "ok" if available else "storage_unavailable"},
            status_code=200 if available else 503,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", name="home")
    async def home(request: Request):
        try:
            files = await run_in_threadpool(
                file_store.list_files,
                settings.max_public_files,
            )
        except StorageUnavailableError:
            return storage_error_response(request)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"files": files},
        )

    @app.get("/files/{filename}", name="download_file")
    async def download_file(filename: str):
        try:
            path = await run_in_threadpool(file_store.resolve_download, filename)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="File not found") from exc
        except StorageUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Storage unavailable") from exc
        if path is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            path=path,
            filename=filename,
            media_type="application/octet-stream",
        )

    @app.get("/admin/login", name="admin_login")
    async def admin_login(request: Request):
        if is_admin(request):
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
        old_session_id = request.session.get("admin_session_id")
        if isinstance(old_session_id, str):
            admin_sessions.revoke(old_session_id)
        request.session.clear()
        request.session["admin_session_id"] = admin_sessions.issue()
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)

    @app.get("/admin", name="admin_dashboard")
    async def admin_dashboard(request: Request):
        if not is_admin(request):
            return _login_redirect(request)
        flash = request.session.pop("flash", None)
        try:
            files = await run_in_threadpool(
                file_store.list_files,
                settings.max_public_files,
            )
        except StorageUnavailableError:
            return storage_error_response(request)
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={
                "files": files,
                "csrf_token": _csrf_token(request),
                "flash": flash,
                "max_upload_mb": settings.max_upload_bytes // MEBIBYTE,
                "max_upload_bytes": settings.max_upload_bytes,
                "max_upload_request_mb": settings.max_upload_request_bytes // MEBIBYTE,
                "max_files_per_upload": settings.max_files_per_upload,
                "max_public_files": settings.max_public_files,
            },
        )

    @app.post("/admin/upload", name="upload_files")
    async def upload_files(request: Request):
        # Deliberately authenticate before request.form(). Anonymous callers do
        # not trigger multipart parsing or temporary-file creation.
        if not is_admin(request):
            return _login_redirect(request)

        try:
            async with request.form(
                max_files=settings.max_files_per_upload,
                max_fields=5,
                max_part_size=64 * 1024,
            ) as form:
                submitted_csrf = form.get("csrf_token")
                if not isinstance(submitted_csrf, str) or not _valid_csrf(
                    request,
                    submitted_csrf,
                ):
                    raise HTTPException(status_code=403, detail="Invalid CSRF token")

                uploads = [
                    value
                    for key, value in form.multi_items()
                    if key == "files" and isinstance(value, UploadFile)
                ]
                if not uploads:
                    _set_flash(request, "error", "请选择至少一个文件")
                    return RedirectResponse(
                        request.url_for("admin_dashboard"),
                        status_code=303,
                    )

                processed_count = len(uploads)
                uploaded_count = 0
                failed_count = 0
                first_error: str | None = None
                unique_names: set[str] = set()
                for upload in uploads:
                    try:
                        saved_name = await run_in_threadpool(
                            file_store.save_upload,
                            upload,
                            settings.max_upload_bytes,
                            settings.max_public_files,
                        )
                        unique_names.add(saved_name.casefold())
                        uploaded_count += 1
                    except (OSError, RuntimeError, ValueError) as exc:
                        failed_count += 1
                        if first_error is None:
                            first_error = (
                                f"{_short_filename(upload.filename)}："
                                f"{_friendly_storage_error(exc)}"
                            )
        except StarletteHTTPException as exc:
            if exc.status_code != 400:
                raise
            _set_flash(
                request,
                "error",
                f"上传请求格式无效；单次最多 {settings.max_files_per_upload} 个文件",
            )
            return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)

        if failed_count:
            message = (
                f"已处理 {processed_count} 个上传项：成功 {uploaded_count} 个，"
                f"失败 {failed_count} 个"
            )
            if first_error:
                message += f"。首个错误：{first_error}"
            _set_flash(request, "error", message)
        else:
            _set_flash(
                request,
                "success",
                f"已处理 {processed_count} 个上传项，最终涉及 {len(unique_names)} 个文件",
            )
        return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)

    @app.post("/admin/files/{filename}/delete", name="delete_file")
    async def delete_file(
        request: Request,
        filename: str,
        csrf_token: Annotated[str, Form()],
    ):
        if not is_admin(request):
            return _login_redirect(request)
        if not _valid_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

        try:
            await run_in_threadpool(file_store.delete, filename)
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
        if is_admin(request) and not _valid_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        session_id = request.session.get("admin_session_id")
        if isinstance(session_id, str):
            admin_sessions.revoke(session_id)
        request.session.clear()
        return RedirectResponse(request.url_for("home"), status_code=303)

    return app
