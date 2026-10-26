"""Grouped archives for non-Codex coding-agent session sources.

This module keeps the v1 Codex manifest namespace untouched.  Source adapters
describe groups; this module snapshots those regular files into a verified
tar.gz bundle and never edits a native database or index.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import ctypes
import errno
import stat
import sys
import tarfile
import tempfile
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator

from . import activity
from . import sources
from . import storage
from .search import _casefold_span


VaultError = storage.VaultError


class _QuarantineRemovalStarted(VaultError):
    """Deletion began; retain quarantine rather than restore a partial group."""

_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_DAY_NS = 86_400 * 1_000_000_000
_HOUR_NS = 3_600 * 1_000_000_000
_HEX64 = set("0123456789abcdef")
_ARCHIVE_ID_LEN = 64
_MAX_MANIFEST_FILES = 100_000
_MAX_MANIFEST_DIRECTORIES = 100_000
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_EXPORT_TOOLS = {"agy", "glm", "opencode", "export"}
_PAX_MTIME_NS = "codex-vault.mtime_ns"
_FORMATS = {
    "jsonl",
    "json",
    "text",
    "binary",
    "claude-jsonl",
    "grok-acp-jsonl",
    "generic-jsonl",
    "codex-jsonl",
    "gemini-json",
    "opencode-json",
    "common-json",
    "model-io-jsonl",
}
_REQUIRED_MANIFEST = {
    "schema_version",
    "archive_id",
    "tool",
    "source_root",
    "session_id",
    "cwd",
    "tool_version",
    "started_at",
    "source_mode",
    "root_entries",
    "directories",
    "files",
    "blob",
    "blob_sha256",
    "raw_bytes",
    "compressed_bytes",
    "archived_at",
}


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _check_components(path: Path) -> None:
    """Use storage's component check without making it part of the public API."""

    storage._check_path_components(path)


def _bundle_root(vault_dir: Path | str, *, create: bool) -> Path | None:
    vault = storage._safe_vault(vault_dir, create=create)
    if vault is None:
        return None
    path = vault / "bundles"
    if create:
        storage._mkdir_private(path)
        storage._mkdir_private(path / "blobs")
        storage._mkdir_private(path / "manifests")
    else:
        _check_components(path)
        st = _lstat(path)
        if st is None:
            return None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise VaultError(f"bundle namespace is unsafe: {path}")
        for name in ("blobs", "manifests"):
            child = path / name
            _check_components(child)
            child_st = _lstat(child)
            if child_st is None or stat.S_ISLNK(child_st.st_mode) or not stat.S_ISDIR(child_st.st_mode):
                raise VaultError(f"bundle namespace is incomplete: {child}")
    return path


def _temporary_file_stats(directory: Path | None, prefix: str) -> tuple[int, int, int]:
    """Measure unfinished bundle files without following symlinks."""

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


def _manifest_path(bundle_root: Path, archive_id: str) -> Path:
    if not isinstance(archive_id, str) or len(archive_id) != _ARCHIVE_ID_LEN or set(archive_id) - _HEX64:
        raise VaultError("bundle archive id is invalid")
    path = bundle_root / "manifests" / f"{archive_id}.json"
    _check_components(path)
    return path


def _valid_rel(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise VaultError("bundle path is invalid")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise VaultError("bundle path escapes source root")
    if p.as_posix() != value:
        raise VaultError("bundle path is not canonical")
    return value


def _source_valid_rel(tool: str, value: str, source_mode: str) -> str:
    value = _valid_rel(value)
    try:
        valid = sources.valid_relative(tool, value, source_mode)
    except Exception as exc:
        raise VaultError("source adapter rejected bundle path") from exc
    if not valid:
        raise VaultError("source adapter rejected bundle path")
    return value


def _identity(manifest_without_id: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest_without_id["schema_version"],
        "tool": manifest_without_id["tool"],
        "source_root": manifest_without_id["source_root"],
        "session_id": manifest_without_id["session_id"],
        "cwd": manifest_without_id["cwd"],
        "tool_version": manifest_without_id["tool_version"],
        "started_at": manifest_without_id["started_at"],
        "source_mode": manifest_without_id["source_mode"],
        "root_entries": manifest_without_id["root_entries"],
        "directories": manifest_without_id["directories"],
        "files": manifest_without_id["files"],
        "blob": manifest_without_id["blob"],
        "blob_sha256": manifest_without_id["blob_sha256"],
        "raw_bytes": manifest_without_id["raw_bytes"],
        "compressed_bytes": manifest_without_id["compressed_bytes"],
    }


def _compute_archive_id(manifest_without_id: dict[str, Any]) -> str:
    encoded = json.dumps(_identity(manifest_without_id), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_root_scope(
    tool: str,
    source_mode: str,
    session_id: str,
    roots: list[str],
    file_paths: set[str],
    directory_paths: set[str],
) -> None:
    member_paths = file_paths | directory_paths
    if any(root not in member_paths for root in roots):
        raise VaultError("bundle root_entries must name actual files or directories")
    if len({str(PurePosixPath(root).parent) for root in roots}) != 1:
        raise VaultError("bundle root_entries must share one parent")
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if right.startswith(left + "/") or left.startswith(right + "/"):
                raise VaultError("bundle root_entries overlap")

    if source_mode == "export":
        if tool not in _EXPORT_TOOLS or len(roots) != 1 or roots[0] not in file_paths:
            raise VaultError("export bundle root must be one explicit export file")
        return
    if source_mode == "diagnostic":
        if (
            tool not in {"zcode", "autoclaw"}
            or len(roots) != 1
            or file_paths != set(roots)
            or directory_paths
            or len(PurePosixPath(roots[0]).parts) != 1
            or session_id != PurePosixPath(roots[0]).stem
        ):
            raise VaultError("diagnostic bundle must contain one flat model-io log file")
        return
    if not session_id or "/" in session_id or "\\" in session_id:
        raise VaultError("native bundle session id is invalid")
    parts = [PurePosixPath(root).parts for root in roots]
    if tool == "claude":
        if len(roots) not in {1, 2} or any(len(item) != 3 or item[0] != "projects" for item in parts):
            raise VaultError("Claude bundle roots are outside one session")
        projects = {item[1] for item in parts}
        names = {item[2] for item in parts}
        allowed = {f"{session_id}.jsonl", session_id}
        if len(projects) != 1 or f"{session_id}.jsonl" not in names or not names.issubset(allowed):
            raise VaultError("Claude bundle roots do not match the session id")
        main = f"projects/{next(iter(projects))}/{session_id}.jsonl"
        if main not in file_paths:
            raise VaultError("Claude bundle main transcript is missing")
        if session_id in names and f"projects/{next(iter(projects))}/{session_id}" not in directory_paths:
            raise VaultError("Claude companion root is not a directory")
    elif tool == "grok":
        if len(roots) != 1 or len(parts[0]) != 3 or parts[0][0] != "sessions" or parts[0][2] != session_id:
            raise VaultError("Grok bundle root does not match one session directory")
        if roots[0] not in directory_paths:
            raise VaultError("Grok bundle root is not a directory")
    elif tool == "gemini":
        if len(roots) != 1 or len(parts[0]) != 4 or parts[0][0] != "tmp" or parts[0][2] != "chats":
            raise VaultError("Gemini bundle root is outside one chat")
        name = parts[0][3]
        if roots[0] not in file_paths or not name.startswith(f"session-{session_id}.") or not name.endswith((".json", ".jsonl")):
            raise VaultError("Gemini bundle root does not match the session id")
    elif tool == "codex":
        if len(roots) != 1 or roots[0] not in file_paths:
            raise VaultError("Codex grouped bundle root must be one rollout file")
    else:
        raise VaultError("native bundle tool is unsupported")


def _validate_topology(roots: list[str], file_paths: set[str], directory_paths: set[str]) -> None:
    if file_paths & directory_paths:
        raise VaultError("bundle file and directory paths overlap")
    for path in file_paths | directory_paths:
        owners = [root for root in roots if path == root or path.startswith(root + "/")]
        if len(owners) != 1:
            raise VaultError("bundle member must belong to exactly one root entry")
        owner = owners[0]
        if path == owner:
            continue
        parent = PurePosixPath(path).parent.as_posix()
        if parent != owner and parent not in directory_paths:
            raise VaultError("bundle directory ancestry is incomplete")
        if owner not in directory_paths:
            raise VaultError("bundle file root cannot contain descendants")


def _validate_manifest(manifest: Any, *, expected_id: str | None = None) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _REQUIRED_MANIFEST:
        raise VaultError("bundle manifest is missing required fields")
    if manifest.get("schema_version") != 1:
        raise VaultError("bundle manifest schema version is unsupported")
    archive_id = manifest.get("archive_id")
    if not isinstance(archive_id, str) or len(archive_id) != _ARCHIVE_ID_LEN or set(archive_id) - _HEX64:
        raise VaultError("bundle archive id is invalid")
    if expected_id is not None and archive_id != expected_id:
        raise VaultError("bundle manifest filename does not match archive id")
    tool = manifest.get("tool")
    source_mode = manifest.get("source_mode")
    if not isinstance(tool, str) or not tool or not isinstance(source_mode, str) or source_mode not in {"native", "export", "diagnostic"}:
        raise VaultError("bundle tool or source_mode is invalid")
    source_root = manifest.get("source_root")
    if (
        not isinstance(source_root, str)
        or not Path(source_root).is_absolute()
        or any(part in {".", ".."} for part in Path(source_root).parts)
        or str(_absolute(source_root)) != source_root
    ):
        raise VaultError("bundle source_root is unsafe")
    for key in ("session_id", "cwd", "tool_version", "started_at", "archived_at"):
        if not isinstance(manifest.get(key), str):
            raise VaultError(f"bundle manifest {key} is invalid")
    session_id = manifest["session_id"]
    root_entries = manifest.get("root_entries")
    if not isinstance(root_entries, list) or not root_entries:
        raise VaultError("bundle root_entries is invalid")
    roots = [_valid_rel(value) for value in root_entries]
    if roots != sorted(set(roots)):
        raise VaultError("bundle root_entries are not unique and canonical")
    directories = manifest.get("directories")
    if not isinstance(directories, list) or len(directories) > _MAX_MANIFEST_DIRECTORIES:
        raise VaultError("bundle directories are invalid")
    normalized_directories: list[dict[str, Any]] = []
    seen_directories: set[str] = set()
    for item in directories:
        if not isinstance(item, dict) or not {"path", "mode", "mtime_ns"}.issubset(item):
            raise VaultError("bundle directory entry is invalid")
        path = _valid_rel(item["path"])
        if path in seen_directories:
            raise VaultError("bundle contains duplicate directory paths")
        seen_directories.add(path)
        for key in ("mode", "mtime_ns"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VaultError(f"bundle directory {key} is invalid")
        if item["mode"] > 0o1777:
            raise VaultError("bundle directory mode is invalid")
        normalized_directories.append({"path": path, "mode": int(item["mode"]), "mtime_ns": int(item["mtime_ns"])})
    if [item["path"] for item in normalized_directories] != sorted(item["path"] for item in normalized_directories):
        raise VaultError("bundle directories are not in canonical order")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > _MAX_MANIFEST_FILES:
        raise VaultError("bundle files are invalid")
    normalized_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not {"path", "sha256", "size", "mode", "mtime_ns", "format"}.issubset(item):
            raise VaultError("bundle file entry is invalid")
        path = _source_valid_rel(tool, item["path"], source_mode)
        if path in seen:
            raise VaultError("bundle contains duplicate file paths")
        seen.add(path)
        sha = item["sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or set(sha) - _HEX64:
            raise VaultError("bundle file hash is invalid")
        for key in ("size", "mode", "mtime_ns"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VaultError(f"bundle file {key} is invalid")
        format_name = item["format"]
        if item["mode"] > 0o1777 or not isinstance(format_name, str) or format_name not in _FORMATS:
            raise VaultError("bundle file metadata is invalid")
        normalized_files.append({
            "path": path,
            "sha256": sha,
            "size": int(item["size"]),
            "mode": int(item["mode"]),
            "mtime_ns": int(item["mtime_ns"]),
            "format": format_name,
        })
    if [item["path"] for item in normalized_files] != sorted(item["path"] for item in normalized_files):
        raise VaultError("bundle files are not in canonical order")
    file_paths = {item["path"] for item in normalized_files}
    directory_paths = {item["path"] for item in normalized_directories}
    if source_mode == "diagnostic" and any(item["format"] != "model-io-jsonl" for item in normalized_files):
        raise VaultError("diagnostic bundle must use the model-io text reader")
    _validate_topology(roots, file_paths, directory_paths)
    _validate_root_scope(tool, source_mode, session_id, roots, file_paths, directory_paths)
    blob_sha = manifest.get("blob_sha256")
    if not isinstance(blob_sha, str) or len(blob_sha) != 64 or set(blob_sha) - _HEX64:
        raise VaultError("bundle blob hash is invalid")
    if manifest.get("blob") != f"bundles/blobs/{blob_sha}.tar.gz":
        raise VaultError("bundle blob path is invalid")
    for key in ("raw_bytes", "compressed_bytes"):
        value = manifest.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VaultError(f"bundle manifest {key} is invalid")
    if manifest["raw_bytes"] != sum(item["size"] for item in normalized_files):
        raise VaultError("bundle manifest raw byte count is invalid")
    normalized = dict(manifest)
    normalized["root_entries"] = roots
    normalized["directories"] = normalized_directories
    normalized["files"] = normalized_files
    if _compute_archive_id(normalized) != archive_id:
        raise VaultError("bundle archive id does not match manifest identity")
    return normalized


def _read_manifest(path: Path, *, expected_id: str | None = None) -> dict[str, Any]:
    st = storage._safe_regular(path, label="bundle manifest")
    if st.st_size > _MAX_MANIFEST_BYTES:
        raise VaultError("bundle manifest exceeds maximum size")
    try:
        fd = storage._open_nofollow(path, os.O_RDONLY)
        try:
            stream = os.fdopen(fd, "rb")
            fd = -1
            with stream:
                encoded = stream.read(_MAX_MANIFEST_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise VaultError("bundle manifest exceeds maximum size")
        raw = json.loads(
            encoded.decode("utf-8"), parse_int=storage._bounded_json_int
        )
    except VaultError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise VaultError("bundle manifest is invalid JSON") from exc
    return _validate_manifest(raw, expected_id=expected_id)


def _iter_manifest_paths(bundle_root: Path) -> Iterator[Path]:
    directory = bundle_root / "manifests"
    try:
        entries = sorted(os.scandir(directory), key=lambda item: item.name)
    except OSError as exc:
        raise VaultError("cannot enumerate bundle manifests") from exc
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        archive_id = entry.name[:-5]
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        if len(archive_id) != 64 or set(archive_id) - _HEX64:
            continue
        yield Path(entry.path)


def _iter_manifests(bundle_root: Path) -> Iterator[dict[str, Any]]:
    for path in _iter_manifest_paths(bundle_root):
        yield _read_manifest(path, expected_id=path.name[:-5])


def _resolve(bundle_root: Path, archive_id: str) -> dict[str, Any]:
    if not isinstance(archive_id, str) or not archive_id or len(archive_id) > 64 or set(archive_id) - _HEX64:
        raise VaultError("bundle archive id is invalid")
    if len(archive_id) == _ARCHIVE_ID_LEN:
        exact = _manifest_path(bundle_root, archive_id)
        if _lstat(exact) is not None:
            return _read_manifest(exact, expected_id=archive_id)
    paths = [path for path in _iter_manifest_paths(bundle_root) if path.name[:-5].startswith(archive_id)]
    if not paths:
        raise VaultError("bundle archive was not found")
    if len(paths) > 1:
        raise VaultError("bundle archive id prefix is ambiguous")
    return _read_manifest(paths[0], expected_id=paths[0].name[:-5])


def _safe_source_file(root: Path, relative: str) -> tuple[Path, os.stat_result]:
    root = storage._require_directory(root, label="bundle source root")
    relative = _valid_rel(relative)
    path = root.joinpath(*PurePosixPath(relative).parts)
    _check_components(path)
    st = storage._safe_regular(path, label="bundle source file")
    return path, st


def _identity_stat(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, stat.S_IMODE(st.st_mode))


class _RestoreIdentityCache(dict[str, tuple[int, int, int, int, int, int]]):
    """Verified destination identities shared by a restore's preflight walks.

    ``trusted_files`` predates this cache and means files published by the
    current restore.  Keep that mapping shape for callers while tracking the
    narrower publication set separately: an equal pre-existing file may be
    rehashed when its identity changes, whereas a file published by this
    restore must still fail the final identity barrier.
    """

    def __init__(self) -> None:
        super().__init__()
        self.published_paths: set[str] = set()


def _cache_file_is_published(
    trusted_files: dict[str, tuple[int, int, int, int, int, int]], relative: str
) -> bool:
    published_paths = getattr(trusted_files, "published_paths", None)
    # Plain dictionaries are the legacy ``trusted_files`` interface: every
    # supplied identity is trusted as a just-published file.
    return published_paths is None or relative in published_paths


def _hash_and_cache_destination(
    target: Path,
    entry: dict[str, Any],
    trusted_files: dict[str, tuple[int, int, int, int, int, int]] | None,
) -> tuple[int, int, int, int, int, int]:
    """Hash a destination and only cache it after a post-hash identity check."""

    digest, size, before = storage._hash_path(target)
    after = storage._safe_regular(target, label="destination member")
    if _identity_stat(after) != _identity_stat(before):
        raise VaultError(f"destination member changed while checking: {entry['path']}")
    if digest != entry["sha256"] or size != entry["size"]:
        raise VaultError(f"destination already contains different bytes: {entry['path']}")
    identity = _identity_stat(after)
    if trusted_files is not None:
        trusted_files[entry["path"]] = identity
    return identity


class _HashingReader:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.total = 0
        self.prefix = bytearray()

    def _track(self, data: bytes) -> bytes:
        self.digest.update(data)
        self.total += len(data)
        if len(self.prefix) < 64 * 1024:
            self.prefix.extend(data[: 64 * 1024 - len(self.prefix)])
        return data

    def read(self, size: int = -1) -> bytes:
        return self._track(self.stream.read(size))

    def readline(self, size: int = -1) -> bytes:
        return self._track(self.stream.readline(size))


class _BoundedReader:
    """Limit bytes returned by a decoded stream before consumers allocate them."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining_with_probe = self.limit - self.total + 1
        if remaining_with_probe <= 0:
            raise VaultError("bundle decoded tar exceeds safe size")
        request = remaining_with_probe if size < 0 else min(size, remaining_with_probe)
        data = self.stream.read(request)
        self.total += len(data)
        if self.total > self.limit:
            raise VaultError("bundle decoded tar exceeds safe size")
        return data


def _pax_record_size(key: str, value: str) -> int:
    content_size = len(key.encode("utf-8")) + len(value.encode("utf-8")) + 3
    record_size = content_size + 1
    while True:
        updated = content_size + len(str(record_size))
        if updated == record_size:
            return record_size
        record_size = updated


def _round_tar_block(size: int) -> int:
    return ((size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE


def _decoded_tar_limit(manifest: dict[str, Any]) -> int:
    """Upper-bound tar bytes emitted by this module's PAX writer.

    Every member has a normal header and a custom nanosecond PAX record.  A
    PAX writer may also need path, size, and mtime records, so include all of
    them even when a particular value fits the ustar header.  The final record
    allowance covers the two zero blocks and tarfile's RECORDSIZE padding.
    """

    total = 0
    for entry in manifest["files"]:
        pax_size = sum(
            _pax_record_size(key, value)
            for key, value in (
                (_PAX_MTIME_NS, str(entry["mtime_ns"])),
                ("path", entry["path"]),
                ("size", str(entry["size"])),
                ("mtime", str(entry["mtime_ns"] // 1_000_000_000)),
            )
        )
        total += tarfile.BLOCKSIZE
        total += _round_tar_block(entry["size"])
        total += tarfile.BLOCKSIZE + _round_tar_block(pax_size)
    return (
        (total + 2 * tarfile.BLOCKSIZE + tarfile.RECORDSIZE - 1)
        // tarfile.RECORDSIZE
    ) * tarfile.RECORDSIZE


def _tar_info(path: str, st: os.stat_result) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.type = tarfile.REGTYPE
    info.mode = stat.S_IMODE(st.st_mode)
    info.size = st.st_size
    info.mtime = st.st_mtime_ns // 1_000_000_000
    info.pax_headers = {_PAX_MTIME_NS: str(st.st_mtime_ns)}
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _capture_directories(root: Path, group: Any, file_paths: set[str]) -> list[dict[str, Any]]:
    directories: dict[str, dict[str, Any]] = {}
    regular_paths: set[str] = set()
    for root_relative in sorted(_valid_rel(value) for value in tuple(group.root_entries)):
        root_path = root.joinpath(*PurePosixPath(root_relative).parts)
        _check_components(root_path)
        root_stat = _lstat(root_path)
        if root_stat is None or stat.S_ISLNK(root_stat.st_mode):
            raise VaultError(f"bundle root entry is unsafe: {root_relative}")
        if stat.S_ISREG(root_stat.st_mode):
            regular_paths.add(root_relative)
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            raise VaultError(f"bundle root entry is not regular or directory: {root_relative}")
        stack = [root_path]
        while stack:
            current = stack.pop()
            current_relative = current.relative_to(root).as_posix()
            current_stat = _lstat(current)
            if current_stat is None or stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise VaultError(f"bundle directory is unsafe: {current_relative}")
            directories[current_relative] = {
                "path": current_relative,
                "mode": stat.S_IMODE(current_stat.st_mode),
                "mtime_ns": int(current_stat.st_mtime_ns),
            }
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name)
            except OSError as exc:
                raise VaultError(f"bundle directory cannot be read: {current_relative}") from exc
            for entry in entries:
                relative = Path(entry.path).relative_to(root).as_posix()
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise VaultError(f"bundle member cannot be inspected: {relative}") from exc
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise VaultError(f"bundle member is a symlink: {relative}")
                if stat.S_ISDIR(entry_stat.st_mode):
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(entry_stat.st_mode):
                    regular_paths.add(relative)
                else:
                    raise VaultError(f"bundle member is not regular: {relative}")
    if regular_paths != file_paths:
        raise VaultError("source group did not enumerate the complete regular member set")
    return [directories[key] for key in sorted(directories)]


def _snapshot_bundle(
    root: Path, group: Any, blobs_dir: Path
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]], int, str, bytes]:
    files = sorted(_valid_rel(value) for value in tuple(group.files))
    if not files or len(files) != len(set(files)):
        raise VaultError("source group has no unique files")
    source_mode = str(group.source_mode)
    directories = _capture_directories(root, group, set(files))
    snapshots: list[tuple[str, Path, os.stat_result]] = []
    for relative in files:
        path, st = _safe_source_file(root, relative)
        try:
            valid = sources.valid_relative(group.tool, relative, source_mode)
        except Exception as exc:
            raise VaultError("source adapter rejected group file") from exc
        if not valid:
            raise VaultError("source adapter rejected group file")
        snapshots.append((relative, path, st))
    temp_fd, temp_name = tempfile.mkstemp(prefix=".bundle-", dir=blobs_dir)
    temp = Path(temp_name)
    metadata_prefix = b""
    metadata_prefix_captured = False
    file_entries: list[dict[str, Any]] = []
    try:
        os.fchmod(temp_fd, _PRIVATE_FILE)
        with os.fdopen(temp_fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    for relative, path, before in snapshots:
                        fd = storage._open_nofollow(path, os.O_RDONLY)
                        try:
                            opened = os.fstat(fd)
                            if _identity_stat(opened) != _identity_stat(before):
                                raise VaultError(f"source changed before snapshot: {relative}")
                            with os.fdopen(fd, "rb") as stream:
                                fd = -1
                                reader = _HashingReader(stream)
                                archive.addfile(_tar_info(relative, before), reader)
                                if not metadata_prefix_captured:
                                    metadata_prefix = bytes(reader.prefix)
                                    metadata_prefix_captured = True
                            after = storage._safe_regular(path, label="bundle source file")
                            if _identity_stat(after) != _identity_stat(before) or reader.total != before.st_size:
                                raise VaultError(f"source changed while snapshotting: {relative}")
                            format_name = group.formats.get(relative, "binary")
                            file_entries.append(
                                {
                                    "path": relative,
                                    "sha256": reader.digest.hexdigest(),
                                    "size": reader.total,
                                    "mode": stat.S_IMODE(before.st_mode),
                                    "mtime_ns": before.st_mtime_ns,
                                    "format": format_name,
                                }
                            )
                        finally:
                            if fd >= 0:
                                os.close(fd)
            raw.flush()
            os.fsync(raw.fileno())
        digest = hashlib.sha256()
        total = 0
        with temp.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
        after_directories = _capture_directories(root, group, set(files))
        if after_directories != directories:
            raise VaultError("source directory metadata changed while snapshotting")
        for relative, path, before in snapshots:
            after = storage._safe_regular(path, label="bundle source file")
            if _identity_stat(after) != _identity_stat(before):
                raise VaultError(f"source changed after bundle snapshot: {relative}")
        return temp, directories, file_entries, total, digest.hexdigest(), metadata_prefix
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _stream_verified_blob(
    blob: Path,
    manifest: dict[str, Any],
    *,
    stage_root: Path | None = None,
    record_callback: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> tuple[int, dict[str, Path]]:
    if stage_root is not None and record_callback is not None:
        raise VaultError("bundle stream cannot stage and decode at the same time")
    expected = {item["path"]: item for item in manifest["files"]}
    seen: set[str] = set()
    staged: dict[str, Path] = {}
    raw_total = 0
    blob_stat = storage._safe_regular(blob, label="bundle blob")
    if blob_stat.st_size != manifest["compressed_bytes"]:
        raise VaultError("bundle compressed size differs")
    fd = storage._open_nofollow(blob, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        if _identity_stat(opened) != _identity_stat(blob_stat):
            raise VaultError("bundle blob changed before verification")
        with os.fdopen(fd, "rb") as raw:
            fd = -1
            compressed_reader = _HashingReader(raw)
            with gzip.GzipFile(fileobj=compressed_reader, mode="rb") as gz:
                decoded_reader = _BoundedReader(gz, _decoded_tar_limit(manifest))
                with tarfile.open(fileobj=decoded_reader, mode="r|") as tar:
                    for member in tar:
                        name = member.name
                        if name in seen or name not in expected or not member.isreg() or member.linkname:
                            raise VaultError("bundle tar member set is unsafe")
                        if _valid_rel(name) != name:
                            raise VaultError("bundle tar member path is not canonical")
                        entry = expected[name]
                        if (
                            member.size != entry["size"]
                            or stat.S_IMODE(member.mode) != entry["mode"]
                            or member.mtime != entry["mtime_ns"] // 1_000_000_000
                            or member.pax_headers.get(_PAX_MTIME_NS) != str(entry["mtime_ns"])
                        ):
                            raise VaultError("bundle tar member metadata differs")
                        stream = tar.extractfile(member)
                        if stream is None:
                            raise VaultError("bundle tar member has no data")
                        if record_callback is not None:
                            reader = _HashingReader(stream)
                            try:
                                for record in sources.iter_text_records(reader, entry["format"]):
                                    record_callback(entry, dict(record))
                            except VaultError:
                                raise
                            except ValueError as exc:
                                raise VaultError("bundle text record decoding/processing failed") from exc
                            while reader.read(1024 * 1024):
                                pass
                            if reader.digest.hexdigest() != entry["sha256"] or reader.total != entry["size"]:
                                raise VaultError("bundle tar member hash or size differs")
                            raw_total += reader.total
                            seen.add(name)
                            continue
                        output = None
                        output_path: Path | None = None
                        digest = hashlib.sha256()
                        actual_size = 0
                        try:
                            if stage_root is not None:
                                output_path = stage_root.joinpath(*PurePosixPath(name).parts)
                                storage._mkdir_private(output_path.parent)
                                output_fd = os.open(
                                    output_path,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                    _PRIVATE_FILE,
                                )
                                output = os.fdopen(output_fd, "wb")
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                                actual_size += len(chunk)
                                if output is not None:
                                    output.write(chunk)
                            if output is not None:
                                output.flush()
                                os.fsync(output.fileno())
                        finally:
                            if output is not None:
                                output.close()
                        if digest.hexdigest() != entry["sha256"] or actual_size != entry["size"]:
                            raise VaultError("bundle tar member hash or size differs")
                        if output_path is not None:
                            staged[name] = output_path
                        raw_total += actual_size
                        seen.add(name)
                while decoded_reader.read(1024 * 1024):
                    pass
            while compressed_reader.read(1024 * 1024):
                pass
            if compressed_reader.total != manifest["compressed_bytes"] or compressed_reader.digest.hexdigest() != manifest["blob_sha256"]:
                raise VaultError("bundle compressed digest or size differs")
        if seen != set(expected) or raw_total != manifest["raw_bytes"]:
            raise VaultError("bundle raw byte or member count differs")
        return raw_total, staged
    except VaultError:
        raise
    except (OSError, tarfile.TarError, EOFError, ValueError, zlib.error) as exc:
        if isinstance(exc, VaultError):
            raise
        raise VaultError("bundle tar.gz is corrupt") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _verify_blob(bundle_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        blob = bundle_root.parent / manifest["blob"]
        total, _ = _stream_verified_blob(blob, manifest)
        return {"ok": True, "archive_id": manifest["archive_id"], "raw_bytes": total, "compressed_bytes": manifest["compressed_bytes"]}
    except (VaultError, OSError) as exc:
        return {"ok": False, "archive_id": manifest.get("archive_id", ""), "error": str(exc)}


def _same_published_manifest(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return {key: value for key, value in left.items() if key != "archived_at"} == {
        key: value for key, value in right.items() if key != "archived_at"
    }


def _write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = _validate_manifest(manifest, expected_id=path.name[:-5])
    if _lstat(path) is not None:
        existing = _read_manifest(path, expected_id=manifest["archive_id"])
        if not _same_published_manifest(existing, manifest):
            raise VaultError("published bundle manifest differs from the archive being created")
        return existing
    fd, name = tempfile.mkstemp(prefix=".bundle-manifest-", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, _PRIVATE_FILE)
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        published = storage._publish_temp_no_replace(temp, path)
        storage._fsync_directory(path.parent)
        existing = _read_manifest(path, expected_id=manifest["archive_id"])
        if not _same_published_manifest(existing, manifest):
            raise VaultError("published bundle manifest differs from the archive being created")
        if published and existing != manifest:
            raise VaultError("newly published bundle manifest did not round-trip")
        return existing
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _publish_blob(bundle_root: Path, temp: Path, sha: str) -> Path:
    target = bundle_root / "blobs" / f"{sha}.tar.gz"
    _check_components(target)
    existing = _lstat(target)
    if existing is not None:
        storage._safe_regular(target, label="existing bundle blob")
        temp.unlink(missing_ok=True)
    else:
        storage._publish_temp_no_replace(temp, target)
    storage._fsync_directory(target.parent)
    return target


def _group_candidates(tool: str, source_root: Path, older_than_days: int) -> list[dict[str, Any]]:
    if tool == "codex":
        raise VaultError("Codex sessions must use the storage backend")
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < 0:
        raise VaultError("older_than_days must be a non-negative integer")
    root = storage._require_directory(source_root, label="bundle source root")
    groups = sources.discover(tool, root)
    cutoff = time.time_ns() - older_than_days * _DAY_NS
    result: list[dict[str, Any]] = []
    for group in groups:
        try:
            files = sorted(_valid_rel(value) for value in group.files)
            root_entries = sorted(_valid_rel(value) for value in group.root_entries)
            if not files:
                result.append({
                    "tool": group.tool,
                    "source_root": str(root),
                    "session_id": str(group.session_id),
                    "cwd": str(getattr(group, "cwd", "")),
                    "source_mode": str(group.source_mode),
                    "files": [],
                    "root_entries": root_entries,
                    "source_bytes": 0,
                    "mtime_ns": 0,
                    "eligible": False,
                    "reason": "; ".join(group.notes) or "source group has no recognized regular files",
                    "_group": group,
                })
                continue
            stats = [storage._safe_regular(root.joinpath(*PurePosixPath(value).parts), label="bundle source file") for value in files]
            directories = _capture_directories(root, group, set(files))
            directory_mtimes = [int(item["mtime_ns"]) for item in directories]
            member_mtimes = [stat_item.st_mtime_ns for stat_item in stats] + directory_mtimes
            newest = max(member_mtimes)
            eligible = bool(group.removable) and newest <= cutoff and all(item <= cutoff for item in member_mtimes)
            reason = "eligible" if eligible else ("too_new" if newest > cutoff else "; ".join(group.notes) or "not_removable")
            result.append({
                "tool": group.tool,
                "source_root": str(root),
                "session_id": str(group.session_id),
                "cwd": str(getattr(group, "cwd", "")),
                "source_mode": str(group.source_mode),
                "files": files,
                "root_entries": root_entries,
                "source_bytes": sum(stat_item.st_size for stat_item in stats),
                "mtime_ns": newest,
                "eligible": eligible,
                "reason": reason,
                "_group": group,
            })
        except (VaultError, OSError) as exc:
            result.append({"tool": group.tool, "source_root": str(root), "session_id": str(group.session_id), "files": [], "root_entries": [], "source_bytes": 0, "eligible": False, "reason": str(exc), "_group": group})
    return result


def scan(tool: str, source_root: Path, older_than_days: int = 30) -> dict[str, Any]:
    candidates = _group_candidates(tool, source_root, older_than_days)
    for candidate in candidates:
        candidate.pop("_group", None)
    return {"tool": tool, "source_root": str(_absolute(source_root)), "older_than_days": older_than_days, "candidates": candidates, "source_bytes": sum(item["source_bytes"] for item in candidates if item["eligible"])}


def archive(
    tool: str,
    source_root: Path,
    vault_dir: Path,
    older_than_days: int = 30,
    execute: bool = False,
    remove_originals: bool = False,
) -> dict[str, Any]:
    candidates = _group_candidates(tool, source_root, older_than_days)
    result: dict[str, Any] = {"execute": bool(execute), "executed": bool(execute),
                              "tool": tool, "source_root": str(_absolute(source_root)),
                              "vault_dir": str(_absolute(vault_dir)),
                              "remove_originals_requested": bool(remove_originals),
                              "candidates": [{key: value for key, value in item.items() if key != "_group"} for item in candidates],
                              "archived": [], "removed": [], "skipped": [], "errors": [], "removed_source_bytes": 0}
    if not execute:
        return result
    selected = [item for item in candidates if item["eligible"]]
    if not selected:
        return result
    with storage._vault_lock(vault_dir, exclusive=True, create=True) as vault:
        bundle_root = _bundle_root(vault, create=True)
        assert bundle_root is not None
        for candidate in selected:
            group = candidate["_group"]
            temp: Path | None = None
            publication_started = False
            try:
                temp, directories, files, compressed_size, compressed_sha, metadata_prefix = _snapshot_bundle(
                    Path(candidate["source_root"]), group, bundle_root / "blobs"
                )
                files.sort(key=lambda item: item["path"])
                first_format = files[0]["format"] if files else "binary"
                metadata = sources.metadata_from_bytes(metadata_prefix, first_format)
                manifest_body: dict[str, Any] = {
                    "schema_version": 1,
                    "tool": group.tool,
                    "source_root": str(candidate["source_root"]),
                    "session_id": str(group.session_id),
                    "cwd": str(metadata.get("cwd") or getattr(group, "cwd", "")),
                    "tool_version": str(metadata.get("tool_version", "")),
                    "started_at": str(metadata.get("started_at", "")),
                    "source_mode": str(group.source_mode),
                    "root_entries": sorted(group.root_entries),
                    "directories": directories,
                    "files": files,
                    "blob": f"bundles/blobs/{compressed_sha}.tar.gz",
                    "blob_sha256": compressed_sha,
                    "raw_bytes": sum(item["size"] for item in files),
                    "compressed_bytes": compressed_size,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                }
                manifest = {**manifest_body, "archive_id": _compute_archive_id(manifest_body)}
                manifest = _validate_manifest(manifest)
                publication_started = True
                _publish_blob(bundle_root, temp, compressed_sha)
                temp = None
                checked = _verify_blob(bundle_root, manifest)
                if not checked["ok"]:
                    raise VaultError(checked["error"])
                published = _write_manifest_atomic(_manifest_path(bundle_root, manifest["archive_id"]), manifest)
                checked = _verify_blob(bundle_root, published)
                if not checked["ok"]:
                    raise VaultError(checked["error"])
                result["archived"].append(published)
            except (VaultError, OSError) as exc:
                if publication_started:
                    raise
                result["errors"].append(
                    {
                        "tool": candidate["tool"],
                        "session_id": candidate["session_id"],
                        "reason": str(exc),
                    }
                )
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
        if remove_originals:
            for manifest in result["archived"]:
                _prune_one(vault, bundle_root, manifest, result)
    return result


def list_archives(vault_dir: Path, tool: str | None = None) -> list[dict[str, Any]]:
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        return []
    with storage._vault_lock(vault, exclusive=False, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            return []
        rows = [manifest for manifest in _iter_manifests(bundle_root) if tool is None or manifest["tool"] == tool]
    rows.sort(key=lambda item: (item["archived_at"], item["archive_id"]))
    return rows


def verify(vault_dir: Path, archive_id: str | None = None, tool: str | None = None) -> dict[str, Any]:
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with storage._vault_lock(vault, exclusive=False, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            if archive_id:
                raise VaultError("bundle namespace does not exist")
            return {"ok": True, "verified": [], "failed": [], "count": 0}
        if archive_id:
            manifest = _resolve(bundle_root, archive_id)
            return _verify_blob(bundle_root, manifest)
        verified: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for path in _iter_manifest_paths(bundle_root):
            manifest_id = path.name[:-5]
            try:
                manifest = _read_manifest(path, expected_id=manifest_id)
            except VaultError as exc:
                failed.append({"archive_id": manifest_id, "manifest": str(path), "error": str(exc)})
                continue
            if tool is not None and manifest["tool"] != tool:
                continue
            check = _verify_blob(bundle_root, manifest)
            if check["ok"]:
                verified.append(check)
            else:
                failed.append({**check, "manifest": str(path)})
    return {"ok": not failed, "verified": verified, "failed": failed, "count": len(verified) + len(failed)}


def _destination_root(path: Path, execute: bool) -> Path:
    path = _absolute(path)
    if execute:
        storage._mkdir_private(path, tighten_existing=False)
    else:
        st = _lstat(path)
        if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)):
            raise VaultError("destination root is unsafe")
    return path


def _restore_preflight(
    root: Path,
    manifest: dict[str, Any],
    trusted_files: dict[str, tuple[int, int, int, int, int, int]] | None = None,
) -> dict[str, str]:
    if manifest["source_mode"] == "native":
        return _restore_native_preflight(root, manifest, trusted_files)
    cached_files = trusted_files if trusted_files is not None else {}
    conflicts: dict[str, str] = {}
    for directory in manifest["directories"]:
        target_directory = root.joinpath(*PurePosixPath(directory["path"]).parts)
        _check_components(target_directory.parent)
        directory_stat = _lstat(target_directory)
        if directory_stat is None:
            continue
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise VaultError(f"destination directory conflicts with a non-directory: {directory['path']}")
    for entry in manifest["files"]:
        target = root.joinpath(*PurePosixPath(entry["path"]).parts)
        _check_components(target.parent)
        st = _lstat(target)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise VaultError(f"destination member is unsafe: {entry['path']}")
        trusted_identity = cached_files.get(entry["path"])
        if trusted_identity is not None:
            if _identity_stat(st) == trusted_identity:
                conflicts[entry["path"]] = "equal"
                continue
            if _cache_file_is_published(cached_files, entry["path"]):
                raise VaultError(f"destination member changed after publication: {entry['path']}")
            cached_files.pop(entry["path"], None)
        _hash_and_cache_destination(target, entry, cached_files)
        conflicts[entry["path"]] = "equal"
    return conflicts


def _restore_native_preflight(
    root: Path,
    manifest: dict[str, Any],
    trusted_files: dict[str, tuple[int, int, int, int, int, int]] | None = None,
) -> dict[str, str]:
    equal: dict[str, str] = {}
    cached_files = trusted_files if trusted_files is not None else {}
    expected_files = {item["path"]: item for item in manifest["files"]}
    expected_directories = {item["path"] for item in manifest["directories"]}
    for root_relative in manifest["root_entries"]:
        target_root = root.joinpath(*PurePosixPath(root_relative).parts)
        _check_components(target_root.parent)
        root_stat = _lstat(target_root)
        if root_stat is None:
            continue
        root_files = {
            path: item
            for path, item in expected_files.items()
            if path == root_relative or path.startswith(root_relative + "/")
        }
        root_directories = {
            path for path in expected_directories if path == root_relative or path.startswith(root_relative + "/")
        }
        actual_files: set[str] = set()
        actual_directories: set[str] = set()
        if stat.S_ISLNK(root_stat.st_mode):
            raise VaultError(f"destination native root is a symlink: {root_relative}")
        if stat.S_ISREG(root_stat.st_mode):
            actual_files.add(root_relative)
        elif stat.S_ISDIR(root_stat.st_mode):
            stack = [(target_root, root_relative)]
            while stack:
                current, current_relative = stack.pop()
                actual_directories.add(current_relative)
                try:
                    entries = sorted(os.scandir(current), key=lambda item: item.name)
                except OSError as exc:
                    raise VaultError(f"destination native root cannot be read: {root_relative}") from exc
                for entry in entries:
                    child_relative = f"{current_relative}/{entry.name}"
                    try:
                        child_stat = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise VaultError(f"destination native member cannot be inspected: {child_relative}") from exc
                    if stat.S_ISLNK(child_stat.st_mode):
                        raise VaultError(f"destination native member is a symlink: {child_relative}")
                    if stat.S_ISDIR(child_stat.st_mode):
                        stack.append((Path(entry.path), child_relative))
                    elif stat.S_ISREG(child_stat.st_mode):
                        actual_files.add(child_relative)
                    else:
                        raise VaultError(f"destination native member is unsafe: {child_relative}")
        else:
            raise VaultError(f"destination native root is unsafe: {root_relative}")
        if actual_files != set(root_files) or actual_directories != root_directories:
            raise VaultError(f"destination native root is partial or has unexpected members: {root_relative}")
        for relative, item in root_files.items():
            target = root.joinpath(*PurePosixPath(relative).parts)
            trusted_identity = cached_files.get(relative)
            if trusted_identity is not None:
                target_stat = _lstat(target)
                if target_stat is None or _identity_stat(target_stat) != trusted_identity:
                    if _cache_file_is_published(cached_files, relative):
                        raise VaultError(f"destination member changed after publication: {relative}")
                    cached_files.pop(relative, None)
                else:
                    equal[relative] = "equal"
                    continue
            _hash_and_cache_destination(target, item, cached_files)
            equal[relative] = "equal"
    return equal


def _mkdir_restore_directory(
    path: Path,
    mode: int,
    created_paths: list[tuple[Path, tuple[int, int]]] | None = None,
) -> bool:
    del mode  # Restore directories remain private until all files are published.
    ownership = created_paths if created_paths is not None else []
    start = len(ownership)
    storage._mkdir_private(
        path, tighten_existing=False, created_paths=ownership
    )
    return any(created == path for created, _identity in ownership[start:])


def _cleanup_restore_stage(stage: Path, manifest: dict[str, Any]) -> None:
    directories: set[Path] = set()
    for item in manifest["files"]:
        path = stage.joinpath(*PurePosixPath(item["path"]).parts)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        current = path.parent
        while current != stage:
            directories.add(current)
            current = current.parent
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except (FileNotFoundError, OSError):
            pass
    try:
        stage.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _stage_restore_bundle(
    bundle_root: Path, manifest: dict[str, Any], destination_root: Path
) -> tuple[Path, dict[str, Path]]:
    stage_parent = destination_root.parent
    while _lstat(stage_parent) is None and stage_parent.parent != stage_parent:
        stage_parent = stage_parent.parent
    stage_parent = storage._require_directory(stage_parent, label="restore staging parent")
    stage = Path(tempfile.mkdtemp(prefix=".codex-vault-restore-stage-", dir=stage_parent))
    os.chmod(stage, _PRIVATE_DIR)
    try:
        blob = bundle_root.parent / manifest["blob"]
        _total, staged = _stream_verified_blob(blob, manifest, stage_root=stage)
        return stage, staged
    except Exception:
        _cleanup_restore_stage(stage, manifest)
        raise


def _copy_verified_stage(staged: Path, output_fd: int, entry: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    total = 0
    input_fd = -1
    try:
        input_fd = storage._open_nofollow(staged, os.O_RDONLY)
        with os.fdopen(input_fd, "rb") as source, os.fdopen(output_fd, "wb") as output:
            input_fd = -1
            output_fd = -1
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if input_fd >= 0:
            os.close(input_fd)
        if output_fd >= 0:
            os.close(output_fd)
    if digest.hexdigest() != entry["sha256"] or total != entry["size"]:
        raise VaultError("staged restore member changed before publication")


def _cleanup_restore_outputs(
    published_files: list[tuple[Path, tuple[int, int, int, int, int, int]]],
    created_directories: list[dict[str, Any]],
    created_paths: list[tuple[Path, tuple[int, int]]] | None = None,
) -> None:
    candidates = (
        created_paths
        if created_paths is not None
        else [
            (directory["target"], directory["identity"])
            for directory in created_directories
        ]
    )
    owned_directories: list[tuple[Path, tuple[int, int]]] = []
    for target, identity in sorted(candidates, key=lambda item: len(item[0].parts)):
        try:
            fd = _open_restore_directory(target, identity)
            try:
                os.fchmod(fd, _PRIVATE_DIR)
            finally:
                os.close(fd)
        except (VaultError, OSError):
            continue
        owned_directories.append((target, identity))
    for target, identity in reversed(published_files):
        current = _lstat(target)
        if current is not None and stat.S_ISREG(current.st_mode) and not stat.S_ISLNK(current.st_mode) and _identity_stat(current) == identity:
            try:
                os.unlink(target)
            except OSError:
                pass
    for target, identity in sorted(
        owned_directories, key=lambda item: len(item[0].parts), reverse=True
    ):
        current = _lstat(target)
        if current is None or stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            continue
        try:
            os.rmdir(target)
        except OSError:
            pass


def _open_restore_directory(path: Path, identity: tuple[int, int]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise VaultError(f"restored directory changed before metadata update: {path}") from exc
    opened = os.fstat(fd)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
        os.close(fd)
        raise VaultError(f"restored directory identity changed: {path}")
    return fd


def _apply_restore_directory_metadata(directory: dict[str, Any]) -> None:
    target = directory["target"]
    fd = _open_restore_directory(target, directory["identity"])
    try:
        os.fchmod(fd, directory["mode"] & 0o1777)
        os.utime(fd, ns=(directory["mtime_ns"], directory["mtime_ns"]))
    finally:
        os.close(fd)
    current = _lstat(target)
    if current is None or stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != directory["identity"]:
        raise VaultError(f"restored directory changed after metadata update: {directory['relative_path']}")


def restore(vault_dir: Path, archive_id: str, destination_home: Path | None = None, execute: bool = False) -> dict[str, Any]:
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with storage._vault_lock(vault, exclusive=True, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            raise VaultError("bundle namespace does not exist")
        manifest = _resolve(bundle_root, archive_id)
        root = _destination_root(Path(destination_home) if destination_home is not None else Path(manifest["source_root"]), False)
        trusted_files = _RestoreIdentityCache()
        equal = _restore_preflight(root, manifest, trusted_files)
        result = {
            "ok": True,
            "execute": bool(execute),
            "executed": bool(execute),
            "archive_id": manifest["archive_id"],
            "destination_root": str(root),
            "file_count": len(manifest["files"]),
            "restored": [],
            "skipped": list(equal),
        }
        if not execute:
            check = _verify_blob(bundle_root, manifest)
            if not check["ok"]:
                raise VaultError(check["error"])
            return result
        stage, staged = _stage_restore_bundle(bundle_root, manifest, root)
        published_files: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
        created_directories: list[dict[str, Any]] = []
        created_paths: list[tuple[Path, tuple[int, int]]] = []
        try:
            # Extraction can take time. Re-run the complete destination
            # preflight before creating anything so a tool that resumed in
            # the meantime cannot be overlaid with a native session.
            equal = _restore_preflight(root, manifest, trusted_files)
            result["skipped"] = list(equal)
            storage._mkdir_private(
                root, tighten_existing=False, created_paths=created_paths
            )
            observed_unowned_directory = False
            for directory in sorted(manifest["directories"], key=lambda item: len(PurePosixPath(item["path"]).parts)):
                target_directory = root.joinpath(*PurePosixPath(directory["path"]).parts)
                created = _mkdir_restore_directory(
                    target_directory, _PRIVATE_DIR, created_paths
                )
                if created:
                    created_identity = next(
                        identity
                        for created_path, identity in reversed(created_paths)
                        if created_path == target_directory
                    )
                    created_stat = _lstat(target_directory)
                    if (
                        created_stat is None
                        or not stat.S_ISDIR(created_stat.st_mode)
                        or stat.S_ISLNK(created_stat.st_mode)
                        or (created_stat.st_dev, created_stat.st_ino)
                        != created_identity
                    ):
                        raise VaultError(f"restored directory is unsafe: {directory['path']}")
                    created_directories.append({"target": target_directory, "relative_path": directory["path"], "mode": directory["mode"], "mtime_ns": directory["mtime_ns"], "identity": created_identity})
                else:
                    observed_unowned_directory = True
            if observed_unowned_directory:
                equal = _restore_preflight(root, manifest, trusted_files)
                result["skipped"] = list(equal)
            for entry in manifest["files"]:
                if entry["path"] in equal:
                    continue
                target = root.joinpath(*PurePosixPath(entry["path"]).parts)
                storage._mkdir_private(
                    target.parent,
                    tighten_existing=False,
                    created_paths=created_paths,
                )
                fd, name = tempfile.mkstemp(prefix=".bundle-restore-", dir=target.parent)
                temp = Path(name)
                try:
                    _copy_verified_stage(staged[entry["path"]], fd, entry)
                    os.utime(temp, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                    published = storage._publish_temp_no_replace(temp, target, mode=entry["mode"] & 0o1777)
                    if published:
                        published_stat = storage._safe_regular(target, label="restored bundle file")
                        published_files.append((target, _identity_stat(published_stat)))
                        result["restored"].append(entry["path"])
                    else:
                        target_stat = _lstat(target)
                        if target_stat is None or stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
                            raise VaultError(f"destination race produced an unsafe member: {entry['path']}")
                        _hash_and_cache_destination(target, entry, trusted_files)
                        result["skipped"].append(entry["path"])
                    storage._fsync_directory(target.parent)
                except Exception:
                    temp.unlink(missing_ok=True)
                    raise
            for target, identity in published_files:
                relative = target.relative_to(root).as_posix()
                trusted_files[relative] = identity
                trusted_files.published_paths.add(relative)
            _restore_preflight(root, manifest, trusted_files)
            for directory in sorted(created_directories, key=lambda item: len(PurePosixPath(item["relative_path"]).parts), reverse=True):
                _apply_restore_directory_metadata(directory)
        except Exception:
            _cleanup_restore_outputs(
                published_files, created_directories, created_paths
            )
            raise
        finally:
            _cleanup_restore_stage(stage, manifest)
    return result


def _group_root_paths(root: Path, manifest: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for relative in manifest["root_entries"]:
        path = root.joinpath(*PurePosixPath(relative).parts)
        _check_components(path)
        st = _lstat(path)
        if st is None or stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise VaultError(f"bundle root entry is unsafe: {relative}")
        paths.append(path)
    return paths


def _current_source_layout(root: Path, manifest: dict[str, Any]) -> tuple[set[str], set[str]]:
    expected_files = {item["path"] for item in manifest["files"]}
    expected_directories = {item["path"] for item in manifest["directories"]}
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for root_relative in manifest["root_entries"]:
        selected_root = root.joinpath(*PurePosixPath(root_relative).parts)
        selected_stat = _lstat(selected_root)
        if selected_stat is None or stat.S_ISLNK(selected_stat.st_mode):
            raise VaultError(f"source changed since archive: {root_relative}")
        if stat.S_ISREG(selected_stat.st_mode):
            actual_files.add(root_relative)
            continue
        if not stat.S_ISDIR(selected_stat.st_mode):
            raise VaultError(f"source changed since archive: {root_relative}")
        stack = [selected_root]
        while stack:
            current = stack.pop()
            current_stat = _lstat(current)
            if current_stat is None or stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise VaultError("source changed since archive: unsafe directory member")
            current_relative = current.relative_to(root).as_posix()
            actual_directories.add(current_relative)
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name)
            except OSError as exc:
                raise VaultError(f"source changed since archive: unreadable directory {current_relative}") from exc
            for entry in entries:
                entry_path = Path(entry.path)
                entry_relative = entry_path.relative_to(root).as_posix()
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise VaultError(f"source changed since archive: unreadable member {entry_relative}") from exc
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise VaultError(f"source changed since archive: symlink member {entry_relative}")
                if stat.S_ISDIR(entry_stat.st_mode):
                    stack.append(entry_path)
                elif stat.S_ISREG(entry_stat.st_mode):
                    actual_files.add(entry_relative)
                else:
                    raise VaultError(f"source changed since archive: non-regular member {entry_relative}")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise VaultError("source changed since archive: member set differs")
    return actual_files, actual_directories


def _verify_current_source_metadata(
    manifest: dict[str, Any],
) -> tuple[Path, list[Path], list[os.stat_result]]:
    root = storage._require_directory(Path(manifest["source_root"]), label="bundle source root")
    _current_source_layout(root, manifest)
    current_stats: list[os.stat_result] = []
    for item in manifest["files"]:
        path = root.joinpath(*PurePosixPath(item["path"]).parts)
        current = storage._safe_regular(path, label="bundle source file")
        if (
            current.st_size != item["size"]
            or stat.S_IMODE(current.st_mode) != item["mode"]
            or current.st_mtime_ns != item["mtime_ns"]
        ):
            raise VaultError(f"source changed since archive: {item['path']}")
        current_stats.append(current)
    for item in manifest["directories"]:
        path = root.joinpath(*PurePosixPath(item["path"]).parts)
        current = _lstat(path)
        if (
            current is None
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or stat.S_IMODE(current.st_mode) != item["mode"]
            or current.st_mtime_ns != item["mtime_ns"]
        ):
            raise VaultError(f"source changed since archive: {item['path']}")
        # Directory mtimes are part of the source group's snapshot. Keep them
        # in the same age barrier as files so a newly-created empty or nested
        # companion cannot be pruned immediately after archiving.
        current_stats.append(current)
    return root, _group_root_paths(root, manifest), current_stats


def _verify_current_source(
    manifest: dict[str, Any],
) -> tuple[Path, list[Path], list[os.stat_result]]:
    root, root_paths, _metadata_stats = _verify_current_source_metadata(manifest)
    current_stats: list[os.stat_result] = []
    for item in manifest["files"]:
        path = root.joinpath(*PurePosixPath(item["path"]).parts)
        digest, size, before = storage._hash_path(path)
        after = storage._safe_regular(path, label="bundle source file")
        if _identity_stat(before) != _identity_stat(after):
            raise VaultError(f"source changed while checking prune candidate: {item['path']}")
        if (
            digest != item["sha256"]
            or size != item["size"]
            or stat.S_IMODE(after.st_mode) != item["mode"]
            or after.st_mtime_ns != item["mtime_ns"]
        ):
            raise VaultError(f"source changed since archive: {item['path']}")
        current_stats.append(after)
    for item in manifest["directories"]:
        path = root.joinpath(*PurePosixPath(item["path"]).parts)
        current = _lstat(path)
        if (
            current is None
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or stat.S_IMODE(current.st_mode) != item["mode"]
            or current.st_mtime_ns != item["mtime_ns"]
        ):
            raise VaultError(f"source changed since archive: {item['path']}")
        current_stats.append(current)
    return root, root_paths, current_stats


def _activity_for_paths(paths: list[Path]) -> tuple[str, str]:
    states: list[tuple[str, str]] = []
    for path in paths:
        path_stat = _lstat(path)
        if path_stat is None or stat.S_ISLNK(path_stat.st_mode):
            return "unknown", "selected session path is missing or unsafe"
        probe = (
            getattr(activity, "probe_session_tree", None)
            if stat.S_ISDIR(path_stat.st_mode)
            else getattr(activity, "probe_session_file", None)
            if stat.S_ISREG(path_stat.st_mode)
            else None
        )
        if probe is None:
            return "unknown", "session activity probe unavailable"
        try:
            result = probe(path)
        except Exception:
            return "unknown", "session activity probe failed"
        state = result.get("state") if isinstance(result, dict) else "unknown"
        states.append((state if state in {"active", "inactive", "unknown"} else "unknown", str(result.get("reason", "")) if isinstance(result, dict) else "invalid activity result"))
    if any(state == "active" for state, _ in states):
        return "active", next(reason for state, reason in states if state == "active")
    if any(state == "unknown" for state, _ in states):
        return "unknown", next(reason for state, reason in states if state == "unknown")
    return "inactive", "all selected group files are inactive"


def _staged_path(quarantine: Path, relative: str, roots: list[str]) -> Path:
    owners = [root for root in roots if relative == root or relative.startswith(root + "/")]
    if not owners:
        raise VaultError(f"bundle member is outside root entries: {relative}")
    owner = max(owners, key=len)
    suffix = relative[len(owner):].lstrip("/")
    path = quarantine / Path(owner).name
    if suffix:
        path = path.joinpath(*PurePosixPath(suffix).parts)
    return path


def _quarantine_layout(quarantine: Path, manifest: dict[str, Any]) -> tuple[set[str], set[str]]:
    roots = manifest["root_entries"]
    expected_files = {item["path"] for item in manifest["files"]}
    expected_directories = {item["path"] for item in manifest["directories"]}
    expected_root_names = {Path(root).name for root in roots}
    try:
        actual_root_names = {entry.name for entry in os.scandir(quarantine)}
    except OSError as exc:
        raise VaultError("quarantine root cannot be read") from exc
    if actual_root_names != expected_root_names:
        raise VaultError("quarantine root member set differs from manifest")
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for root_relative in roots:
        staged_root = quarantine / Path(root_relative).name
        st = _lstat(staged_root)
        if st is None or stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise VaultError(f"quarantine root entry is unsafe: {root_relative}")
        if stat.S_ISREG(st.st_mode):
            actual_files.add(root_relative)
            continue
        stack = [staged_root]
        while stack:
            current = stack.pop()
            current_st = _lstat(current)
            if current_st is None or stat.S_ISLNK(current_st.st_mode):
                raise VaultError("quarantine contains a symlink or missing member")
            relative_suffix = current.relative_to(staged_root).as_posix()
            source_relative = root_relative if relative_suffix == "." else f"{root_relative}/{relative_suffix}"
            if stat.S_ISDIR(current_st.st_mode):
                actual_directories.add(source_relative)
                try:
                    entries = sorted(os.scandir(current), key=lambda item: item.name)
                except OSError as exc:
                    raise VaultError("quarantine directory cannot be read") from exc
                for entry in entries:
                    entry_path = Path(entry.path)
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(entry_stat.st_mode):
                        raise VaultError("quarantine contains a symlink")
                    if stat.S_ISDIR(entry_stat.st_mode):
                        stack.append(entry_path)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        entry_suffix = entry_path.relative_to(staged_root).as_posix()
                        actual_files.add(f"{root_relative}/{entry_suffix}")
                    else:
                        raise VaultError("quarantine contains a non-regular member")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise VaultError("quarantine member set differs from manifest")
    return actual_files, actual_directories


def _verify_quarantine_members(
    quarantine: Path, manifest: dict[str, Any]
) -> dict[str, tuple[int, int, int, int, int, int]]:
    _quarantine_layout(quarantine, manifest)
    roots = manifest["root_entries"]
    identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    for item in manifest["files"]:
        staged = _staged_path(quarantine, item["path"], roots)
        st = storage._safe_regular(staged, label="quarantined bundle file")
        digest, size, hashed_stat = storage._hash_path(staged)
        after = storage._safe_regular(staged, label="quarantined bundle file")
        if _identity_stat(after) != _identity_stat(hashed_stat):
            raise VaultError("quarantined source changed while being verified")
        if digest != item["sha256"] or size != item["size"] or _identity_stat(st)[5] != item["mode"] or st.st_mtime_ns != item["mtime_ns"]:
            raise VaultError("quarantined source changed before removal")
        identities[item["path"]] = _identity_stat(after)
    for item in manifest["directories"]:
        staged = _staged_path(quarantine, item["path"], roots)
        st = _lstat(staged)
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise VaultError("quarantined directory changed before removal")
        if stat.S_IMODE(st.st_mode) != item["mode"] or st.st_mtime_ns != item["mtime_ns"]:
            raise VaultError("quarantined directory metadata changed before removal")
        identities[item["path"]] = _identity_stat(st)
    return identities


def _rename_directory_no_replace(source: Path, target: Path) -> bool:
    """Atomically rename one directory without replacing a raced target."""

    libc = ctypes.CDLL(None, use_errno=True)
    result: int
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(os.fsencode(source), os.fsencode(target), 0x00000004)  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
        else:
            return False
    except AttributeError:
        return False
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    raise OSError(error, os.strerror(error), str(target))


def _move_no_replace(source: Path, target: Path) -> bool:
    source_stat = _lstat(source)
    if source_stat is None or stat.S_ISLNK(source_stat.st_mode):
        raise VaultError(f"quarantine recovery source is unsafe: {source}")
    if stat.S_ISDIR(source_stat.st_mode):
        moved = _rename_directory_no_replace(source, target)
        if not moved:
            return False
        target_stat = _lstat(target)
        if (
            target_stat is None
            or stat.S_ISLNK(target_stat.st_mode)
            or not stat.S_ISDIR(target_stat.st_mode)
            or (target_stat.st_dev, target_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino)
        ):
            raise VaultError("quarantine recovery directory identity differs")
        return True
    if not stat.S_ISREG(source_stat.st_mode):
        raise VaultError(f"quarantine recovery source is not regular: {source}")
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            return False
        raise
    target_stat = _lstat(target)
    if (
        target_stat is None
        or stat.S_ISLNK(target_stat.st_mode)
        or not stat.S_ISREG(target_stat.st_mode)
        or (target_stat.st_dev, target_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino)
    ):
        raise VaultError("quarantine recovery link identity differs")
    os.unlink(source)
    return True


def _restore_quarantine(moved: list[tuple[Path, Path]], quarantine: Path, result: dict[str, Any], reason: str) -> bool:
    skipped = {"archive_id": result.get("archive_id", ""), "activity": "unknown", "reason": reason}
    # If any original root has reappeared, keep every moved root together so a
    # newly-created path is never overwritten and rollback is never partial.
    if any(_lstat(original) is not None for original, _ in moved):
        skipped["quarantine"] = str(quarantine)
        result["skipped"].append(skipped)
        return False
    restored: list[tuple[Path, Path]] = []
    try:
        def recovery_order(pair: tuple[Path, Path]) -> int:
            staged_stat = _lstat(pair[1])
            return 0 if staged_stat is not None and stat.S_ISDIR(staged_stat.st_mode) else 1

        ordered = sorted(moved, key=recovery_order)
        for original, staged in ordered:
            if _lstat(staged) is not None:
                if not _move_no_replace(staged, original):
                    raise FileExistsError(errno.EEXIST, "source path reappeared during rollback", str(original))
                restored.append((original, staged))
    except (OSError, VaultError):
        # A multi-root rollback can fail halfway. Move already-restored roots
        # back into quarantine where possible so the recovery state remains
        # grouped and no later root is silently abandoned.
        for original, staged in reversed(restored):
            try:
                if _lstat(original) is not None and _lstat(staged) is None:
                    _move_no_replace(original, staged)
            except (OSError, VaultError):
                pass
        skipped["quarantine"] = str(quarantine)
        result["skipped"].append(skipped)
        return False
    try:
        quarantine.rmdir()
    except OSError:
        # An unexpected quarantine member is preserved, while all selected
        # roots stay safely restored at their original names.
        skipped["quarantine"] = str(quarantine)
    if moved:
        storage._fsync_directory(moved[0][0].parent)
    result["skipped"].append(skipped)
    return True


def _remove_quarantine_exact(quarantine: Path, manifest: dict[str, Any]) -> None:
    identities = _verify_quarantine_members(quarantine, manifest)
    roots = manifest["root_entries"]
    # Recheck every member after content verification and before the first
    # unlink. This closes the gap where one file changes while another is
    # being hashed.
    for item in manifest["files"]:
        staged = _staged_path(quarantine, item["path"], roots)
        st = storage._safe_regular(staged, label="quarantined bundle file")
        if _identity_stat(st) != identities[item["path"]]:
            raise VaultError("quarantined source changed after verification")
    for item in manifest["directories"]:
        staged = _staged_path(quarantine, item["path"], roots)
        st = _lstat(staged)
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or _identity_stat(st) != identities[item["path"]]:
            raise VaultError("quarantined directory changed after verification")
    # The caller checks original roots immediately before this operation.
    deletion_started = False
    try:
        for item in sorted(manifest["files"], key=lambda entry: len(PurePosixPath(entry["path"]).parts), reverse=True):
            staged = _staged_path(quarantine, item["path"], roots)
            st = storage._safe_regular(staged, label="quarantined bundle file")
            if _identity_stat(st) != identities[item["path"]]:
                raise VaultError("quarantined file changed before unlink")
            os.unlink(staged)
            deletion_started = True
        for item in sorted(manifest["directories"], key=lambda entry: len(PurePosixPath(entry["path"]).parts), reverse=True):
            staged = _staged_path(quarantine, item["path"], roots)
            st = _lstat(staged)
            identity = identities[item["path"]]
            if (
                st is None
                or stat.S_ISLNK(st.st_mode)
                or not stat.S_ISDIR(st.st_mode)
                or (st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode)) != (identity[0], identity[1], identity[5])
            ):
                raise VaultError("quarantined directory changed before removal")
            os.rmdir(staged)
        quarantine.rmdir()
    except (OSError, VaultError) as exc:
        if deletion_started:
            raise _QuarantineRemovalStarted(
                f"bundle prune stopped after deletion began; quarantine retained for recovery: {quarantine}"
            ) from exc
        raise


def _prune_one(vault: Path, bundle_root: Path, manifest: dict[str, Any], result: dict[str, Any]) -> None:
    try:
        root, root_paths, current_stats = _verify_current_source(manifest)
    except VaultError as exc:
        result["skipped"].append({"archive_id": manifest["archive_id"], "reason": str(exc)})
        return
    if any(time.time_ns() - item.st_mtime_ns < _HOUR_NS for item in current_stats):
        result["skipped"].append({"archive_id": manifest["archive_id"], "reason": "source is less than one hour old"})
        return
    activity_state, activity_reason = _activity_for_paths(root_paths)
    if activity_state != "inactive":
        result["skipped"].append({"archive_id": manifest["archive_id"], "activity": activity_state, "reason": activity_reason})
        return
    parent_dirs = {path.parent for path in root_paths}
    if len(parent_dirs) != 1:
        result["skipped"].append({"archive_id": manifest["archive_id"], "reason": "group roots do not share a parent"})
        return
    parent = next(iter(parent_dirs))
    quarantine = parent / f".codex-vault-pending-bundle-{uuid.uuid4().hex}"
    try:
        parent_before = _lstat(parent)
        if parent_before is None:
            raise VaultError("bundle root parent disappeared")
        root_stats = {relative: _lstat(root.joinpath(*PurePosixPath(relative).parts)) for relative in manifest["root_entries"]}
        if any(stat_item is None for stat_item in root_stats.values()):
            raise VaultError("bundle root entry disappeared before quarantine")
        for left in manifest["root_entries"]:
            for right in manifest["root_entries"]:
                if left != right and right.startswith(left + "/"):
                    raise VaultError("bundle root entries overlap")
        storage._mkdir_private(quarantine)
    except (VaultError, OSError) as exc:
        skipped = {
            "archive_id": manifest["archive_id"],
            "reason": f"bundle prune setup failed: {exc}",
        }
        # The unique path may have been created before setup failed. Preserve
        # anything at that path because ownership and contents are uncertain.
        try:
            quarantine_stat = _lstat(quarantine)
        except OSError:
            quarantine_stat = None
            skipped["quarantine"] = str(quarantine)
        if quarantine_stat is not None:
            skipped["quarantine"] = str(quarantine)
        result["skipped"].append(skipped)
        return
    moved: list[tuple[Path, Path]] = []
    deletion_completed = False
    try:
        for relative in manifest["root_entries"]:
            original = root.joinpath(*PurePosixPath(relative).parts)
            staged = quarantine / original.name
            os.rename(original, staged)
            moved.append((original, staged))
        storage._fsync_directory(parent)
        parent_after = _lstat(parent)
        if parent_after is None or (parent_after.st_dev, parent_after.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            raise VaultError("bundle root parent changed during quarantine")
        for relative, before in root_stats.items():
            staged = quarantine / Path(relative).name
            after = _lstat(staged)
            if after is None or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise VaultError("bundle root entry identity changed during quarantine")
        state, reason = _activity_for_paths([quarantine])
        if state != "inactive":
            result["archive_id"] = manifest["archive_id"]
            _restore_quarantine(moved, quarantine, result, reason)
            return
        if any(_lstat(original) is not None for original, _ in moved):
            result["archive_id"] = manifest["archive_id"]
            result["skipped"].append({"archive_id": manifest["archive_id"], "activity": "unknown", "reason": "new source root path appeared during quarantine", "quarantine": str(quarantine)})
            return
        _remove_quarantine_exact(quarantine, manifest)
        deletion_completed = True
        storage._fsync_directory(parent)
    except _QuarantineRemovalStarted as exc:
        result["skipped"].append(
            {
                "archive_id": manifest["archive_id"],
                "activity": "unknown",
                "reason": str(exc),
                "quarantine": str(quarantine),
            }
        )
        raise
    except (VaultError, OSError) as exc:
        if deletion_completed:
            result["removed"].append(manifest["archive_id"])
            result["removed_source_bytes"] += manifest["raw_bytes"]
            raise VaultError(
                f"bundle source was removed but parent directory durability is uncertain: {parent}"
            ) from exc
        result["archive_id"] = manifest["archive_id"]
        _restore_quarantine(moved, quarantine, result, "bundle prune failed; quarantine retained for recovery")
        return
    result["removed"].append(manifest["archive_id"])
    result["removed_source_bytes"] += manifest["raw_bytes"]


def prune(vault_dir: Path, archive_id: str | None = None, execute: bool = False, tool: str | None = None) -> dict[str, Any]:
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with storage._vault_lock(vault, exclusive=True, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            raise VaultError("bundle namespace does not exist")
        manifests = [_resolve(bundle_root, archive_id)] if archive_id else [item for item in _iter_manifests(bundle_root) if tool is None or item["tool"] == tool]
        candidates: list[dict[str, Any]] = []
        for manifest in manifests:
            try:
                _root, _root_paths, current_stats = _verify_current_source_metadata(manifest)
                recent = any(time.time_ns() - item.st_mtime_ns < _HOUR_NS for item in current_stats)
                if recent:
                    candidates.append({"archive_id": manifest["archive_id"], "eligible": False, "activity": "inactive", "reason": "source is less than one hour old"})
                    continue
            except VaultError as exc:
                candidates.append({"archive_id": manifest["archive_id"], "eligible": False, "reason": str(exc)})
                continue
            check = _verify_blob(bundle_root, manifest)
            if not check["ok"]:
                candidates.append({"archive_id": manifest["archive_id"], "eligible": False, "reason": check["error"]})
                continue
            try:
                _root, root_paths, current_stats = _verify_current_source(manifest)
                recent = any(time.time_ns() - item.st_mtime_ns < _HOUR_NS for item in current_stats)
                if recent:
                    candidates.append({"archive_id": manifest["archive_id"], "eligible": False, "activity": "inactive", "reason": "source is less than one hour old"})
                    continue
                state, reason = _activity_for_paths(root_paths)
                eligible = state == "inactive"
                candidates.append({"archive_id": manifest["archive_id"], "eligible": eligible, "activity": state, "reason": "eligible" if eligible else reason})
            except VaultError as exc:
                candidates.append({"archive_id": manifest["archive_id"], "eligible": False, "reason": str(exc)})
        result: dict[str, Any] = {"ok": True, "execute": bool(execute), "executed": bool(execute), "candidates": candidates, "removed": [], "skipped": [item for item in candidates if not item["eligible"]], "removed_source_bytes": 0}
        if not execute:
            return result
        by_id = {manifest["archive_id"]: manifest for manifest in manifests}
        for candidate in candidates:
            if candidate["eligible"]:
                _prune_one(vault, bundle_root, by_id[candidate["archive_id"]], result)
        return result


def _snippet(text: str, query: str, limit: int = 240, *, folded_query: str | None = None) -> str:
    span = _casefold_span(text, query, folded_query=folded_query)
    if span is None:
        return text[:limit]
    start = max(0, span[0] - limit // 3)
    return ("…" if start else "") + text[start : start + limit]


def search(vault_dir: Path, query: str, limit: int = 20, project: str | None = None, session_id: str | None = None, tool: str | None = None) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise VaultError("query must not be blank")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise VaultError("limit must be greater than zero")
    folded_query = query.casefold()
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        return {"query": query, "matches": [], "scanned_archives": 0, "truncated": False, "skipped_records": 0}
    matches: list[dict[str, Any]] = []
    skipped_records = 0
    total_matches = 0
    selected_archives = 0
    with storage._vault_lock(vault, exclusive=False, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            return {"query": query, "matches": [], "scanned_archives": 0, "truncated": False, "skipped_records": 0}
        manifests = [item for item in _iter_manifests(bundle_root) if tool is None or item["tool"] == tool]
        for manifest in manifests:
            if project and project.casefold() not in manifest["cwd"].casefold():
                continue
            if session_id and session_id != manifest["session_id"] and not manifest["archive_id"].startswith(session_id):
                continue
            selected_archives += 1
            archive_matches: list[dict[str, Any]] = []
            archive_skipped = 0

            def collect(entry: dict[str, Any], record: dict[str, Any]) -> None:
                nonlocal archive_skipped, total_matches
                if record.get("skipped"):
                    archive_skipped += 1
                    return
                text = str(record.get("text", ""))
                if folded_query not in text.casefold():
                    return
                total_matches += 1
                if len(matches) + len(archive_matches) < limit:
                    archive_matches.append({"archive_id": manifest["archive_id"], "tool": manifest["tool"], "session_id": manifest["session_id"], "cwd": manifest["cwd"], "member_path": entry["path"], "line": record.get("line"), "kind": record.get("kind", "text"), "snippet": _snippet(text, query, folded_query=folded_query)})

            blob = bundle_root.parent / manifest["blob"]
            _stream_verified_blob(blob, manifest, record_callback=collect)
            matches.extend(archive_matches)
            skipped_records += archive_skipped
    return {"query": query, "matches": matches, "scanned_archives": selected_archives, "truncated": total_matches > limit, "skipped_records": skipped_records}


def show(vault_dir: Path, archive_id: str, limit: int = 100, query: str | None = None) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise VaultError("limit must be greater than zero")
    folded_query = query.casefold() if query is not None else None
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        raise VaultError("vault does not exist")
    with storage._vault_lock(vault, exclusive=False, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            raise VaultError("bundle namespace does not exist")
        manifest = _resolve(bundle_root, archive_id)
        records: list[dict[str, Any]] = []
        skipped = 0
        matching_records = 0

        def collect(entry: dict[str, Any], record: dict[str, Any]) -> None:
            nonlocal skipped, matching_records
            if record.get("skipped"):
                skipped += 1
                return
            if folded_query is not None and folded_query not in str(record.get("text", "")).casefold():
                return
            matching_records += 1
            if len(records) < limit:
                records.append({**record, "member_path": entry["path"], "text": str(record.get("text", ""))[:240]})

        blob = bundle_root.parent / manifest["blob"]
        _stream_verified_blob(blob, manifest, record_callback=collect)
    return {"archive_id": manifest["archive_id"], "records": records, "skipped_records": skipped, "truncated": matching_records > limit}


def stats(vault_dir: Path, tool: str | None = None) -> dict[str, Any]:
    empty = {
        "bundle_archives": 0,
        "bundle_blobs": 0,
        "bundle_manifest_bytes": 0,
        "bundle_blob_bytes": 0,
        "bundle_vault_bytes": 0,
        "bundle_raw_bytes": 0,
        "bundle_source_bytes_retained": 0,
        "bundle_net_storage_bytes": 0,
        "bundle_allocated_bytes": 0,
        "bundle_temporary_files": 0,
        "bundle_temporary_bytes": 0,
        "bundle_temporary_allocated_bytes": 0,
    }
    vault = storage._safe_vault(vault_dir, create=False)
    if vault is None:
        return empty
    with storage._vault_lock(vault, exclusive=False, create=False):
        bundle_root = _bundle_root(vault, create=False)
        if bundle_root is None:
            return empty
        manifests = [item for item in _iter_manifests(bundle_root) if tool is None or item['tool'] == tool]
        manifest_stats = [storage._safe_regular(_manifest_path(bundle_root, item['archive_id']),
                                               label="bundle manifest") for item in manifests]
        blob_stats: list[os.stat_result] = []
        if tool is not None:
            # Shared blobs count once per selected view. Unreferenced blobs
            # belong only to the unfiltered storage total.
            for relative in sorted({item['blob'] for item in manifests}):
                blob_stats.append(storage._safe_regular(vault / relative, label="bundle blob"))
        else:
            for entry in os.scandir(bundle_root / "blobs"):
                if entry.name.startswith(".bundle-") or not entry.name.endswith(".tar.gz"):
                    continue
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
                    blob_stats.append(entry_stat)
        temporary_files = 0
        temporary_bytes = 0
        temporary_allocated_bytes = 0
        if tool is None:
            blob_temporary = _temporary_file_stats(bundle_root / "blobs", ".bundle-")
            manifest_temporary = _temporary_file_stats(bundle_root / "manifests", ".bundle-manifest-")
            temporary_files = blob_temporary[0] + manifest_temporary[0]
            temporary_bytes = blob_temporary[1] + manifest_temporary[1]
            temporary_allocated_bytes = blob_temporary[2] + manifest_temporary[2]
        manifest_bytes = sum(item.st_size for item in manifest_stats)
        blob_bytes = sum(item.st_size for item in blob_stats)
        vault_bytes = manifest_bytes + blob_bytes + temporary_bytes
        retained_source_bytes = 0
        retained_paths: set[tuple[str, str]] = set()
        for manifest in manifests:
            source_root = Path(manifest["source_root"])
            for item in manifest["files"]:
                source_key = (manifest["source_root"], item["path"])
                if source_key in retained_paths:
                    continue
                retained_paths.add(source_key)
                source = source_root.joinpath(*PurePosixPath(item["path"]).parts)
                try:
                    _check_components(source)
                    source_stat = _lstat(source)
                except (VaultError, OSError):
                    continue
                if source_stat is not None and stat.S_ISREG(source_stat.st_mode) and not stat.S_ISLNK(source_stat.st_mode):
                    retained_source_bytes += int(source_stat.st_size)
        allocated = sum(getattr(item, "st_blocks", 0) * 512 for item in manifest_stats + blob_stats)
        return {
            "bundle_archives": len(manifests),
            "bundle_blobs": len(blob_stats),
            "bundle_manifest_bytes": manifest_bytes,
            "bundle_blob_bytes": blob_bytes,
            "bundle_vault_bytes": vault_bytes,
            "bundle_raw_bytes": sum(item["raw_bytes"] for item in manifests),
            "bundle_source_bytes_retained": retained_source_bytes,
            "bundle_net_storage_bytes": vault_bytes + retained_source_bytes,
            "bundle_allocated_bytes": allocated + temporary_allocated_bytes,
            "bundle_temporary_files": temporary_files,
            "bundle_temporary_bytes": temporary_bytes,
            "bundle_temporary_allocated_bytes": temporary_allocated_bytes,
        }


__all__ = ["VaultError", "archive", "list_archives", "verify", "restore", "prune", "search", "show", "scan", "stats"]
