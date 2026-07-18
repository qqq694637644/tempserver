from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from starlette.datastructures import UploadFile


DATA_DIRECTORY_NAME = ".tempserver-data"
MULTIPART_DIRECTORY_NAME = "multipart"
MANIFEST_NAME = ".tempserver-manifest.json"
MANIFEST_BACKUP_NAME = ".tempserver-manifest.backup.json"
MANIFEST_TEMP_PREFIX = ".tempserver-manifest-"
INSTANCE_LOCK_NAME = ".tempserver.lock"
LEGACY_UPLOAD_TEMP_PREFIX = ".tempserver-upload-"
UPLOAD_TEMP_PREFIX = ".upload-"
BLOB_SUFFIX = ".blob"
MANIFEST_VERSION = 2
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


class StorageUnavailableError(RuntimeError):
    """The configured storage directory is unavailable at runtime."""


class UploadTooLargeError(ValueError):
    """An uploaded file exceeds the configured per-file limit."""


class PublicFileLimitError(ValueError):
    """Adding a new public name would exceed the configured site limit."""


@dataclass(frozen=True, slots=True)
class ResolvedFile:
    path: Path
    name: str
    size: int


class _InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = open(path, "a+b")
        try:
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise RuntimeError(
                "FILE_STORAGE_DIR is already in use by another tempserver instance"
            ) from exc

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
            self._handle = None


class FileStore:
    """Single-process, versioned file storage with durable manifest backups."""

    def __init__(self, storage_dir: Path, protected_paths: tuple[Path, ...] = ()) -> None:
        self.storage_dir = storage_dir.expanduser().resolve()
        self._validate_storage_location(protected_paths)

        if self.storage_dir.exists() and not self.storage_dir.is_dir():
            raise RuntimeError(
                f"FILE_STORAGE_DIR is not a directory: {self.storage_dir}"
            )
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Unable to create FILE_STORAGE_DIR: {self.storage_dir}"
            ) from exc

        self._reject_sensitive_files_at_root()
        self.data_dir = self.storage_dir / DATA_DIRECTORY_NAME
        data_dir_preexisted = self.data_dir.exists()
        self.multipart_dir = self.data_dir / MULTIPART_DIRECTORY_NAME
        try:
            self.data_dir.mkdir(exist_ok=True)
            self.multipart_dir.mkdir(exist_ok=True)
        except OSError as exc:
            raise RuntimeError("Unable to create internal storage directories") from exc
        if not self.data_dir.is_dir() or not self.multipart_dir.is_dir():
            raise RuntimeError("Internal storage path is not a directory")

        self.manifest_path = self.storage_dir / MANIFEST_NAME
        self.manifest_backup_path = self.storage_dir / MANIFEST_BACKUP_NAME
        self.instance_lock_path = self.storage_dir / INSTANCE_LOCK_NAME
        self._lock = threading.RLock()
        self._instance_lock: _InstanceLock | None = None
        self._closed = False

        try:
            self._instance_lock = _InstanceLock(self.instance_lock_path)
            with self._lock:
                self._manifest = self._load_or_initialize_manifest_locked(
                    data_dir_preexisted
                )
                self._cleanup_transient_files_locked()
        except Exception:
            self.close()
            raise

    @staticmethod
    def validate_filename(filename: str) -> str:
        filename = unicodedata.normalize("NFC", filename)
        if filename in {"", ".", ".."}:
            raise ValueError("文件名不能为空")
        if WINDOWS_INVALID_CHARACTERS.search(filename):
            raise ValueError("文件名包含 Windows 不支持的字符")
        if filename.endswith((" ", ".")):
            raise ValueError("文件名不能以空格或句点结尾")
        if filename.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ValueError("文件名是 Windows 保留名称")
        if FileStore._is_sensitive_filename(filename):
            raise ValueError("该文件名属于敏感配置文件，禁止公开")
        if filename.startswith(
            (LEGACY_UPLOAD_TEMP_PREFIX, MANIFEST_TEMP_PREFIX)
        ) or filename in {
            DATA_DIRECTORY_NAME,
            MANIFEST_NAME,
            MANIFEST_BACKUP_NAME,
            INSTANCE_LOCK_NAME,
        }:
            raise ValueError("文件名使用了系统保留名称")
        return filename

    @staticmethod
    def canonical_name(filename: str) -> str:
        return unicodedata.normalize("NFC", filename).casefold()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._instance_lock is not None:
                self._instance_lock.close()
                self._instance_lock = None

    def list_files(self, limit: int) -> list[dict[str, str]]:
        with self._lock:
            self._require_available_locked()
            records = sorted(
                self._manifest["files"].values(),
                key=lambda record: self.canonical_name(record["name"]),
            )
            return [
                {"name": record["name"], "size": _format_size(record["size"])}
                for record in records[:limit]
            ]

    def resolve_download(self, filename: str) -> ResolvedFile | None:
        safe_name = self.validate_filename(filename)
        key = self.canonical_name(safe_name)
        with self._lock:
            self._require_available_locked()
            record = self._manifest["files"].get(key)
            if record is None:
                return None
            path = self._blob_path(record["blob"])
            try:
                if path.is_symlink() or not path.is_file():
                    raise StorageUnavailableError(
                        f"Referenced blob is missing: {record['blob']}"
                    )
                stat_result = path.stat()
            except OSError as exc:
                raise StorageUnavailableError("文件存储目录当前不可用") from exc
            return ResolvedFile(
                path=path,
                name=record["name"],
                size=stat_result.st_size,
            )

    def save_upload(
        self,
        upload: UploadFile,
        max_upload_bytes: int,
        max_public_files: int,
    ) -> str:
        temporary_path: Path | None = None
        blob_path: Path | None = None

        try:
            filename = self.validate_filename(upload.filename or "")
            key = self.canonical_name(filename)
            if upload.size is not None and upload.size > max_upload_bytes:
                raise UploadTooLargeError("文件超过单文件大小限制")

            with self._lock:
                self._require_available_locked()
                if key not in self._manifest["files"] and len(
                    self._manifest["files"]
                ) >= max_public_files:
                    raise PublicFileLimitError("站点文件数量已达到上限")

            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=UPLOAD_TEMP_PREFIX,
                dir=self.data_dir,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                upload.file.seek(0)
                total = 0
                while chunk := upload.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_upload_bytes:
                        raise UploadTooLargeError("文件超过单文件大小限制")
                    temporary_file.write(chunk)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            with self._lock:
                self._require_available_locked()
                if key not in self._manifest["files"] and len(
                    self._manifest["files"]
                ) >= max_public_files:
                    raise PublicFileLimitError("站点文件数量已达到上限")
                blob_name = f"{uuid.uuid4().hex}{BLOB_SUFFIX}"
                blob_path = self._blob_path(blob_name)
                os.replace(temporary_path, blob_path)
                temporary_path = None
                size = blob_path.stat().st_size
                updated = self._copy_manifest_locked()
                updated["generation"] += 1
                updated["files"][key] = {
                    "name": filename,
                    "blob": blob_name,
                    "size": size,
                }
                self._persist_manifest_locked(updated)
                self._manifest = updated

            blob_path = None
            return filename
        finally:
            if temporary_path is not None:
                _unlink_with_retries(temporary_path)
            if blob_path is not None:
                _unlink_with_retries(blob_path)

    def delete(self, filename: str) -> None:
        safe_name = self.validate_filename(filename)
        key = self.canonical_name(safe_name)
        with self._lock:
            self._require_available_locked()
            record = self._manifest["files"].get(key)
            if record is None:
                raise FileNotFoundError(safe_name)

            updated = self._copy_manifest_locked()
            updated["generation"] += 1
            updated["files"].pop(key)
            root_copy = self.storage_dir / record["name"]
            if root_copy.exists() and record["name"] not in updated["legacy_cleanup"]:
                updated["legacy_cleanup"].append(record["name"])
            self._persist_manifest_locked(updated)
            self._manifest = updated

    def collect_garbage(self, grace_seconds: int) -> dict[str, int]:
        cutoff = time.time() - grace_seconds
        deleted_blobs = 0
        deleted_legacy = 0
        with self._lock:
            self._require_available_locked()
            referenced = {
                record["blob"] for record in self._manifest["files"].values()
            }
            for entry in self.data_dir.iterdir():
                if entry.name == MULTIPART_DIRECTORY_NAME:
                    continue
                if not entry.name.endswith(BLOB_SUFFIX) or entry.name in referenced:
                    continue
                try:
                    if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime <= cutoff:
                        if _unlink_with_retries(entry):
                            deleted_blobs += 1
                except OSError:
                    continue

            remaining_cleanup: list[str] = []
            for name in self._manifest["legacy_cleanup"]:
                path = self.storage_dir / name
                if not path.exists():
                    continue
                if _unlink_with_retries(path):
                    deleted_legacy += 1
                else:
                    remaining_cleanup.append(name)

            if remaining_cleanup != self._manifest["legacy_cleanup"]:
                updated = self._copy_manifest_locked()
                updated["generation"] += 1
                updated["legacy_cleanup"] = remaining_cleanup
                self._persist_manifest_locked(updated)
                self._manifest = updated

            self._repair_manifest_copies_locked()

        return {"blobs": deleted_blobs, "legacy": deleted_legacy}

    def is_available(self) -> bool:
        try:
            with self._lock:
                self._require_available_locked()
            return True
        except StorageUnavailableError:
            return False

    def manifest_summary(self) -> dict[str, int]:
        with self._lock:
            self._require_available_locked()
            return {
                "version": self._manifest["version"],
                "generation": self._manifest["generation"],
                "files": len(self._manifest["files"]),
                "legacy_cleanup": len(self._manifest["legacy_cleanup"]),
            }

    def _validate_storage_location(self, protected_paths: tuple[Path, ...]) -> None:
        for protected_path in protected_paths:
            protected = protected_path.resolve()
            if protected == self.storage_dir or protected.is_relative_to(
                self.storage_dir
            ):
                raise RuntimeError(
                    "FILE_STORAGE_DIR must be a dedicated directory and must not "
                    "contain the application directory"
                )

    def _reject_sensitive_files_at_root(self) -> None:
        try:
            for entry in self.storage_dir.iterdir():
                if self._is_sensitive_filename(entry.name):
                    raise RuntimeError(
                        f"Sensitive configuration file found in FILE_STORAGE_DIR: {entry.name}"
                    )
        except OSError as exc:
            raise RuntimeError("Unable to inspect FILE_STORAGE_DIR") from exc

    def _require_available_locked(self) -> None:
        if self._closed:
            raise StorageUnavailableError("文件存储已关闭")
        try:
            available = (
                self.storage_dir.is_dir()
                and self.data_dir.is_dir()
                and self.multipart_dir.is_dir()
                and (
                    self.manifest_path.is_file()
                    or self.manifest_backup_path.is_file()
                )
            )
        except OSError as exc:
            raise StorageUnavailableError("文件存储目录当前不可用") from exc
        if not available:
            raise StorageUnavailableError("文件存储目录当前不可用")

    def _load_or_initialize_manifest_locked(
        self, data_dir_preexisted: bool
    ) -> dict[str, Any]:
        candidates: list[tuple[dict[str, Any], bool]] = []
        invalid_paths: list[str] = []
        for path in (self.manifest_path, self.manifest_backup_path):
            if not path.exists():
                continue
            try:
                manifest, migrated = self._read_manifest_candidate(path)
                candidates.append((manifest, migrated))
            except RuntimeError:
                invalid_paths.append(path.name)

        if candidates:
            manifest, migrated = max(
                candidates,
                key=lambda item: item[0]["generation"],
            )
            if migrated:
                manifest["generation"] += 1
            self._persist_manifest_locked(manifest)
            return manifest

        if invalid_paths:
            raise RuntimeError(
                "Storage manifest and backup are unreadable; restore a valid manifest "
                "before starting tempserver"
            )

        if data_dir_preexisted:
            raise RuntimeError(
                "Storage manifest is missing from an initialized FILE_STORAGE_DIR; "
                "restore .tempserver-manifest.json or its backup"
            )

        return self._initialize_from_legacy_locked()

    def _initialize_from_legacy_locked(self) -> dict[str, Any]:
        existing_blobs = [
            entry
            for entry in self.data_dir.iterdir()
            if entry.name.endswith(BLOB_SUFFIX)
        ]
        if existing_blobs:
            raise RuntimeError(
                "Storage manifest is missing while internal blobs exist; refusing "
                "to guess file mappings"
            )

        manifest: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "generation": 1,
            "files": {},
            "legacy_cleanup": [],
        }
        created_blobs: list[Path] = []
        try:
            for entry in self.storage_dir.iterdir():
                if self._is_internal_entry(entry.name):
                    continue
                try:
                    display_name = self.validate_filename(entry.name)
                    if entry.is_symlink() or not entry.is_file():
                        continue
                except (OSError, ValueError):
                    continue

                key = self.canonical_name(display_name)
                if key in manifest["files"]:
                    raise RuntimeError(
                        "Case-insensitive duplicate legacy file names must be resolved "
                        "before startup"
                    )
                blob_name = f"{uuid.uuid4().hex}{BLOB_SUFFIX}"
                blob_path = self._blob_path(blob_name)
                temporary_path = self.data_dir / f"{UPLOAD_TEMP_PREFIX}{uuid.uuid4().hex}"
                with entry.open("rb") as source, temporary_path.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary_path, blob_path)
                created_blobs.append(blob_path)
                manifest["files"][key] = {
                    "name": display_name,
                    "blob": blob_name,
                    "size": blob_path.stat().st_size,
                }
                manifest["legacy_cleanup"].append(entry.name)

            self._persist_manifest_locked(manifest)
            return manifest
        except Exception:
            for path in created_blobs:
                _unlink_with_retries(path)
            raise

    def _read_manifest_candidate(self, path: Path) -> tuple[dict[str, Any], bool]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Storage manifest is unreadable or invalid") from exc

        version = raw.get("version") if isinstance(raw, dict) else None
        if version == 1:
            return self._migrate_v1_manifest(raw), True
        if version != MANIFEST_VERSION:
            raise RuntimeError("Unsupported storage manifest format")
        return self._validate_v2_manifest(raw), False

    def _migrate_v1_manifest(self, raw: dict[str, Any]) -> dict[str, Any]:
        files = raw.get("files")
        deleted = raw.get("deleted")
        if not isinstance(files, dict) or not isinstance(deleted, list):
            raise RuntimeError("Invalid version 1 storage manifest")

        migrated: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "generation": max(1, int(raw.get("generation", 1))),
            "files": {},
            "legacy_cleanup": [],
        }
        for name, record in files.items():
            display_name = self.validate_filename(name)
            if not isinstance(record, dict):
                raise RuntimeError("Invalid file record in storage manifest")
            blob = record.get("blob")
            size = record.get("size")
            if not isinstance(blob, str) or not isinstance(size, int) or size < 0:
                raise RuntimeError("Invalid file metadata in storage manifest")
            self._validate_blob_reference(blob)
            key = self.canonical_name(display_name)
            if key in migrated["files"]:
                raise RuntimeError(
                    "Case-insensitive duplicate names exist in the old manifest"
                )
            migrated["files"][key] = {
                "name": display_name,
                "blob": blob,
                "size": size,
            }
            if (self.storage_dir / name).exists():
                migrated["legacy_cleanup"].append(name)

        for name in deleted:
            if not isinstance(name, str):
                raise RuntimeError("Invalid deleted record in storage manifest")
            display_name = self.validate_filename(name)
            if (self.storage_dir / display_name).exists():
                migrated["legacy_cleanup"].append(display_name)

        return migrated

    def _validate_v2_manifest(self, raw: dict[str, Any]) -> dict[str, Any]:
        generation = raw.get("generation")
        files = raw.get("files")
        legacy_cleanup = raw.get("legacy_cleanup", [])
        if (
            not isinstance(generation, int)
            or generation < 1
            or not isinstance(files, dict)
            or not isinstance(legacy_cleanup, list)
        ):
            raise RuntimeError("Invalid storage manifest structure")

        normalized_files: dict[str, dict[str, Any]] = {}
        for key, record in files.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise RuntimeError("Invalid file record in storage manifest")
            display_name = self.validate_filename(record.get("name", ""))
            blob = record.get("blob")
            size = record.get("size")
            if (
                self.canonical_name(display_name) != key
                or not isinstance(blob, str)
                or not isinstance(size, int)
                or size < 0
            ):
                raise RuntimeError("Invalid file metadata in storage manifest")
            self._validate_blob_reference(blob)
            normalized_files[key] = {
                "name": display_name,
                "blob": blob,
                "size": size,
            }

        normalized_cleanup: list[str] = []
        for name in legacy_cleanup:
            if not isinstance(name, str):
                raise RuntimeError("Invalid legacy cleanup record")
            display_name = self.validate_filename(name)
            if display_name not in normalized_cleanup:
                normalized_cleanup.append(display_name)

        return {
            "version": MANIFEST_VERSION,
            "generation": generation,
            "files": normalized_files,
            "legacy_cleanup": normalized_cleanup,
        }

    def _validate_blob_reference(self, blob_name: str) -> None:
        path = self._blob_path(blob_name)
        try:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Referenced blob is missing: {blob_name}")
        except OSError as exc:
            raise RuntimeError(f"Referenced blob is unavailable: {blob_name}") from exc

    def _copy_manifest_locked(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "generation": self._manifest["generation"],
            "files": {
                key: dict(record) for key, record in self._manifest["files"].items()
            },
            "legacy_cleanup": list(self._manifest["legacy_cleanup"]),
        }

    def _persist_manifest_locked(self, manifest: dict[str, Any]) -> None:
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        # The backup is the commit record. If replacing the primary later fails,
        # restart recovery still selects the newer generation from the backup.
        self._write_atomic(self.manifest_backup_path, payload)
        try:
            self._write_atomic(self.manifest_path, payload)
        except OSError:
            pass

    def _repair_manifest_copies_locked(self) -> None:
        payload = json.dumps(
            self._manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._write_atomic(self.manifest_backup_path, payload)
        try:
            self._write_atomic(self.manifest_path, payload)
        except OSError:
            pass

    def _write_atomic(self, target: Path, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=MANIFEST_TEMP_PREFIX,
                dir=self.storage_dir,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            _replace_with_retries(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                _unlink_with_retries(temporary_path)

    def _cleanup_transient_files_locked(self) -> None:
        cutoff = time.time() - STALE_FILE_MAX_AGE_SECONDS
        for entry in self.storage_dir.iterdir():
            if entry.name.startswith(
                (LEGACY_UPLOAD_TEMP_PREFIX, MANIFEST_TEMP_PREFIX)
            ):
                _unlink_if_stale(entry, cutoff)
        for entry in self.data_dir.iterdir():
            if entry.name == MULTIPART_DIRECTORY_NAME:
                continue
            if entry.name.startswith(UPLOAD_TEMP_PREFIX):
                _unlink_if_stale(entry, cutoff)
        for entry in self.multipart_dir.iterdir():
            _unlink_if_stale(entry, cutoff)

    def _blob_path(self, blob_name: str) -> Path:
        if (
            not isinstance(blob_name, str)
            or not blob_name.endswith(BLOB_SUFFIX)
            or Path(blob_name).name != blob_name
            or "/" in blob_name
            or "\\" in blob_name
        ):
            raise RuntimeError("Invalid blob name in storage manifest")
        return self.data_dir / blob_name

    @staticmethod
    def _is_sensitive_filename(filename: str) -> bool:
        normalized = filename.casefold()
        return normalized == ".env" or normalized.startswith(".env.")

    @staticmethod
    def _is_internal_entry(filename: str) -> bool:
        return (
            filename
            in {
                DATA_DIRECTORY_NAME,
                MANIFEST_NAME,
                MANIFEST_BACKUP_NAME,
                INSTANCE_LOCK_NAME,
            }
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


def _unlink_with_retries(path: Path, attempts: int = 3) -> bool:
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if attempt == attempts - 1:
                return False
            time.sleep(0.05 * (attempt + 1))
        except OSError:
            return False
    return False


def _unlink_if_stale(path: Path, cutoff: float) -> None:
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime <= cutoff:
            _unlink_with_retries(path)
    except OSError:
        return
