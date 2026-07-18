from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile


DATA_DIRECTORY_NAME = ".tempserver-data"
MANIFEST_NAME = ".tempserver-manifest.json"
MANIFEST_TEMP_PREFIX = ".tempserver-manifest-"
LEGACY_UPLOAD_TEMP_PREFIX = ".tempserver-upload-"
UPLOAD_TEMP_PREFIX = ".upload-"
BLOB_SUFFIX = ".blob"
STALE_FILE_MAX_AGE_SECONDS = 24 * 60 * 60
WINDOWS_INVALID_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class FileStore:
    """Versioned file storage with stable public names.

    Public names are mapped to immutable internal blobs. Replacing or deleting a
    file only updates the manifest, so an in-progress Windows download can keep
    its existing file handle while new requests immediately see the new state.
    """

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir.expanduser().resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if not self.storage_dir.is_dir():
            raise RuntimeError(
                f"FILE_STORAGE_DIR is not a directory: {self.storage_dir}"
            )

        self.data_dir = self.storage_dir / DATA_DIRECTORY_NAME
        self.data_dir.mkdir(exist_ok=True)
        if not self.data_dir.is_dir():
            raise RuntimeError(f"Internal data path is not a directory: {self.data_dir}")

        self.manifest_path = self.storage_dir / MANIFEST_NAME
        self._lock = threading.RLock()
        with self._lock:
            self._manifest = self._load_manifest_locked()
            self._cleanup_stale_files_locked()

    @staticmethod
    def validate_filename(filename: str) -> str:
        if filename in {"", ".", ".."}:
            raise ValueError("文件名不能为空")
        if WINDOWS_INVALID_CHARACTERS.search(filename):
            raise ValueError("文件名包含 Windows 不支持的字符")
        if filename.endswith((" ", ".")):
            raise ValueError("文件名不能以空格或句点结尾")
        if filename.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ValueError("文件名是 Windows 保留名称")
        if filename.startswith(
            (LEGACY_UPLOAD_TEMP_PREFIX, MANIFEST_TEMP_PREFIX)
        ) or filename == DATA_DIRECTORY_NAME or filename == MANIFEST_NAME:
            raise ValueError("文件名使用了系统保留名称")
        return filename

    def list_files(self) -> list[dict[str, str]]:
        with self._lock:
            files: dict[str, int] = {}
            mapped = self._manifest["files"]
            deleted = set(self._manifest["deleted"])

            for name, record in mapped.items():
                path = self._blob_path(record["blob"])
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    files[name] = path.stat().st_size
                except OSError:
                    continue

            for entry in self.storage_dir.iterdir():
                if self._is_internal_entry(entry.name):
                    continue
                if entry.name in mapped or entry.name in deleted:
                    continue
                try:
                    self.validate_filename(entry.name)
                    if entry.is_symlink() or not entry.is_file():
                        continue
                    files[entry.name] = entry.stat().st_size
                except (OSError, ValueError):
                    continue

            return [
                {"name": name, "size": _format_size(size)}
                for name, size in sorted(files.items(), key=lambda item: item[0].casefold())
            ]

    def resolve_download(self, filename: str) -> Path | None:
        safe_name = self.validate_filename(filename)
        with self._lock:
            if safe_name in self._manifest["deleted"]:
                return None

            record = self._manifest["files"].get(safe_name)
            if record is not None:
                path = self._blob_path(record["blob"])
            else:
                path = self.storage_dir / safe_name

            try:
                if path.is_symlink() or not path.is_file():
                    return None
            except OSError:
                return None
            return path

    async def save_upload(self, upload: UploadFile) -> str:
        temporary_path: Path | None = None
        blob_path: Path | None = None

        try:
            filename = self.validate_filename(upload.filename or "")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=UPLOAD_TEMP_PREFIX,
                dir=self.data_dir,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                while chunk := await upload.read(1024 * 1024):
                    temporary_file.write(chunk)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            blob_name = f"{uuid.uuid4().hex}{BLOB_SUFFIX}"
            blob_path = self._blob_path(blob_name)
            os.replace(temporary_path, blob_path)
            temporary_path = None
            size = blob_path.stat().st_size

            with self._lock:
                updated = self._copy_manifest_locked()
                updated["files"][filename] = {"blob": blob_name, "size": size}
                updated["deleted"] = [
                    name for name in updated["deleted"] if name != filename
                ]
                self._save_manifest_locked(updated)
                self._manifest = updated

            blob_path = None
            return filename
        finally:
            if temporary_path is not None:
                _unlink_with_retries(temporary_path)
            if blob_path is not None:
                _unlink_with_retries(blob_path)
            await upload.close()

    def delete(self, filename: str) -> None:
        safe_name = self.validate_filename(filename)
        with self._lock:
            mapped = safe_name in self._manifest["files"]
            legacy_path = self.storage_dir / safe_name
            legacy_exists = False
            try:
                legacy_exists = legacy_path.is_file() and not legacy_path.is_symlink()
            except OSError:
                pass

            if not mapped and not legacy_exists:
                raise FileNotFoundError(safe_name)

            updated = self._copy_manifest_locked()
            updated["files"].pop(safe_name, None)
            if safe_name not in updated["deleted"]:
                updated["deleted"].append(safe_name)
            self._save_manifest_locked(updated)
            self._manifest = updated

    def _blob_path(self, blob_name: str) -> Path:
        if (
            not blob_name.endswith(BLOB_SUFFIX)
            or Path(blob_name).name != blob_name
            or "/" in blob_name
            or "\\" in blob_name
        ):
            raise RuntimeError("Invalid blob name in storage manifest")
        return self.data_dir / blob_name

    def _load_manifest_locked(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"version": 1, "files": {}, "deleted": []}

        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Storage manifest is unreadable or invalid") from exc

        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RuntimeError("Unsupported storage manifest format")
        files = raw.get("files")
        deleted = raw.get("deleted")
        if not isinstance(files, dict) or not isinstance(deleted, list):
            raise RuntimeError("Invalid storage manifest structure")

        normalized_files: dict[str, dict[str, Any]] = {}
        for name, record in files.items():
            if not isinstance(name, str) or not isinstance(record, dict):
                raise RuntimeError("Invalid file record in storage manifest")
            self.validate_filename(name)
            blob = record.get("blob")
            size = record.get("size")
            if not isinstance(blob, str) or not isinstance(size, int) or size < 0:
                raise RuntimeError("Invalid file metadata in storage manifest")
            self._blob_path(blob)
            normalized_files[name] = {"blob": blob, "size": size}

        normalized_deleted: list[str] = []
        for name in deleted:
            if not isinstance(name, str):
                raise RuntimeError("Invalid deleted record in storage manifest")
            self.validate_filename(name)
            if name not in normalized_deleted:
                normalized_deleted.append(name)

        return {
            "version": 1,
            "files": normalized_files,
            "deleted": normalized_deleted,
        }

    def _copy_manifest_locked(self) -> dict[str, Any]:
        return {
            "version": 1,
            "files": {
                name: dict(record) for name, record in self._manifest["files"].items()
            },
            "deleted": list(self._manifest["deleted"]),
        }

    def _save_manifest_locked(self, manifest: dict[str, Any]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=MANIFEST_TEMP_PREFIX,
                dir=self.storage_dir,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    manifest,
                    temporary_file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            _replace_with_retries(temporary_path, self.manifest_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                _unlink_with_retries(temporary_path)

    def _cleanup_stale_files_locked(self) -> None:
        cutoff = time.time() - STALE_FILE_MAX_AGE_SECONDS
        referenced_blobs = {
            record["blob"] for record in self._manifest["files"].values()
        }
        shadowed_names = set(self._manifest["files"]) | set(
            self._manifest["deleted"]
        )

        for entry in self.storage_dir.iterdir():
            if entry.name.startswith(
                (LEGACY_UPLOAD_TEMP_PREFIX, MANIFEST_TEMP_PREFIX)
            ):
                _unlink_if_stale(entry, cutoff)
            elif entry.name in shadowed_names:
                _unlink_if_stale(entry, cutoff)

        for entry in self.data_dir.iterdir():
            if entry.name.startswith(UPLOAD_TEMP_PREFIX):
                _unlink_if_stale(entry, cutoff)
            elif entry.name.endswith(BLOB_SUFFIX) and entry.name not in referenced_blobs:
                _unlink_if_stale(entry, cutoff)

    @staticmethod
    def _is_internal_entry(filename: str) -> bool:
        return (
            filename in {DATA_DIRECTORY_NAME, MANIFEST_NAME}
            or filename.startswith(LEGACY_UPLOAD_TEMP_PREFIX)
            or filename.startswith(MANIFEST_TEMP_PREFIX)
        )


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


def _replace_with_retries(source: Path, target: Path, attempts: int = 5) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def _unlink_with_retries(path: Path, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == attempts - 1:
                return
            time.sleep(0.05 * (attempt + 1))
        except OSError:
            return


def _unlink_if_stale(path: Path, cutoff: float) -> None:
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime <= cutoff:
            _unlink_with_retries(path)
    except OSError:
        return
