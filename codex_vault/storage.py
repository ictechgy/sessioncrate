"""Safe, local storage primitives for Codex session archives.

The module intentionally has no dependency on Codex's databases or indexes.
It only knows about rollout JSONL files, including Zstandard-compressed copies,
below ``sessions`` and ``archived_sessions``.  Archives retain the
source bytes exactly and add only small JSON manifests.
"""

from __future__ import annotations

import errno
import fcntl
import gzip
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .activity import probe_session_file
from . import zstd_io


class VaultError(RuntimeError):
    """A user-actionable storage or safety failure."""


class SessionActivityBlocked(VaultError):
    """One selected source is in use or cannot be checked; preserve it."""

    def __init__(self, state: str, reason: str):
        self.state = state
        super().__init__(reason)


class _SourceRemovalSkipped(VaultError):
    """A pre-quarantine source check failed without changing the source."""


_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_DAY_NS = 86_400 * 1_000_000_000
_HOUR_NS = 3_600 * 1_000_000_000
_MAX_META_LINE = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_JSON_INTEGER_DIGITS = 20
_MAX_ZSTD_RECORD_BYTES = _MAX_META_LINE
_MIN_ZSTD_DECODED_BYTES = 64 * 1024 * 1024
_MAX_ZSTD_DECODED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_ZSTD_EXPANSION_RATIO = 256
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_ID = re.compile(r"^[0-9a-f]{1,64}$")
_ROLLOUT = re.compile(r"^rollout-.*\.jsonl(?:\.zst)?$")
_REQUIRED_MANIFEST = {
    "archive_id",
    "blob_sha256",
    "blob",
    "source_root",
    "relative_path",
    "session_id",
    "cwd",
    "tool_version",
    "started_at",
    "raw_bytes",
    "compressed_bytes",
    "source_mtime_ns",
    "source_mode",
    "archived_at",
}


def _absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _check_path_components(path: Path) -> None:
    """Reject symlink components without resolving the path."""

    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        st = _lstat(current)
        if st is not None and stat.S_ISLNK(st.st_mode):
            # macOS exposes the temporary directory through /var (and, on
            # some releases, /tmp).  These fixed system aliases are outside
            # the caller-controlled archive boundary; symlinks below them
            # remain rejected.
            if current in {Path("/var"), Path("/tmp")} and os.path.realpath(current).startswith("/private/"):
                continue
            raise VaultError(f"symlink path component is not allowed: {current}")


def _mkdir_private(path: Path, *, tighten_existing: bool = True) -> Path:
    """Create a directory tree while checking each existing component."""

    path = _absolute(path)
    _check_path_components(path)
    missing: list[Path] = []
    current = path
    while _lstat(current) is None:
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(missing):
        try:
            os.mkdir(item, _PRIVATE_DIR)
        except FileExistsError:
            pass
        st = _lstat(item)
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise VaultError(f"unsafe archive directory: {item}")
        os.chmod(item, _PRIVATE_DIR)
    st = _lstat(path)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VaultError(f"unsafe archive directory: {path}")
    if tighten_existing or path in missing:
        os.chmod(path, _PRIVATE_DIR)
    return path


def _require_directory(path: Path, *, label: str) -> Path:
    path = _absolute(path)
    _check_path_components(path)
    st = _lstat(path)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VaultError(f"{label} must be a real directory: {path}")
    return path


def _safe_codex_home(codex_home: Path | str) -> Path:
    return _require_directory(Path(codex_home), label="Codex home")


def _safe_vault(vault_dir: Path | str, *, create: bool) -> Path | None:
    path = _absolute(vault_dir)
    if create:
        # The caller may deliberately place a vault inside an existing
        # directory.  Keep that directory's permissions; newly created vault
        # roots and all vault-owned children are still tightened to 0700.
        return _mkdir_private(path, tighten_existing=False)
    _check_path_components(path)
    st = _lstat(path)
    if st is None:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VaultError(f"vault must be a real directory: {path}")
    return path


def _safe_vault_subdir(vault: Path, name: str, *, create: bool) -> Path | None:
    if name not in {"blobs", "manifests"}:
        raise VaultError("invalid vault subdirectory")
    path = vault / name
    if create:
        return _mkdir_private(path)
    _check_path_components(path)
    st = _lstat(path)
    if st is None:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VaultError(f"vault subdirectory is unsafe: {path}")
    return path


def _temporary_file_stats(directory: Path | None, prefix: str) -> tuple[int, int, int]:
    """Measure unfinished archive files without following links."""

    if directory is None:
        return 0, 0, 0
    count = 0
    logical_bytes = 0
    allocated_bytes = 0
    try:
        entries = os.scandir(directory)
    except OSError:
        return 0, 0, 0
    with entries:
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            try:
                item = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(item.st_mode):
                continue
            count += 1
            logical_bytes += int(item.st_size)
            allocated_bytes += int(getattr(item, "st_blocks", 0)) * 512
    return count, logical_bytes, allocated_bytes


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise VaultError(f"cannot open directory for durability: {path}: {exc}") from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _vault_lock(vault_dir: Path | str, *, exclusive: bool, create: bool) -> Iterator[Path]:
    vault = _safe_vault(vault_dir, create=create)
    if vault is None:
        raise VaultError(f"vault does not exist: {_absolute(vault_dir)}")
    lock_path = vault / ".lock"
    _check_path_components(lock_path)
    lock_stat = _lstat(lock_path)
    if lock_stat is not None and (
        stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode)
    ):
        raise VaultError("vault lock must be a regular non-symlink file")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        if lock_stat is None:
            try:
                fd = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    _PRIVATE_FILE,
                )
                created = True
            except FileExistsError:
                lock_stat = _lstat(lock_path)
                if lock_stat is None:
                    raise VaultError("vault lock changed while being opened")
                fd = os.open(lock_path, os.O_RDWR | nofollow)
        else:
            fd = os.open(lock_path, os.O_RDWR | nofollow)
    except OSError as exc:
        raise VaultError(f"cannot open vault lock: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if created:
            lock_stat = opened
        expected_identity = (int(opened.st_dev), int(opened.st_ino))
        if (
            lock_stat is None
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(opened.st_mode)
            or (int(lock_stat.st_dev), int(lock_stat.st_ino)) != expected_identity
            or not _matches_regular_identity(_lstat(lock_path), expected_identity)
        ):
            raise VaultError("vault lock changed while being opened")
        # Only chmod an inode created by this process. An existing pathname may
        # have been supplied by another account-local actor, so changing its
        # mode would risk modifying an unrelated hard-linked file.
        if created:
            if opened.st_nlink != 1:
                raise VaultError("new vault lock has an unexpected hard-link count")
            os.fchmod(fd, _PRIVATE_FILE)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {
                errno.EACCES,
                errno.EAGAIN,
            }:
                raise VaultError("vault is locked by another operation; retry later") from exc
            raise VaultError(f"cannot lock vault: {exc}") from exc
        if not _matches_regular_identity(_lstat(lock_path), expected_identity):
            raise VaultError("vault lock changed before operation")
        yield vault
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _open_nofollow(path: Path, flags: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow)
    except OSError as exc:
        raise VaultError(f"cannot open safe file {path}: {exc}") from exc
    st = os.fstat(fd)
    if stat.S_ISLNK(st.st_mode):
        os.close(fd)
        raise VaultError(f"symlink file is not allowed: {path}")
    return fd


def _safe_regular(path: Path, *, label: str) -> os.stat_result:
    _check_path_components(path)
    st = _lstat(path)
    if st is None:
        raise VaultError(f"{label} does not exist: {path}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise VaultError(f"{label} must be a regular non-symlink file: {path}")
    return st


def _bounded_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds supported range")
    return int(value)


def _read_json(path: Path, *, max_bytes: int, expected: os.stat_result) -> Any:
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        expected_identity = _stat_identity(expected)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _stat_identity(opened) != expected_identity
        ):
            raise VaultError("manifest changed while being opened")
        stream = os.fdopen(fd, "rb")
        fd = -1
        with stream:
            encoded = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(encoded) > max_bytes:
            raise VaultError("manifest exceeds maximum size")
        current = _lstat(path)
        if (
            _stat_identity(after) != expected_identity
            or current is None
            or not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or _stat_identity(current) != expected_identity
        ):
            raise VaultError("manifest changed while being read")
        return json.loads(encoded.decode("utf-8"), parse_int=_bounded_json_int)
    except VaultError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise VaultError("manifest is invalid JSON") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    parent = path.parent
    _safe_vault_subdir(parent.parent, parent.name, create=True)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd, temp_name = tempfile.mkstemp(prefix=".vault-tmp-", dir=parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, _PRIVATE_FILE)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_temp_no_replace(temp, path)
        _fsync_directory(parent)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _replace_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Durably replace one vault-owned manifest after validating its pathname."""

    parent = path.parent
    _safe_vault_subdir(parent.parent, parent.name, create=False)
    _safe_regular(path, label="existing manifest")
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd, temp_name = tempfile.mkstemp(prefix=".vault-tmp-", dir=parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, _PRIVATE_FILE)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(parent)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _matches_regular_identity(st: os.stat_result | None, expected: tuple[int, int]) -> bool:
    return (
        st is not None
        and not stat.S_ISLNK(st.st_mode)
        and stat.S_ISREG(st.st_mode)
        and (int(st.st_dev), int(st.st_ino)) == expected
    )


def _publish_temp_no_replace(temp: Path, target: Path, *, mode: int | None = _PRIVATE_FILE) -> bool:
    """Link a temp file into place without ever replacing another file."""

    _check_path_components(target.parent)
    temp_fd = _open_nofollow(temp, os.O_RDONLY)
    try:
        opened = os.fstat(temp_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise VaultError(f"temporary archive file is not regular: {temp}")
        if mode is not None:
            os.fchmod(temp_fd, mode)
        expected = os.fstat(temp_fd)
        expected_identity = (int(expected.st_dev), int(expected.st_ino))
        temp_now = _lstat(temp)
        if not _matches_regular_identity(temp_now, expected_identity):
            raise VaultError(f"unsafe temporary archive race at: {temp}")

        existing = _lstat(target)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise VaultError(f"unsafe existing archive file: {target}")
            temp.unlink(missing_ok=True)
            return False
        try:
            os.link(temp, target, follow_symlinks=False)
        except FileExistsError:
            existing = _lstat(target)
            if existing is None or stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise VaultError(f"unsafe archive publication race at: {target}")
            temp_now = _lstat(temp)
            if not _matches_regular_identity(temp_now, expected_identity):
                raise VaultError(f"unsafe temporary archive race at: {temp}")
            temp.unlink(missing_ok=True)
            return False
        except OSError as exc:
            raise VaultError(f"cannot publish archive file: {target}: {exc}") from exc

        temp_now = _lstat(temp)
        target_now = _lstat(target)
        if not _matches_regular_identity(temp_now, expected_identity) or not _matches_regular_identity(
            target_now, expected_identity
        ):
            raise VaultError(f"unsafe archive publication race at: {target}")
        temp.unlink()
        target_now = _lstat(target)
        if not _matches_regular_identity(target_now, expected_identity):
            raise VaultError(f"unsafe archive publication race at: {target}")
        return True
    finally:
        os.close(temp_fd)


def _validate_days(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VaultError("older_than_days must be a non-negative integer")
    return value


def _candidate(relative_path: str, root: Path, st: os.stat_result, *, eligible: bool, reason: str) -> dict[str, Any]:
    size = int(st.st_size) if stat.S_ISREG(st.st_mode) else 0
    return {
        "relative_path": relative_path,
        "source_root": str(root),
        "size": size,
        "bytes": size,
        "source_bytes": size,
        "mtime_ns": int(st.st_mtime_ns),
        "source_mtime_ns": int(st.st_mtime_ns),
        "source_mode": int(stat.S_IMODE(st.st_mode)),
        "eligible": bool(eligible),
        "reason": reason,
    }


def scan(codex_home: Path, older_than_days: int = 30) -> dict[str, Any]:
    """Return metadata for recognized rollout files without opening content."""

    root = _safe_codex_home(codex_home)
    days = _validate_days(older_than_days)
    cutoff = time.time_ns() - days * _DAY_NS
    candidates: list[dict[str, Any]] = []
    for top_name in ("sessions", "archived_sessions"):
        top = root / top_name
        top_stat = _lstat(top)
        if top_stat is None:
            continue
        if stat.S_ISLNK(top_stat.st_mode) or not stat.S_ISDIR(top_stat.st_mode):
            continue
        stack = [top]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                entry_path = Path(entry.path)
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(entry_stat.st_mode):
                    if _ROLLOUT.match(entry.name):
                        rel = entry_path.relative_to(root).as_posix()
                        candidates.append(_candidate(rel, root, entry_stat, eligible=False, reason="symlink"))
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    stack.append(entry_path)
                    continue
                if not stat.S_ISREG(entry_stat.st_mode) or not _ROLLOUT.match(entry.name):
                    continue
                rel = entry_path.relative_to(root).as_posix()
                if entry_stat.st_mtime_ns > cutoff:
                    reason = "too_new"
                    eligible = False
                else:
                    reason = "eligible"
                    eligible = True
                candidates.append(_candidate(rel, root, entry_stat, eligible=eligible, reason=reason))
    candidates.sort(key=lambda item: item["relative_path"])
    return {
        "codex_home": str(root),
        "older_than_days": days,
        "candidates": candidates,
        "source_bytes": sum(item["source_bytes"] for item in candidates if item["eligible"]),
    }


def _valid_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise VaultError("manifest relative_path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VaultError("manifest relative_path escapes source root")
    if path.as_posix() != value:
        raise VaultError("manifest relative_path is not canonical")
    if not path.parts or path.parts[0] not in {"sessions", "archived_sessions"}:
        raise VaultError("manifest relative_path is outside recognized session roots")
    if not _ROLLOUT.match(path.name):
        raise VaultError("manifest relative_path is not a rollout JSONL file")
    return path.as_posix()


def _is_zstd_relative(relative_path: str) -> bool:
    return relative_path.endswith(".jsonl.zst")


def _zstd_decoded_limit(raw_bytes: int) -> int:
    return min(
        _MAX_ZSTD_DECODED_BYTES,
        max(_MIN_ZSTD_DECODED_BYTES, raw_bytes * _MAX_ZSTD_EXPANSION_RATIO),
    )


def _valid_manifest(
    manifest: Any,
    *,
    expected_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or not _REQUIRED_MANIFEST.issubset(manifest):
        raise VaultError("manifest is missing required fields")
    archive_id = manifest.get("archive_id")
    blob_sha = manifest.get("blob_sha256")
    if not isinstance(archive_id, str) or not _HEX64.fullmatch(archive_id):
        raise VaultError("manifest archive_id is invalid")
    if expected_id is not None and archive_id != expected_id:
        raise VaultError("manifest filename does not match archive_id")
    if not isinstance(blob_sha, str) or not _HEX64.fullmatch(blob_sha):
        raise VaultError("manifest blob_sha256 is invalid")
    blob = manifest.get("blob")
    if blob != f"blobs/{blob_sha}.jsonl.gz":
        raise VaultError("manifest blob path is invalid")
    source_root = manifest.get("source_root")
    if not isinstance(source_root, str) or not source_root or not Path(source_root).is_absolute():
        raise VaultError("manifest source_root must be absolute")
    if any(part in {".", ".."} for part in Path(source_root).parts):
        raise VaultError("manifest source_root is unsafe")
    relative = _valid_relative_path(manifest.get("relative_path"))
    for key in ("session_id", "cwd", "tool_version", "started_at", "archived_at"):
        if not isinstance(manifest.get(key), str):
            raise VaultError(f"manifest {key} is invalid")
    for key in ("raw_bytes", "compressed_bytes", "source_mtime_ns", "source_mode"):
        value = manifest.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VaultError(f"manifest {key} is invalid")
    if manifest["source_mode"] > 0o1777:
        raise VaultError("manifest source_mode is invalid")
    if _is_zstd_relative(relative):
        decoded_bytes = manifest.get("decoded_bytes")
        if (
            isinstance(decoded_bytes, bool)
            or not isinstance(decoded_bytes, int)
            or decoded_bytes <= 0
            or decoded_bytes > _zstd_decoded_limit(manifest["raw_bytes"])
        ):
            raise VaultError("manifest decoded_bytes is invalid for Zstandard content")
    if archive_id != _archive_id(source_root, relative, blob_sha):
        raise VaultError("manifest archive_id does not match source identity")
    normalized = dict(manifest)
    normalized["relative_path"] = relative
    return normalized


def _manifest_path(vault: Path, archive_id: str) -> Path:
    if not _HEX64.fullmatch(archive_id):
        raise VaultError("archive id is invalid")
    manifests = _safe_vault_subdir(vault, "manifests", create=False)
    if manifests is None:
        raise VaultError("vault has no manifests")
    path = manifests / f"{archive_id}.json"
    _check_path_components(path)
    return path


def _read_manifest_path(path: Path, *, expected_id: str | None = None) -> dict[str, Any]:
    st = _safe_regular(path, label="manifest")
    if st.st_size > _MAX_MANIFEST_BYTES:
        raise VaultError("manifest exceeds maximum size")
    raw = _read_json(path, max_bytes=_MAX_MANIFEST_BYTES, expected=st)
    return _valid_manifest(raw, expected_id=expected_id)


def _iter_manifest_paths(vault: Path) -> Iterator[Path]:
    manifests = _safe_vault_subdir(vault, "manifests", create=False)
    if manifests is None:
        return
    try:
        entries = sorted(os.scandir(manifests), key=lambda item: item.name)
    except OSError as exc:
        raise VaultError(f"cannot enumerate manifests: {exc}") from exc
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        archive_id = entry.name[:-5]
        if not _HEX64.fullmatch(archive_id):
            continue
        yield Path(entry.path)


def _iter_valid_manifests(vault: Path) -> Iterator[dict[str, Any]]:
    for path in _iter_manifest_paths(vault):
        archive_id = path.name[:-5]
        # A valid-looking manifest filename with malformed contents is a
        # vault integrity failure.  Unknown filenames and symlinks are still
        # ignored by _iter_manifest_paths, but recognized records fail closed
        # for list/search/stats callers.
        yield _read_manifest_path(path, expected_id=archive_id)


def list_archives(vault_dir: Path) -> list[dict[str, Any]]:
    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        return []
    with _vault_lock(vault, exclusive=False, create=False):
        result = list(_iter_valid_manifests(vault))
    result.sort(key=lambda item: (item["archived_at"], item["archive_id"]))
    return result


def resolve_archive(vault_dir: Path, archive_id: str) -> dict[str, Any]:
    if not isinstance(archive_id, str) or not _ARCHIVE_ID.fullmatch(archive_id):
        raise VaultError("archive id must be a hexadecimal id or prefix")
    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with _vault_lock(vault, exclusive=False, create=False):
        return _resolve_archive_unlocked(vault, archive_id)


def _resolve_archive_unlocked(vault: Path, archive_id: str) -> dict[str, Any]:
    if not isinstance(archive_id, str) or not _ARCHIVE_ID.fullmatch(archive_id):
        raise VaultError("archive id must be a hexadecimal id or prefix")
    exact_path = None
    if len(archive_id) == 64:
        exact_path = _manifest_path(vault, archive_id)
        if _lstat(exact_path) is not None:
            return _read_manifest_path(exact_path, expected_id=archive_id)
    matching_paths = [
        path
        for path in _iter_manifest_paths(vault)
        if path.name[:-5].startswith(archive_id)
    ]
    matches = [
        _read_manifest_path(path, expected_id=path.name[:-5])
        for path in matching_paths
    ]
    if not matches:
        raise VaultError(f"archive not found: {archive_id}")
    if len(matches) > 1:
        raise VaultError(f"archive id prefix is ambiguous: {archive_id}")
    return matches[0]


def _blob_path(vault: Path, manifest: dict[str, Any]) -> Path:
    manifest = _valid_manifest(manifest)
    blobs = _safe_vault_subdir(vault, "blobs", create=False)
    if blobs is None:
        raise VaultError("vault has no blobs")
    path = blobs / f"{manifest['blob_sha256']}.jsonl.gz"
    _check_path_components(path)
    _safe_regular(path, label="archive blob")
    return path


def verified_blob_path(vault_dir: Path, manifest: dict[str, Any]) -> Path:
    """Validate a manifest and return its safe compressed blob path."""

    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    return _blob_path(vault, manifest)


class _DigestingReader:
    """Account for bytes read without taking ownership of the wrapped stream."""

    def __init__(self, stream: Any):
        self._stream = stream
        self._digest = hashlib.sha256()
        self.total = 0

    def _account(self, data: bytes) -> bytes:
        self._digest.update(data)
        self.total += len(data)
        return data

    def read(self, size: int = -1) -> bytes:
        return self._account(self._stream.read(size))

    def readline(self, size: int = -1) -> bytes:
        return self._account(self._stream.readline(size))

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _require_unchanged_open_blob(
    path: Path,
    fd: int,
    expected: tuple[int, int, int, int, int, int],
) -> None:
    opened = os.fstat(fd)
    current = _lstat(path)
    if (
        not stat.S_ISREG(opened.st_mode)
        or _stat_identity(opened) != expected
        or current is None
        or not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or _stat_identity(current) != expected
    ):
        raise VaultError(f"archive blob changed while being read: {path}")


def iter_archive_lines(
    vault_dir: Path,
    manifest: dict[str, Any],
    max_record_bytes: int | None = None,
) -> Iterator[bytes | tuple[int, bytes | None]]:
    """Yield decompressed JSONL lines while keeping the blob streaming.

    ``max_record_bytes`` is used by the search reader to bound memory for a
    hostile or accidentally enormous physical line.  In that mode each item
    is ``(line_number, bytes_or_none)`` and ``None`` marks an oversized line.
    The default remains a simple byte-line iterator for callers that want the
    raw stream.
    """

    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    checked = _valid_manifest(manifest)
    if _is_zstd_relative(checked["relative_path"]) and not zstd_io.is_available():
        raise VaultError("Zstandard content requires Python 3.14+")
    blob = _blob_path(vault, checked)

    if max_record_bytes is not None and (
        isinstance(max_record_bytes, bool)
        or not isinstance(max_record_bytes, int)
        or max_record_bytes <= 0
    ):
        raise VaultError("max_record_bytes must be positive")

    def lines_from(
        stream: Any,
        *,
        expected_bytes: int | None = None,
        decoded_limit: int | None = None,
    ) -> Iterator[bytes | tuple[int, bytes | None]]:
        total = 0
        line_number = 0

        def account(data: bytes) -> None:
            nonlocal total
            total += len(data)
            if expected_bytes is not None and total > expected_bytes:
                raise VaultError("Zstandard decoded size mismatch")
            if decoded_limit is not None and total > decoded_limit:
                raise VaultError("Zstandard content exceeds the safe decoded size limit")

        zstd_raw_record_limit = (
            _MAX_ZSTD_RECORD_BYTES
            if expected_bytes is not None and max_record_bytes is None
            else None
        )
        while True:
            if max_record_bytes is None:
                if zstd_raw_record_limit is None:
                    line = stream.readline()
                else:
                    line = stream.readline(zstd_raw_record_limit + 1)
            else:
                line = stream.readline(max_record_bytes + 1)
            if not line:
                break
            account(line)
            line_number += 1
            if max_record_bytes is None:
                if zstd_raw_record_limit is not None and len(line) > zstd_raw_record_limit:
                    raise VaultError(
                        "Zstandard JSONL record exceeds the safe raw iterator line limit"
                    )
                yield line
                continue
            if len(line) <= max_record_bytes:
                yield line_number, line
                continue
            while not line.endswith(b"\n"):
                line = stream.readline(min(64 * 1024, max_record_bytes + 1))
                if not line:
                    break
                account(line)
            yield line_number, None

        if expected_bytes is not None and total != expected_bytes:
            raise VaultError("Zstandard decoded size mismatch")

    expected_path = _safe_regular(blob, label="archive blob")
    fd = _open_nofollow(blob, os.O_RDONLY)
    fd_open = True
    try:
        opened = os.fstat(fd)
        expected_identity = _stat_identity(expected_path)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != expected_identity:
            raise VaultError(f"archive blob changed before it could be read: {blob}")
        raw = os.fdopen(fd, "rb")
        fd_open = False
        with raw:
            _verify_gzip(
                blob,
                checked["blob_sha256"],
                checked["raw_bytes"],
                fileobj=raw,
            )
            _require_unchanged_open_blob(blob, raw.fileno(), expected_identity)
            raw.seek(0)
            with gzip.GzipFile(fileobj=raw, mode="rb") as original:
                source = _DigestingReader(original)
                if not _is_zstd_relative(checked["relative_path"]):
                    yield from lines_from(source)
                else:
                    try:
                        with zstd_io.open_reader(source) as decoded:
                            yield from lines_from(
                                decoded,
                                expected_bytes=checked["decoded_bytes"],
                                decoded_limit=_zstd_decoded_limit(checked["raw_bytes"]),
                            )
                    except zstd_io.ZstdUnavailableError as exc:
                        raise VaultError(str(exc)) from exc
                    except zstd_io.decode_errors() as exc:
                        raise VaultError(
                            "Zstandard session is corrupt, truncated, or has trailing data"
                        ) from exc
                if (
                    source.total != checked["raw_bytes"]
                    or source.hexdigest() != checked["blob_sha256"]
                ):
                    raise VaultError(f"archive blob digest or size changed while being read: {blob}")
            _require_unchanged_open_blob(blob, raw.fileno(), expected_identity)
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise VaultError(f"cannot read archive blob: {blob}: {exc}") from exc
    finally:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass


def _hash_stream(stream: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _hash_path(path: Path) -> tuple[str, int, os.stat_result]:
    expected = _safe_regular(path, label="source")
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        expected_identity = _stat_identity(expected)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != expected_identity:
            raise VaultError("file changed before it could be read")
        stream = os.fdopen(fd, "rb")
        fd = -1
        with stream:
            digest, size = _hash_stream(stream)
            after = os.fstat(stream.fileno())
            current = _lstat(path)
            if (
                _stat_identity(after) != expected_identity
                or current is None
                or not stat.S_ISREG(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _stat_identity(current) != expected_identity
            ):
                raise VaultError("file changed while being read")
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    return digest, size, after


def _parse_session_meta(first: bytes, path: Path) -> dict[str, Any]:
    if not first or len(first) > _MAX_META_LINE:
        raise VaultError(f"session does not start with a bounded session_meta record: {path}")
    try:
        record = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise VaultError(f"first session record is invalid JSON: {path}") from exc
    if not isinstance(record, dict):
        raise VaultError(f"first session record must be an object: {path}")
    kind = record.get("type") or record.get("kind") or record.get("event")
    if kind != "session_meta":
        raise VaultError(f"first session record is not session_meta: {path}")
    return record


def _session_meta(path: Path) -> tuple[dict[str, Any], bytes]:
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "rb") as stream:
            first = stream.readline(_MAX_META_LINE + 1)
    except OSError as exc:
        raise VaultError(f"cannot read session metadata: {path}: {exc}") from exc
    record = _parse_session_meta(first, path)
    return record, first


def _field(record: dict[str, Any], *names: str) -> str:
    sources: list[dict[str, Any]] = [record]
    for nested_name in ("payload", "meta", "session_meta"):
        nested = record.get(nested_name)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for name in names:
            value = source.get(name)
            if isinstance(value, (str, int, float)):
                return str(value)
    return ""


def _metadata_fields(record: dict[str, Any]) -> dict[str, str]:
    return {
        "session_id": _field(record, "session_id", "id", "conversation_id"),
        "cwd": _field(record, "cwd", "working_directory"),
        "tool_version": _field(record, "tool_version", "cli_version", "version"),
        "started_at": _field(record, "started_at", "timestamp", "created_at"),
    }


def _verify_gzip(
    path: Path,
    expected_sha: str,
    expected_size: int,
    *,
    fileobj: Any | None = None,
) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        if fileobj is None:
            stream_context = gzip.open(path, "rb")
        else:
            fileobj.seek(0)
            stream_context = gzip.GzipFile(fileobj=fileobj, mode="rb")
        with stream_context as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise VaultError(f"archive blob is corrupt or truncated: {path}: {exc}") from exc
    if total != expected_size or digest.hexdigest() != expected_sha:
        raise VaultError(f"archive blob digest or size mismatch: {path}")


def _metadata_from_stream(
    stream: Any,
    path: Path,
    *,
    decoded_limit: int | None = None,
) -> tuple[dict[str, str], int]:
    first_limit = _MAX_META_LINE + 1
    if decoded_limit is not None:
        first_limit = min(first_limit, decoded_limit + 1)
    first = stream.readline(first_limit)
    total = len(first)
    if decoded_limit is not None and total > decoded_limit:
        raise VaultError(f"Zstandard content exceeds the safe decoded size limit: {path}")
    record = _parse_session_meta(first, path)

    while True:
        read_size = 1024 * 1024
        if decoded_limit is not None:
            read_size = min(read_size, decoded_limit - total + 1)
        chunk = stream.read(read_size)
        if not chunk:
            break
        total += len(chunk)
        if decoded_limit is not None and total > decoded_limit:
            raise VaultError(f"Zstandard content exceeds the safe decoded size limit: {path}")
    return _metadata_fields(record), total


def _metadata_from_archived_temp(
    temp: Path,
    source_path: Path,
    raw_bytes: int,
) -> tuple[dict[str, str], int]:
    try:
        with gzip.open(temp, "rb") as original:
            try:
                with zstd_io.open_reader(original) as decoded:
                    metadata, decoded_bytes = _metadata_from_stream(
                        decoded,
                        source_path,
                        decoded_limit=_zstd_decoded_limit(raw_bytes),
                    )
                return metadata, decoded_bytes
            except zstd_io.ZstdUnavailableError as exc:
                raise VaultError(str(exc)) from exc
            except zstd_io.decode_errors() as exc:
                raise VaultError(
                    f"Zstandard session is corrupt, truncated, or has trailing data: {source_path}"
                ) from exc
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise VaultError(f"cannot read archived source copy: {source_path}") from exc


def _stream_archive(
    path: Path,
    blobs: Path,
    expected: os.stat_result,
) -> tuple[Path, str, int, dict[str, str], int | None]:
    fd = _open_nofollow(path, os.O_RDONLY)
    fd_open = True
    temp: Path | None = None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise VaultError("source changed before archiving")
        temp_fd, temp_name = tempfile.mkstemp(prefix=".vault-blob-", dir=blobs)
        temp = Path(temp_name)
        os.fchmod(temp_fd, _PRIVATE_FILE)
        is_zstd = _is_zstd_relative(path.name)
        metadata: dict[str, str] = {}
        source_stream = os.fdopen(fd, "rb")
        fd_open = False
        with source_stream as source, os.fdopen(temp_fd, "wb") as raw:
            digest = hashlib.sha256()
            total = 0
            compression_level = 0 if is_zstd else 9
            with gzip.GzipFile(
                fileobj=raw,
                mode="wb",
                compresslevel=compression_level,
                mtime=0,
            ) as compressed:
                if not is_zstd:
                    first = source.readline(_MAX_META_LINE + 1)
                    record = _parse_session_meta(first, path)
                    metadata = _metadata_fields(record)
                    compressed.write(first)
                    digest.update(first)
                    total += len(first)
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    compressed.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        after = _safe_regular(path, label="source")
        if (
            after.st_size != expected.st_size
            or after.st_mtime_ns != expected.st_mtime_ns
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(expected.st_mode)
            or after.st_dev != expected.st_dev
            or after.st_ino != expected.st_ino
        ):
            raise VaultError(f"source changed while archiving: {path}")
        second_sha, second_size, second_stat = _hash_path(path)
        if second_sha != digest.hexdigest() or second_size != total:
            raise VaultError(f"source bytes changed while archiving: {path}")
        if (
            second_stat.st_mtime_ns != expected.st_mtime_ns
            or stat.S_IMODE(second_stat.st_mode) != stat.S_IMODE(expected.st_mode)
        ):
            raise VaultError(f"source metadata changed while archiving: {path}")
        _verify_gzip(temp, digest.hexdigest(), total)
        decoded_bytes: int | None = None
        if is_zstd:
            metadata, decoded_bytes = _metadata_from_archived_temp(temp, path, total)
        return temp, digest.hexdigest(), total, metadata, decoded_bytes
    except Exception:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp is not None:
            temp.unlink(missing_ok=True)
        raise


def _archive_id(source_root: str, relative_path: str, blob_sha: str) -> str:
    material = f"codex-session-vault-v1\0{source_root}\0{relative_path}\0{blob_sha}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _source_path_for_manifest(manifest: dict[str, Any]) -> Path:
    manifest = _valid_manifest(manifest)
    root = _require_directory(Path(manifest["source_root"]), label="manifest source root")
    relative = PurePosixPath(manifest["relative_path"])
    path = root.joinpath(*relative.parts)
    _check_path_components(path)
    return path


def _new_manifest(
    *,
    source_root: Path,
    relative_path: str,
    blob_sha: str,
    raw_bytes: int,
    compressed_bytes: int,
    source_stat: os.stat_result,
    metadata: dict[str, str],
    decoded_bytes: int | None = None,
) -> dict[str, Any]:
    root_string = str(source_root)
    manifest = {
        "archive_id": _archive_id(root_string, relative_path, blob_sha),
        "blob_sha256": blob_sha,
        "blob": f"blobs/{blob_sha}.jsonl.gz",
        "source_root": root_string,
        "relative_path": relative_path,
        "session_id": metadata.get("session_id", ""),
        "cwd": metadata.get("cwd", ""),
        "tool_version": metadata.get("tool_version", ""),
        "started_at": metadata.get("started_at", ""),
        "raw_bytes": int(raw_bytes),
        "compressed_bytes": int(compressed_bytes),
        "source_mtime_ns": int(source_stat.st_mtime_ns),
        "source_mode": int(stat.S_IMODE(source_stat.st_mode)),
        "archived_at": datetime.now(timezone.utc).isoformat(),
    }
    if decoded_bytes is not None:
        manifest["decoded_bytes"] = int(decoded_bytes)
    return manifest


def _existing_manifest(vault: Path, archive_id: str) -> dict[str, Any] | None:
    path = _manifest_path(vault, archive_id)
    if _lstat(path) is None:
        return None
    manifest = _read_manifest_path(path, expected_id=archive_id)
    return manifest


def _publish_blob(vault: Path, temp: Path, sha: str, raw_bytes: int) -> Path:
    blobs = _safe_vault_subdir(vault, "blobs", create=True)
    assert blobs is not None
    target = blobs / f"{sha}.jsonl.gz"
    _check_path_components(target)
    existing = _lstat(target)
    if existing is not None:
        _safe_regular(target, label="existing archive blob")
        _verify_gzip(target, sha, raw_bytes)
        temp.unlink(missing_ok=True)
    else:
        _publish_temp_no_replace(temp, target)
        _verify_gzip(target, sha, raw_bytes)
    _fsync_directory(blobs)
    return target


def _source_candidate_path(root: Path, candidate: dict[str, Any]) -> Path:
    relative = _valid_relative_path(candidate.get("relative_path"))
    if candidate.get("source_root") != str(root):
        raise VaultError("scan candidate source root changed")
    path = root.joinpath(*PurePosixPath(relative).parts)
    _check_path_components(path)
    return path


def _archive_candidate(
    vault: Path,
    blobs: Path,
    root: Path,
    item: dict[str, Any],
    *,
    remove_originals: bool,
) -> dict[str, Any]:
    """Archive one independently recoverable legacy rollout."""

    temp: Path | None = None
    try:
        source = _source_candidate_path(root, item)
        if _is_zstd_relative(item["relative_path"]) and not zstd_io.is_available():
            raise VaultError("Zstandard content requires Python 3.14+")
        source_stat = _safe_regular(source, label="source")
        if source_stat.st_size != item["size"] or source_stat.st_mtime_ns != item["mtime_ns"]:
            raise VaultError(f"source changed after scan: {source}")
        source_mode = stat.S_IMODE(source_stat.st_mode)
        if source_mode > 0o1777:
            raise VaultError(f"source mode is unsupported: {oct(source_mode)}")
        if remove_originals and time.time_ns() - source_stat.st_mtime_ns < _HOUR_NS:
            raise VaultError(f"source must be at least one hour old before removal: {source}")
        temp, sha, raw_bytes, metadata, decoded_bytes = _stream_archive(source, blobs, source_stat)
        manifest = _new_manifest(
            source_root=root,
            relative_path=item["relative_path"],
            blob_sha=sha,
            raw_bytes=raw_bytes,
            compressed_bytes=0,
            source_stat=source_stat,
            metadata=metadata,
            decoded_bytes=decoded_bytes,
        )
        manifest = _valid_manifest(manifest)
        existing_manifest = _existing_manifest(vault, manifest["archive_id"])
        if existing_manifest is not None:
            if (
                existing_manifest["blob_sha256"] != sha
                or existing_manifest["raw_bytes"] != raw_bytes
                or existing_manifest.get("decoded_bytes") != decoded_bytes
            ):
                raise VaultError("archive id already exists with different content")
            _verify_gzip(_blob_path(vault, existing_manifest), sha, raw_bytes)
            manifest["compressed_bytes"] = existing_manifest["compressed_bytes"]
            if any(
                manifest[key] != existing_manifest[key]
                for key in (
                    "source_mtime_ns",
                    "source_mode",
                    "session_id",
                    "cwd",
                    "tool_version",
                    "started_at",
                )
            ):
                manifest_path = _manifest_path(vault, manifest["archive_id"])
                _replace_json_atomic(manifest_path, manifest)
                return _read_manifest_path(manifest_path, expected_id=manifest["archive_id"])
            return existing_manifest

        blob_path = _publish_blob(vault, temp, sha, raw_bytes)
        manifest["compressed_bytes"] = int(blob_path.stat().st_size)
        manifest_path = _manifest_path(vault, manifest["archive_id"])
        _write_json_atomic(manifest_path, manifest)
        published_manifest = _read_manifest_path(manifest_path, expected_id=manifest["archive_id"])
        if (
            published_manifest["blob_sha256"] != manifest["blob_sha256"]
            or published_manifest["raw_bytes"] != manifest["raw_bytes"]
            or published_manifest["source_root"] != manifest["source_root"]
            or published_manifest["relative_path"] != manifest["relative_path"]
            or published_manifest.get("decoded_bytes") != manifest.get("decoded_bytes")
        ):
            raise VaultError("manifest publication race produced different metadata")
        _fsync_directory(manifest_path.parent)
        return published_manifest
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def archive(
    codex_home: Path,
    vault_dir: Path,
    older_than_days: int = 30,
    execute: bool = False,
    remove_originals: bool = False,
) -> dict[str, Any]:
    """Preview or durably archive eligible session files."""

    root = _safe_codex_home(codex_home)
    scan_result = scan(root, older_than_days=older_than_days)
    candidates = scan_result["candidates"]
    eligible = [item for item in candidates if item["eligible"]]
    result: dict[str, Any] = {
        "execute": bool(execute),
        "executed": bool(execute),
        "codex_home": str(root),
        "vault_dir": str(_absolute(vault_dir)),
        "candidates": candidates,
        "archived": [],
        "removed": [],
        "skipped": [],
        "removed_source_bytes": 0,
        "errors": [],
    }
    if not execute:
        result["remove_originals_requested"] = bool(remove_originals)
        return result
    if not eligible:
        return result
    with _vault_lock(vault_dir, exclusive=True, create=True) as vault:
        blobs = _safe_vault_subdir(vault, "blobs", create=True)
        _safe_vault_subdir(vault, "manifests", create=True)
        assert blobs is not None
        for item in eligible:
            try:
                manifest = _archive_candidate(
                    vault,
                    blobs,
                    root,
                    item,
                    remove_originals=remove_originals,
                )
            except (VaultError, OSError) as exc:
                result["errors"].append({
                    "relative_path": item["relative_path"],
                    "reason": str(exc),
                })
                continue
            result["archived"].append(manifest)
        if remove_originals and result["archived"]:
            for manifest in result["archived"]:
                try:
                    source = _source_path_for_manifest(manifest)
                except (VaultError, OSError) as error:
                    result["skipped"].append({
                        "archive_id": manifest["archive_id"],
                        "relative_path": manifest["relative_path"],
                        "reason": str(error),
                    })
                    continue
                try:
                    _remove_source_atomically(source, manifest)
                except SessionActivityBlocked as error:
                    result["skipped"].append({
                        "archive_id": manifest["archive_id"],
                        "relative_path": manifest["relative_path"],
                        "activity": error.state,
                        "reason": str(error),
                    })
                    continue
                except _SourceRemovalSkipped as error:
                    result["skipped"].append({
                        "archive_id": manifest["archive_id"],
                        "relative_path": manifest["relative_path"],
                        "reason": str(error),
                    })
                    continue
                result["removed"].append(manifest["relative_path"])
                result["removed_source_bytes"] += manifest["raw_bytes"]
    return result


def _verify_one(vault: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        blob = _blob_path(vault, manifest)
        st = _safe_regular(blob, label="archive blob")
        if st.st_size != manifest["compressed_bytes"]:
            raise VaultError("compressed blob size differs from manifest")
        _verify_gzip(blob, manifest["blob_sha256"], manifest["raw_bytes"])
        return {
            "ok": True,
            "archive_id": manifest["archive_id"],
            "blob_sha256": manifest["blob_sha256"],
            "raw_bytes": manifest["raw_bytes"],
            "compressed_bytes": manifest["compressed_bytes"],
        }
    except (VaultError, OSError) as exc:
        return {"ok": False, "archive_id": manifest.get("archive_id", ""), "error": str(exc)}


def verify(vault_dir: Path, archive_id: str | None = None) -> dict[str, Any]:
    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with _vault_lock(vault, exclusive=False, create=False):
        if archive_id is not None:
            manifests = [_resolve_archive_unlocked(vault, archive_id)]
            checks = [_verify_one(vault, manifests[0])]
        else:
            checks = []
            for path in _iter_manifest_paths(vault):
                manifest_id = path.name[:-5]
                try:
                    manifest = _read_manifest_path(path, expected_id=manifest_id)
                except (VaultError, OSError) as exc:
                    checks.append({"ok": False, "archive_id": manifest_id, "error": str(exc)})
                    continue
                checks.append(_verify_one(vault, manifest))
    if archive_id is not None:
        return checks[0]
    return {
        "ok": all(item["ok"] for item in checks),
        "verified": [item for item in checks if item["ok"]],
        "failed": [item for item in checks if not item["ok"]],
        "count": len(checks),
    }


def _destination_root(destination_home: Path, *, create: bool) -> Path:
    path = _absolute(destination_home)
    if create:
        return _mkdir_private(path, tighten_existing=False)
    return _require_directory(path, label="destination home")


def _safe_destination_path(root: Path, relative_path: str, *, create_parents: bool) -> Path:
    relative = PurePosixPath(_valid_relative_path(relative_path))
    path = root.joinpath(*relative.parts)
    if create_parents:
        _mkdir_private(path.parent, tighten_existing=False)
    else:
        _check_path_components(path.parent)
    _check_path_components(path)
    return path


def restore(
    vault_dir: Path,
    archive_id: str,
    destination_home: Path | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Restore an archive without replacing a differing existing file."""

    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with _vault_lock(vault, exclusive=True, create=False):
        manifest = _resolve_archive_unlocked(vault, archive_id)
        check = _verify_one(vault, manifest)
        if not check["ok"]:
            raise VaultError(check["error"])
        requested_root = Path(destination_home) if destination_home is not None else Path(manifest["source_root"])
        if execute:
            root = _destination_root(requested_root, create=True)
        else:
            root = _absolute(requested_root)
            if _lstat(root) is not None:
                root = _destination_root(root, create=False)
            else:
                _check_path_components(root)
        target = _safe_destination_path(root, manifest["relative_path"], create_parents=execute)
        result: dict[str, Any] = {
            "ok": True,
            "execute": bool(execute),
            "executed": bool(execute),
            "archive_id": manifest["archive_id"],
            "destination": str(target),
            "restored": False,
            "skipped": False,
        }
        existing = _lstat(target)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise VaultError(f"destination is an unsafe non-regular file: {target}")
            existing_sha, existing_size, _ = _hash_path(target)
            if existing_size == manifest["raw_bytes"] and existing_sha == manifest["blob_sha256"]:
                result["skipped"] = True
                return result
            raise VaultError(f"destination already contains different bytes: {target}")
        if not execute:
            return result
        blob = _blob_path(vault, manifest)
        temp_fd, temp_name = tempfile.mkstemp(prefix=".vault-restore-", dir=target.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(temp_fd, _PRIVATE_FILE)
            digest = hashlib.sha256()
            total = 0
            try:
                with os.fdopen(temp_fd, "wb") as output, gzip.open(blob, "rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
                raise VaultError(f"archive blob is corrupt during restore: {exc}") from exc
            if total != manifest["raw_bytes"] or digest.hexdigest() != manifest["blob_sha256"]:
                raise VaultError("archive digest changed during restore")
            os.utime(temp, ns=(manifest["source_mtime_ns"], manifest["source_mtime_ns"]))
            _fsync_directory(target.parent)
            published = _publish_temp_no_replace(temp, target, mode=manifest["source_mode"] & 0o1777)
            if not published:
                existing_sha, existing_size, _ = _hash_path(target)
                if existing_size == total and existing_sha == digest.hexdigest():
                    result["skipped"] = True
                    return result
                raise VaultError(f"destination already contains different bytes: {target}")
            _fsync_directory(target.parent)
            result["restored"] = True
            return result
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _source_metadata_matches_manifest(path: Path, manifest: dict[str, Any]) -> tuple[bool, str]:
    try:
        st = _safe_regular(path, label="source")
        if (
            st.st_size != manifest["raw_bytes"]
            or st.st_mtime_ns != manifest["source_mtime_ns"]
            or stat.S_IMODE(st.st_mode) != manifest["source_mode"]
        ):
            return False, "source metadata changed"
        return True, "source metadata matches"
    except (VaultError, OSError) as exc:
        return False, str(exc)


def _source_matches_manifest(path: Path, manifest: dict[str, Any]) -> tuple[bool, str]:
    metadata_matches, reason = _source_metadata_matches_manifest(path, manifest)
    if not metadata_matches:
        return False, reason
    try:
        sha, size, hashed_stat = _hash_path(path)
        if size != manifest["raw_bytes"] or sha != manifest["blob_sha256"]:
            return False, "source bytes changed"
        if (
            hashed_stat.st_mtime_ns != manifest["source_mtime_ns"]
            or stat.S_IMODE(hashed_stat.st_mode) != manifest["source_mode"]
        ):
            return False, "source metadata changed"
        return True, "eligible"
    except (VaultError, OSError) as exc:
        return False, str(exc)


def _stat_identity(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Fields that expose replacement, write, chmod and truncate races."""

    return (
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_size),
        int(st.st_mtime_ns),
        int(st.st_ctime_ns),
        int(stat.S_IMODE(st.st_mode)),
    )


def _hash_fd(fd: int) -> tuple[str, int]:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _restore_quarantined_source_no_replace(
    pending: Path,
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    """Publish a quarantined regular file without replacing a raced destination."""

    if not _matches_regular_identity(_lstat(pending), expected_identity):
        raise VaultError(f"source retained in quarantine for recovery: {pending}")
    try:
        os.link(pending, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise VaultError(f"source retained in quarantine for recovery: {pending}") from exc
    except OSError as exc:
        raise VaultError(f"source retained in quarantine for recovery: {pending}: {exc}") from exc
    if not _matches_regular_identity(_lstat(pending), expected_identity) or not _matches_regular_identity(
        _lstat(path), expected_identity
    ):
        raise VaultError(f"source retained in quarantine for recovery: {pending}")
    try:
        os.unlink(pending)
    except OSError as exc:
        raise VaultError(
            f"source restored but quarantine copy could not be removed: {pending}: {exc}"
        ) from exc
    if not _matches_regular_identity(_lstat(path), expected_identity):
        raise VaultError(f"source changed during quarantine recovery: {path}")
    _fsync_directory(path.parent)


def _remove_source_atomically(path: Path, manifest: dict[str, Any]) -> None:
    ok, reason = _source_matches_manifest(path, manifest)
    if not ok:
        raise _SourceRemovalSkipped(f"refusing to remove {path}: {reason}")
    if time.time_ns() - manifest["source_mtime_ns"] < _HOUR_NS:
        raise _SourceRemovalSkipped(f"source must be at least one hour old before removal: {path}")
    _require_session_inactive(path)
    pending = path.with_name(f".codex-vault-pending-{uuid.uuid4().hex}")
    _check_path_components(path.parent)
    try:
        os.rename(path, pending)
    except OSError as exc:
        raise _SourceRemovalSkipped(f"could not quarantine source before removal: {path}: {exc}") from exc
    pending_removed = False
    try:
        # Query the renamed file as well: an existing writer still refers to
        # its inode. Check before opening our own verification descriptor.
        _require_session_inactive(pending)
        _fsync_directory(path.parent)
        pending_stat = _safe_regular(pending, label="quarantined source")
        pending_fd = _open_nofollow(pending, os.O_RDONLY)
        if (
            pending_stat.st_size != manifest["raw_bytes"]
            or pending_stat.st_mtime_ns != manifest["source_mtime_ns"]
            or stat.S_IMODE(pending_stat.st_mode) != manifest["source_mode"]
        ):
            os.close(pending_fd)
            raise VaultError("quarantined source metadata changed")
        try:
            before_hash_stat = os.fstat(pending_fd)
            pending_sha, pending_size = _hash_fd(pending_fd)
            after_hash_stat = os.fstat(pending_fd)
            if _stat_identity(before_hash_stat) != _stat_identity(after_hash_stat):
                raise VaultError("quarantined source changed while being verified")
            if pending_size != manifest["raw_bytes"] or pending_sha != manifest["blob_sha256"]:
                raise VaultError("quarantined source bytes changed")
            final_stat = _lstat(pending)
            if final_stat is None or _stat_identity(final_stat) != _stat_identity(after_hash_stat):
                raise VaultError("quarantined source changed before removal")
            os.unlink(pending)
            pending_removed = True
        finally:
            os.close(pending_fd)
        _fsync_directory(path.parent)
    except Exception as exc:
        if pending_removed:
            raise VaultError(
                "source was removed but removal durability could not be confirmed; "
                f"verified archive retained for restore: {path}: {exc}"
            ) from exc
        # Never overwrite a path that appeared while the source was quarantined,
        # and never move a changed or symlink replacement back into it.  A
        # quarantine left behind is recoverable by the user and is safer than
        # guessing which bytes should own the original pathname.
        try:
            pending_now = _lstat(pending)
            if pending_now is not None:
                if stat.S_ISLNK(pending_now.st_mode) or not stat.S_ISREG(pending_now.st_mode):
                    raise VaultError(f"source retained in quarantine for recovery: {pending}")
                pending_identity = (int(pending_now.st_dev), int(pending_now.st_ino))
                pending_matches, _ = _source_matches_manifest(pending, manifest)
                if pending_matches and _matches_regular_identity(_lstat(pending), pending_identity):
                    _restore_quarantined_source_no_replace(pending, path, pending_identity)
                else:
                    raise VaultError(f"source retained in quarantine for recovery: {pending}")
        except VaultError:
            raise
        except OSError as rollback_exc:
            raise VaultError(f"source retained in quarantine for recovery: {pending}: {rollback_exc}") from exc
        if isinstance(exc, VaultError):
            raise
        raise VaultError(f"source removal failed; source restored: {path}: {exc}") from exc


def _require_session_inactive(path: Path) -> None:
    result = probe_session_file(path)
    state = result.get("state")
    if state != "inactive":
        raise SessionActivityBlocked(
            state if state in {"active", "unknown"} else "unknown",
            str(result.get("reason") or "could not establish selected session file inactivity"),
        )


def prune(vault_dir: Path, archive_id: str | None = None, execute: bool = False) -> dict[str, Any]:
    """Safely remove unchanged originals while retaining verified archives."""

    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with _vault_lock(vault, exclusive=True, create=False):
        manifests = [_resolve_archive_unlocked(vault, archive_id)] if archive_id is not None else list(_iter_valid_manifests(vault))
        entries: list[dict[str, Any]] = []
        for manifest in manifests:
            try:
                source = _source_path_for_manifest(manifest)
            except (VaultError, OSError) as exc:
                entries.append({"archive_id": manifest["archive_id"], "relative_path": manifest["relative_path"], "eligible": False, "reason": str(exc)})
                continue
            metadata_matches, reason = _source_metadata_matches_manifest(source, manifest)
            if not metadata_matches:
                entries.append({"archive_id": manifest["archive_id"], "relative_path": manifest["relative_path"], "eligible": False, "reason": reason})
                continue
            check = _verify_one(vault, manifest)
            if not check["ok"]:
                entries.append({"archive_id": manifest["archive_id"], "relative_path": manifest["relative_path"], "eligible": False, "reason": check["error"]})
                continue
            matches, reason = _source_matches_manifest(source, manifest)
            entry = {"archive_id": manifest["archive_id"], "relative_path": manifest["relative_path"], "eligible": False, "reason": reason}
            if matches:
                if time.time_ns() - manifest["source_mtime_ns"] < _HOUR_NS:
                    entry["reason"] = "source is less than one hour old"
                else:
                    activity = probe_session_file(source)
                    state = activity.get("state")
                    entry["activity"] = state if state in {"active", "inactive", "unknown"} else "unknown"
                    entry["eligible"] = state == "inactive"
                    entry["reason"] = "eligible" if entry["eligible"] else str(activity.get("reason") or "could not establish selected session file inactivity")
            entries.append(entry)
        result: dict[str, Any] = {
            "ok": True,
            "execute": bool(execute),
            "executed": bool(execute),
            "candidates": entries,
            "removed": [],
            "skipped": [entry for entry in entries if not entry["eligible"]],
            "removed_source_bytes": 0,
        }
        if not execute:
            return result
        selected = [entry for entry in entries if entry["eligible"]]
        if selected:
            by_id = {manifest["archive_id"]: manifest for manifest in manifests}
            for entry in selected:
                try:
                    source = _source_path_for_manifest(by_id[entry["archive_id"]])
                except (VaultError, OSError) as error:
                    entry.update(eligible=False, reason=str(error))
                    result["skipped"].append(entry)
                    continue
                try:
                    _remove_source_atomically(source, by_id[entry["archive_id"]])
                except SessionActivityBlocked as error:
                    entry.update(eligible=False, activity=error.state, reason=str(error))
                    result["skipped"].append(entry)
                    continue
                except _SourceRemovalSkipped as error:
                    entry.update(eligible=False, reason=str(error))
                    result["skipped"].append(entry)
                    continue
                result["removed"].append(entry["relative_path"])
                result["removed_source_bytes"] += by_id[entry["archive_id"]]["raw_bytes"]
        return result


def stats(vault_dir: Path) -> dict[str, Any]:
    vault = _safe_vault(vault_dir, create=False)
    if vault is None:
        return {
            "archives": 0,
            "archive_count": 0,
            "blobs": 0,
            "blob_count": 0,
            "raw_bytes": 0,
            "raw_archived_bytes": 0,
            "source_bytes_retained": 0,
            "retained_source_bytes": 0,
            "managed_bytes": 0,
            "blob_bytes": 0,
            "compressed_bytes": 0,
            "manifest_bytes": 0,
            "vault_bytes": 0,
            "allocated_bytes": 0,
            "net_storage_bytes": 0,
            "compression_savings_estimate_bytes": 0,
            "temporary_files": 0,
            "temporary_bytes": 0,
            "temporary_allocated_bytes": 0,
        }
    with _vault_lock(vault, exclusive=False, create=False):
        manifests = list(_iter_valid_manifests(vault))
        blobs_dir = _safe_vault_subdir(vault, "blobs", create=False)
        blob_bytes = 0
        allocated = 0
        blob_count = 0
        if blobs_dir is not None:
            for entry in os.scandir(blobs_dir):
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode) and _HEX64.fullmatch(entry.name[:-9]) and entry.name.endswith(".jsonl.gz"):
                    blob_count += 1
                    blob_bytes += st.st_size
                    allocated += getattr(st, "st_blocks", 0) * 512
        manifest_bytes = 0
        manifest_allocated = 0
        manifests_dir = _safe_vault_subdir(vault, "manifests", create=False)
        if manifests_dir is not None:
            for entry in os.scandir(manifests_dir):
                st = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISREG(st.st_mode)
                    and not stat.S_ISLNK(st.st_mode)
                    and not entry.name.startswith(".vault-tmp-")
                    and entry.name.endswith(".json")
                ):
                    manifest_bytes += st.st_size
                    manifest_allocated += getattr(st, "st_blocks", 0) * 512
        blob_temporary = _temporary_file_stats(blobs_dir, ".vault-blob-")
        manifest_temporary = _temporary_file_stats(manifests_dir, ".vault-tmp-")
        temporary_files = blob_temporary[0] + manifest_temporary[0]
        temporary_bytes = blob_temporary[1] + manifest_temporary[1]
        temporary_allocated_bytes = blob_temporary[2] + manifest_temporary[2]
        vault_bytes = blob_bytes + manifest_bytes + temporary_bytes
        raw_bytes = sum(item["raw_bytes"] for item in manifests)
        retained_source_bytes = 0
        retained_paths: set[tuple[str, str]] = set()
        for manifest in manifests:
            source_key = (manifest["source_root"], manifest["relative_path"])
            if source_key in retained_paths:
                continue
            retained_paths.add(source_key)
            try:
                source = _source_path_for_manifest(manifest)
                source_stat = _lstat(source)
            except (VaultError, OSError):
                continue
            if source_stat is not None and stat.S_ISREG(source_stat.st_mode) and not stat.S_ISLNK(source_stat.st_mode):
                retained_source_bytes += int(source_stat.st_size)
        managed_bytes = vault_bytes + retained_source_bytes
        return {
            "archives": len(manifests),
            "archive_count": len(manifests),
            "blobs": blob_count,
            "blob_count": blob_count,
            "raw_bytes": raw_bytes,
            "raw_archived_bytes": raw_bytes,
            "source_bytes_retained": retained_source_bytes,
            "retained_source_bytes": retained_source_bytes,
            "managed_bytes": managed_bytes,
            "blob_bytes": blob_bytes,
            "compressed_bytes": blob_bytes,
            "manifest_bytes": manifest_bytes,
            "vault_bytes": vault_bytes,
            "stored_bytes": vault_bytes,
            "allocated_bytes": allocated + manifest_allocated + temporary_allocated_bytes,
            "temporary_files": temporary_files,
            "temporary_bytes": temporary_bytes,
            "temporary_allocated_bytes": temporary_allocated_bytes,
            # ``managed_bytes`` is the current measured storage owned by the
            # vault plus distinct source paths that are still present.  The
            # raw total is historical when one source path has multiple
            # archived versions, so savings are reported as potential
            # compression rather than as a claim about reclaimed disk space.
            "net_storage_bytes": managed_bytes,
            "compression_savings_estimate_bytes": max(0, raw_bytes - vault_bytes),
        }


__all__ = [
    "VaultError",
    "archive",
    "probe_session_file",
    "iter_archive_lines",
    "list_archives",
    "prune",
    "resolve_archive",
    "restore",
    "scan",
    "stats",
    "verify",
    "verified_blob_path",
]
