from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


MEBIBYTE = 1024 * 1024
FORBIDDEN_ADMIN_PASSWORDS = {
    "admin123456",
    "change-this-password",
    "changeme12345",
    "password1234",
    "请替换为强密码",
    "请设置至少12位的强密码",
}
FORBIDDEN_SESSION_SECRETS = {
    "replace-with-at-least-32-random-characters",
    "请替换为至少32位的随机字符串",
    "请使用下方命令生成随机值",
}


@dataclass(frozen=True, slots=True)
class Settings:
    admin_username: str
    admin_password: str
    storage_dir: Path
    session_secret: str
    session_cookie_secure: bool = True
    session_max_age_seconds: int = 8 * 60 * 60
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    max_upload_bytes: int = 50 * MEBIBYTE
    max_upload_request_bytes: int = 100 * MEBIBYTE
    max_files_per_upload: int = 20
    max_public_files: int = 500

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("ADMIN_USERNAME", self.admin_username.strip()),
                ("ADMIN_PASSWORD", self.admin_password),
                ("FILE_STORAGE_DIR", str(self.storage_dir).strip()),
                ("SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        password_normalized = self.admin_password.casefold()
        if len(self.admin_password) < 12:
            raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters")
        if password_normalized in {
            password.casefold() for password in FORBIDDEN_ADMIN_PASSWORDS
        }:
            raise RuntimeError("ADMIN_PASSWORD must not use an example placeholder")
        if password_normalized == self.admin_username.casefold():
            raise RuntimeError("ADMIN_PASSWORD must not equal ADMIN_USERNAME")

        if len(self.session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
        if self.session_secret.casefold() in {
            secret.casefold() for secret in FORBIDDEN_SESSION_SECRETS
        }:
            raise RuntimeError("SESSION_SECRET must not use an example placeholder")
        if len(set(self.session_secret)) < 8:
            raise RuntimeError("SESSION_SECRET is too predictable; generate a random value")

        if not 300 <= self.session_max_age_seconds <= 86400:
            raise RuntimeError("SESSION_MAX_AGE_SECONDS must be between 300 and 86400")
        if not 1 <= self.login_max_attempts <= 100:
            raise RuntimeError("LOGIN_MAX_ATTEMPTS must be between 1 and 100")
        if not 10 <= self.login_window_seconds <= 86400:
            raise RuntimeError("LOGIN_WINDOW_SECONDS must be between 10 and 86400")
        if not MEBIBYTE <= self.max_upload_bytes <= 2 * 1024 * MEBIBYTE:
            raise RuntimeError("MAX_UPLOAD_BYTES must be between 1 MiB and 2 GiB")
        if not self.max_upload_bytes <= self.max_upload_request_bytes <= 4 * 1024 * MEBIBYTE:
            raise RuntimeError(
                "MAX_UPLOAD_REQUEST_BYTES must be at least MAX_UPLOAD_BYTES and at most 4 GiB"
            )
        if not 1 <= self.max_files_per_upload <= 100:
            raise RuntimeError("MAX_FILES_PER_UPLOAD must be between 1 and 100")
        if not 1 <= self.max_public_files <= 5000:
            raise RuntimeError("MAX_PUBLIC_FILES must be between 1 and 5000")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        storage_dir = os.getenv("FILE_STORAGE_DIR", "").strip()
        if not storage_dir:
            raise RuntimeError("Missing required environment variables: FILE_STORAGE_DIR")

        return cls(
            admin_username=os.getenv("ADMIN_USERNAME", "").strip(),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            storage_dir=Path(storage_dir),
            session_secret=os.getenv("SESSION_SECRET", ""),
            session_cookie_secure=_parse_bool(
                "SESSION_COOKIE_SECURE",
                os.getenv("SESSION_COOKIE_SECURE", "true"),
            ),
            session_max_age_seconds=_parse_int(
                "SESSION_MAX_AGE_SECONDS",
                os.getenv("SESSION_MAX_AGE_SECONDS", str(8 * 60 * 60)),
            ),
            login_max_attempts=_parse_int(
                "LOGIN_MAX_ATTEMPTS",
                os.getenv("LOGIN_MAX_ATTEMPTS", "5"),
            ),
            login_window_seconds=_parse_int(
                "LOGIN_WINDOW_SECONDS",
                os.getenv("LOGIN_WINDOW_SECONDS", "300"),
            ),
            max_upload_bytes=_parse_int(
                "MAX_UPLOAD_BYTES",
                os.getenv("MAX_UPLOAD_BYTES", str(50 * MEBIBYTE)),
            ),
            max_upload_request_bytes=_parse_int(
                "MAX_UPLOAD_REQUEST_BYTES",
                os.getenv("MAX_UPLOAD_REQUEST_BYTES", str(100 * MEBIBYTE)),
            ),
            max_files_per_upload=_parse_int(
                "MAX_FILES_PER_UPLOAD",
                os.getenv("MAX_FILES_PER_UPLOAD", "20"),
            ),
            max_public_files=_parse_int(
                "MAX_PUBLIC_FILES",
                os.getenv("MAX_PUBLIC_FILES", "500"),
            ),
        )


def _parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _parse_int(name: str, value: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
