"""Streaming, literal content search for compressed Codex session archives."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Iterator

from .storage import VaultError, iter_archive_lines as _storage_iter_archive_lines, list_archives, resolve_archive


MAX_RECORD_BYTES = 1024 * 1024
MAX_SNIPPET_CHARS = 240
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe_text(value: object) -> str:
    if isinstance(value, str):
        text = value
    elif value is None:
        return ""
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    # Newlines and other controls are not useful in a one-line result and can
    # otherwise be used to forge terminal output.
    return _CONTROL_CHARS.sub(" ", text).strip()


def _readable_value(value: object) -> str:
    """Extract text blocks while ignoring image and binary payloads."""
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return " ".join(part for part in (_readable_value(item) for item in value) if part)
    if isinstance(value, dict):
        value_type = value.get("type")
        if isinstance(value_type, str) and (value_type.startswith("image") or value_type in {"input_image", "output_image"}):
            return ""
        for field in ("text", "message", "summary", "content", "output"):
            if field in value:
                text = _readable_value(value[field])
                if text:
                    return text
        return ""
    return ""


def _payload(record: dict) -> dict:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else record


def _readable_record(record: object) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    record_type = record.get("type")
    payload = _payload(record)
    payload_type = payload.get("type")
    pieces: list[str] = []

    if record_type == "response_item":
        if payload_type == "message":
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") in {"input_text", "output_text"}:
                        text = _safe_text(item.get("text"))
                        if text:
                            pieces.append(text)
            kind = "message"
        elif payload_type == "function_call":
            name = _safe_text(payload.get("name"))
            arguments = _safe_text(payload.get("arguments"))
            pieces = [part for part in (name, arguments) if part]
            kind = "function_call"
        elif payload_type == "function_call_output":
            text = _readable_value(payload.get("output"))
            pieces = [text] if text else []
            kind = "function_call_output"
        elif payload_type == "custom_tool_call":
            for field in ("input", "output"):
                text = _readable_value(payload.get(field))
                if text:
                    pieces.append(text)
            kind = "custom_tool_call"
        elif payload_type == "custom_tool_call_output":
            text = _readable_value(payload.get("output"))
            pieces = [text] if text else []
            kind = "custom_tool_call_output"
        else:
            return None
    elif record_type == "event_msg":
        if payload_type in {"user_message", "agent_message"}:
            kind = payload_type
            for field in ("message", "text", "content"):
                if field in payload:
                    text = _readable_value(payload.get(field))
                    if text:
                        pieces.append(text)
                        break
        elif payload_type in {"compacted", "compaction", "summary", "context_compacted", "compacted_summary"}:
            kind = "compacted_summary"
            for field in ("summary", "message", "text", "content"):
                if field in payload:
                    text = _readable_value(payload.get(field))
                    if text:
                        pieces.append(text)
                        break
        else:
            return None
    elif record_type in {"compacted", "compaction"}:
        kind = "compacted_summary"
        for field in ("summary", "message", "text", "content"):
            if field in payload:
                text = _readable_value(payload.get(field))
                if text:
                    pieces.append(text)
                    break
    else:
        return None

    text = " ".join(piece for piece in pieces if piece)
    return (kind, text) if text else None


def _validate_manifest(manifest: dict) -> tuple[str, str]:
    archive_id = manifest.get("archive_id")
    blob = manifest.get("blob")
    if not isinstance(archive_id, str) or not archive_id:
        raise VaultError("archive manifest has an invalid archive id")
    if not isinstance(blob, str) or not blob:
        raise VaultError(f"archive {archive_id} has an invalid blob reference")
    relative = Path(blob)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("blobs",):
        raise VaultError(f"archive {archive_id} has an unsafe blob reference")
    if relative.suffixes[-2:] != [".jsonl", ".gz"]:
        raise VaultError(f"archive {archive_id} has an invalid blob reference")
    return archive_id, blob


def _iter_archive_lines(vault_dir: Path, manifest: dict) -> Iterator[tuple[int, bytes] | tuple[int, None]]:
    """Use storage's path-safe, bounded stream helper."""
    try:
        lines = _storage_iter_archive_lines(vault_dir, manifest, max_record_bytes=MAX_RECORD_BYTES)
    except TypeError as error:
        if "max_record_bytes" in str(error):
            raise VaultError("storage stream helper does not enforce record bounds") from error
        raise
    line_number = 0
    for item in lines:
        if isinstance(item, tuple) and len(item) == 2:
            yield item[0], item[1]
        elif isinstance(item, bytes):
            line_number += 1
            if len(item) > MAX_RECORD_BYTES:
                yield line_number, None
            else:
                yield line_number, item
        else:
            raise VaultError("storage returned an invalid archive line")


def _iter_readable(vault_dir: Path, manifest: dict) -> Iterator[tuple[int, str, str] | tuple[int, None, None]]:
    archive_id, _ = _validate_manifest(manifest)
    try:
        for line_number, raw_line in _iter_archive_lines(vault_dir, manifest):
            if raw_line is None:
                yield line_number, None, None
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as error:
                raise VaultError(f"archive {archive_id} contains invalid UTF-8 at line {line_number}") from error
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise VaultError(f"archive {archive_id} contains invalid JSON at line {line_number}") from error
            except RecursionError as error:
                raise VaultError(f"archive {archive_id} contains deeply nested JSON at line {line_number}") from error
            try:
                readable = _readable_record(record)
            except RecursionError as error:
                raise VaultError(f"archive {archive_id} contains deeply nested JSON at line {line_number}") from error
            if readable is not None:
                yield line_number, readable[0], readable[1]
            else:
                yield line_number, None, None
    except VaultError as error:
        # The storage helper deliberately hides low-level details behind a
        # VaultError. Normalize its blob-read diagnostic so callers receive a
        # stable, transcript-free search error.
        storage_message = str(error).casefold()
        if "zstandard" in storage_message:
            raise
        if ("cannot read archive blob" in storage_message or "gzip" in storage_message
                or "corrupt" in storage_message or "truncated" in storage_message):
            raise VaultError(f"archive {archive_id} has corrupt gzip data") from error
        raise
    except (gzip.BadGzipFile, EOFError, OSError, ValueError) as error:
        raise VaultError(f"archive {archive_id} has corrupt gzip data") from error


def _casefold_span(text: str, query: str) -> tuple[int, int] | None:
    """Return the original-text span of a literal case-insensitive match.

    Unicode casefolding can expand one character into several characters (for
    example, ``ß`` becomes ``ss``).  The folded text is searched as one string;
    the matching offsets are then mapped back with a single pass over the
    original text, avoiding per-character offset lists.
    """
    folded_query = query.casefold()
    position = text.casefold().find(folded_query)
    if position < 0:
        return None
    if not folded_query:
        return (0, 1 if text else 0)

    end_position = position + len(folded_query)
    folded_offset = 0
    start: int | None = None
    end: int | None = None
    for index, character in enumerate(text):
        next_offset = folded_offset + len(character.casefold())
        if start is None and folded_offset <= position < next_offset:
            start = index
        if end is None and next_offset >= end_position:
            end = index + 1
        folded_offset = next_offset
        if start is not None and end is not None:
            break

    # A non-empty folded query found in the folded text must map to at least
    # one original character.  Keep a defensive fallback for unusual future
    # Unicode casefold mappings that might not advance an offset.
    if start is None:
        return None
    return start, end if end is not None else len(text)


def _match_context(text: str, query: str) -> str:
    span = _casefold_span(text, query)
    if span is None:
        return ""
    start, end = span
    end = min(len(text), max(start + 1, end))
    context = max(0, (MAX_SNIPPET_CHARS - (end - start)) // 2)
    window_start = max(0, start - context)
    window_end = min(len(text), window_start + MAX_SNIPPET_CHARS)
    window_start = max(0, window_end - MAX_SNIPPET_CHARS)
    snippet = text[window_start:window_end]
    if window_start:
        snippet = "…" + snippet
    if window_end < len(text):
        snippet += "…"
    return snippet[:MAX_SNIPPET_CHARS]


def _manifests(vault_dir: Path) -> list[dict]:
    try:
        manifests = list_archives(Path(vault_dir))
    except VaultError:
        raise
    except Exception as error:
        raise VaultError("unable to list archive manifests") from error
    if not isinstance(manifests, list):
        raise VaultError("archive listing returned an invalid result")
    return manifests


def _selected(manifest: dict, project: str | None, session_id: str | None) -> bool:
    if project is not None and project.casefold() not in str(manifest.get("cwd", "")).casefold():
        return False
    if session_id is not None:
        actual = str(manifest.get("session_id", ""))
        archive_id = str(manifest.get("archive_id", ""))
        if actual != session_id and not archive_id.startswith(session_id):
            return False
    return True


def search(
    vault_dir: Path,
    query: str,
    limit: int = 20,
    project: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Search readable records in gzip archives using a literal substring."""
    if not isinstance(query, str) or not query.strip():
        raise VaultError("query must not be blank")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise VaultError("limit must be greater than zero")
    matches: list[dict] = []
    scanned_archives = 0
    skipped_records = 0
    truncated = False
    for manifest in _manifests(Path(vault_dir)):
        if not isinstance(manifest, dict):
            raise VaultError("archive listing contains an invalid manifest")
        if not _selected(manifest, project, session_id):
            continue
        archive_id, _ = _validate_manifest(manifest)
        scanned_archives += 1
        iterator = _iter_readable(Path(vault_dir), manifest)
        for line_number, kind, text in iterator:
            if kind is None:
                skipped_records += 1
                continue
            if query.casefold() not in text.casefold():
                continue
            if len(matches) >= limit:
                truncated = True
                for _, remaining_kind, _ in iterator:
                    if remaining_kind is None:
                        skipped_records += 1
                break
            matches.append(
                {
                    "archive_id": archive_id,
                    "session_id": str(manifest.get("session_id", "")),
                    "relative_path": str(manifest.get("relative_path", "")),
                    "cwd": str(manifest.get("cwd", "")),
                    "line": line_number,
                    "kind": kind,
                    "snippet": _match_context(text, query),
                }
            )
        if truncated:
            break
    return {
        "query": query,
        "matches": matches,
        "scanned_archives": scanned_archives,
        "truncated": truncated,
        "skipped_records": skipped_records,
    }


def show(vault_dir: Path, archive_id: str, limit: int = 100, query: str | None = None) -> dict:
    """Return readable records from one archive without writing plaintext."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise VaultError("limit must be greater than zero")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise VaultError("query must not be blank")
    try:
        manifest = resolve_archive(Path(vault_dir), archive_id)
    except VaultError:
        raise
    resolved_archive_id = str(manifest["archive_id"])
    records: list[dict] = []
    skipped_records = 0
    truncated = False
    iterator = _iter_readable(Path(vault_dir), manifest)
    for line_number, kind, text in iterator:
        if kind is None:
            skipped_records += 1
            continue
        if query is not None and query.casefold() not in text.casefold():
            continue
        if len(records) >= limit:
            truncated = True
            for _, remaining_kind, _ in iterator:
                if remaining_kind is None:
                    skipped_records += 1
            break
        records.append({"kind": kind, "line": line_number, "text": text[:MAX_SNIPPET_CHARS]})
    return {"archive_id": resolved_archive_id, "records": records, "truncated": truncated, "skipped_records": skipped_records}
