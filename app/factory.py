from __future__ import annotations

import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings


BASE_DIR = Path(__file__).resolve().parent
TEMPORARY_UPLOAD_PREFIX = ".tempserver-upload-"
WINDOWS_INVALID_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _validate_filename(filename: str) -> str:
    if filename in {"", ".", ".."}:
        raise ValueError("文件名不能为空")
    if WINDOWS_INVALID_CHARACTERS.search(filename):
        raise ValueError("文件名包含 Windows 不支持的字符")
    if filename.endswith((" ", ".")):
        raise ValueError("文件名不能以空格或句点结尾")
    if filename.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError("文件名是 Windows 保留名称")
    if filename.startswith(TEMPORARY_UPLOAD_PREFIX):
        raise ValueError("文件名使用了系统保留前缀")
    return filename


def _direct_file(storage_dir: Path, filename: str) -> Path:
    safe_name = _validate_filename(filename)
    candidate = storage_dir / safe_name
    if candidate.parent.resolve() != storage_dir.resolve():
        raise ValueError("非法文件路径")
    return candidate


def _list_files(storage_dir: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for entry in storage_dir.iterdir():
        try:
            if (
                entry.name.startswith(TEMPORARY_UPLOAD_PREFIX)
                or entry.is_symlink()
                or not entry.is_file()
            ):
                continue
            size = entry.stat().st_size
        except OSError:
            continue
        files.append(
            {
                "name": entry.name,
                "size": _format_size(size),
            }
        )
    return sorted(files, key=lambda item: item["name"].casefold())


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
        and secrets.compare_digest(stored_token, submitted_token)
    )


def _is_admin(request: Request) -> bool:
    return request.session.get("is_admin") is True


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url_for("admin_login"), status_code=303)


async def _save_upload(storage_dir: Path, upload: UploadFile) -> str:
    temporary_path: Path | None = None

    try:
        filename = _validate_filename(upload.filename or "")
        target = _direct_file(storage_dir, filename)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=TEMPORARY_UPLOAD_PREFIX,
            dir=storage_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            while chunk := await upload.read(1024 * 1024):
                temporary_file.write(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, target)
        temporary_path = None
        return filename
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        await upload.close()


def create_app(settings: Settings) -> FastAPI:
    storage_dir = settings.storage_dir.resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    if not storage_dir.is_dir():
        raise RuntimeError(f"FILE_STORAGE_DIR is not a directory: {storage_dir}")

    app = FastAPI(title="tempserver", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage_dir = storage_dir
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tempserver_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.get("/", name="home")
    async def home(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"files": _list_files(storage_dir)},
        )

    @app.get("/files/{filename}", name="download_file")
    async def download_file(filename: str):
        try:
            path = _direct_file(storage_dir, filename)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="File not found") from exc
        if path.is_symlink() or not path.is_file():
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

        username_matches = secrets.compare_digest(username, settings.admin_username)
        password_matches = secrets.compare_digest(password, settings.admin_password)
        if not (username_matches and password_matches):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": _csrf_token(request),
                    "error": "用户名或密码错误",
                },
                status_code=401,
            )

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
                "files": _list_files(storage_dir),
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

        uploaded: list[str] = []
        errors: list[str] = []
        for upload in files:
            try:
                uploaded.append(await _save_upload(storage_dir, upload))
            except (OSError, ValueError) as exc:
                errors.append(f"{upload.filename or '未命名文件'}：{exc}")

        if errors:
            prefix = f"已上传 {len(uploaded)} 个文件；" if uploaded else ""
            request.session["flash"] = {
                "kind": "error",
                "message": prefix + "上传失败：" + "；".join(errors),
            }
        else:
            request.session["flash"] = {
                "kind": "success",
                "message": f"已上传或覆盖 {len(uploaded)} 个文件",
            }
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
            path = _direct_file(storage_dir, filename)
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError(filename)
            path.unlink()
            request.session["flash"] = {
                "kind": "success",
                "message": f"已删除 {filename}",
            }
        except (OSError, ValueError) as exc:
            request.session["flash"] = {
                "kind": "error",
                "message": f"删除 {filename} 失败：{exc}",
            }
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

