from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    admin_username: str
    admin_password: str
    storage_dir: Path
    session_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "")
        storage_dir = os.getenv("FILE_STORAGE_DIR", "").strip()
        session_secret = os.getenv("SESSION_SECRET", "")

        missing = [
            name
            for name, value in (
                ("ADMIN_USERNAME", admin_username),
                ("ADMIN_PASSWORD", admin_password),
                ("FILE_STORAGE_DIR", storage_dir),
                ("SESSION_SECRET", session_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        if len(session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters")

        return cls(
            admin_username=admin_username,
            admin_password=admin_password,
            storage_dir=Path(storage_dir).expanduser(),
            session_secret=session_secret,
        )

