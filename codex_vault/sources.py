"""Read-only source adapters for local AI coding session artifacts.

The Claude layout follows the official session documentation:
https://code.claude.com/docs/en/sessions and
https://code.claude.com/docs/en/sub-agents.  Claude's JSONL schema is an
internal format which may change; parsers therefore preserve unknown records.
Gemini's native layout and ``GEMINI_CLI_HOME`` are documented at:
https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md.
Grok, GLM, AGY, and OpenCode layouts are intentionally conservative: native
database/config paths are never guessed, and opaque tools use explicit exports.
The public Grok ACP observation used for the bounded fixture is documented at
https://github.com/PocketShell-io/pocketshell/issues/2193; no official stable
Grok transcript schema was found, so the Grok reader is explicitly best-effort.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator
from urllib.parse import unquote

from . import storage
from .storage import VaultError


MAX_RECORD_BYTES = 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CREDENTIAL_NAMES = {
    ".env", ".env.local", "auth.json", "credentials.json", "oauth.json",
    "settings.json", "config.json", "claude_desktop_config.json",
}
_EXPORT_EXTENSIONS = (".json", ".jsonl", ".md", ".markdown", ".txt")
_EXPORT_TOOLS = {"agy", "glm", "opencode", "export"}
_NATIVE_TOOLS = {"claude", "grok", "gemini", "codex"}
_DIAGNOSTIC_TOOLS = {"zcode", "autoclaw"}
_DIAGNOSTIC_FILENAME = re.compile(r"model-io-[A-Za-z0-9_-]{1,80}\.jsonl\Z")
_DIAGNOSTIC_APP_ROOTS = {
    "zcode": {".zcode"},
    # AutoClaw may write through the same Zcode diagnostic profile. The tool
    # label alone does not establish which app owns an explicitly selected log.
    "autoclaw": {".zcode"},
}
_DIAGNOSTIC_LOG_LEAVES = {"rollout", "debug"}
_NATIVE_ROOT_NAMES = {".claude", ".grok", ".gemini", ".codex", ".zcode", ".autoclaw", ".auto-claw"}
_MODEL_IO_HIDDEN_CONTENT_KINDS = {
    "reasoning", "thinking", "analysis", "redacted_thinking", "redacted-thinking",
}
_CREDENTIAL_TOKEN = re.compile(r"(?:^|[._-])(credentials?|oauth|tokens?|secrets?|accounts?|api[_-]?keys?)(?:$|[._-])", re.IGNORECASE)


@dataclass(frozen=True)
class SessionGroup:
    tool: str
    source_root: Path
    session_id: str
    files: tuple[str, ...]
    root_entries: tuple[str, ...]
    formats: dict[str, str]
    cwd: str = ""
    source_mode: str = "native"
    removable: bool = True
    notes: tuple[str, ...] = ()


def default_source_root(tool: str) -> Path | None:
    """Return a path without inspecting it or reading any configuration."""
    if tool == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    if tool == "grok":
        return Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser()
    if tool == "gemini":
        gemini_home = os.environ.get("GEMINI_CLI_HOME")
        return (Path(gemini_home).expanduser() / ".gemini") if gemini_home else Path.home() / ".gemini"
    if tool == "codex":
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    if tool in _EXPORT_TOOLS or tool in _DIAGNOSTIC_TOOLS:
        return None
    raise VaultError(f"unknown source tool: {tool}")


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {"id": "codex", "label": "Codex", "mode": "native", "default_root": str(default_source_root("codex")), "caveat": "Handled by the legacy backend for plain JSONL and .jsonl.zst; new zstd archive/search/show support requires Python 3.14."},
        {"id": "claude", "label": "Claude Code", "mode": "native", "default_root": str(default_source_root("claude")), "caveat": "CLI transcripts and same-session companions only; Desktop/Cowork excluded."},
        {"id": "grok", "label": "Grok Build", "mode": "native", "default_root": str(default_source_root("grok")), "caveat": "ACP updates parsing is best-effort; native session directories are archived as complete groups."},
        {"id": "gemini", "label": "Gemini CLI", "mode": "native", "default_root": str(default_source_root("gemini")), "caveat": "Only documented tmp/*/chats/session-*.json artifacts are selected."},
        {"id": "agy", "label": "Antigravity", "mode": "export", "default_root": None, "caveat": "Explicit plaintext export root required; native SQLite is unsupported."},
        {"id": "glm", "label": "GLM", "mode": "export", "default_root": None, "caveat": "Explicit user-selected export root required."},
        {"id": "opencode", "label": "OpenCode", "mode": "export", "default_root": None, "caveat": "Use JSON/text exports; native shared databases are untouched."},
        {"id": "export", "label": "Generic export", "mode": "export", "default_root": None, "caveat": "Explicit plaintext export root required."},
        {"id": "zcode", "label": "Zcode diagnostics", "mode": "diagnostic", "default_root": None, "caveat": "Explicit flat model-io diagnostic log directory required; logs are partial diagnostics, not complete or resumable sessions."},
        {"id": "autoclaw", "label": "AutoClaw diagnostics", "mode": "diagnostic", "default_root": None, "caveat": "Explicit flat model-io diagnostic log directory required; logs are partial diagnostics, not complete or resumable sessions."},
    ]


def _parts(path: str) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or candidate.as_posix() != path or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate.parts


def valid_relative(tool: str, path: str, source_mode: str) -> bool:
    parts = _parts(path)
    if parts is None or not parts:
        return False
    if source_mode == "diagnostic":
        return tool in _DIAGNOSTIC_TOOLS and len(parts) == 1 and _DIAGNOSTIC_FILENAME.fullmatch(parts[0]) is not None
    if source_mode not in {"native", "export"}:
        return False
    native_project_layout = (
        source_mode == "native"
        and (
            (tool == "claude" and len(parts) >= 3 and parts[0] == "projects")
            or (tool == "grok" and len(parts) >= 4 and parts[0] == "sessions")
            or (tool == "gemini" and len(parts) == 4 and parts[0] == "tmp" and parts[2] == "chats" and parts[3].startswith("session-"))
        )
    )
    if any(
        _is_forbidden_name(part)
        for index, part in enumerate(parts)
        if not (native_project_layout and index == 1)
    ):
        return False
    if source_mode == "export":
        if any(part.startswith(".") for part in parts):
            return False
        return tool in _EXPORT_TOOLS and path.lower().endswith(_EXPORT_EXTENSIONS)
    if tool == "codex":
        # Keep this validator permissive for historical grouped bundle
        # manifests. New legacy archives use the stricter discovery predicate
        # below and are routed through storage directly.
        return len(parts) >= 2 and parts[0] in {"sessions", "archived_sessions"}
    if tool == "claude":
        return len(parts) >= 3 and parts[0] == "projects"
    if tool == "grok":
        return len(parts) >= 4 and parts[0] == "sessions"
    if tool == "gemini":
        return len(parts) == 4 and parts[0] == "tmp" and parts[2] == "chats" and parts[3].startswith("session-")
    return False


def _is_forbidden_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower in _CREDENTIAL_NAMES
        or lower.startswith(".env")
        or lower.startswith((".credentials", "oauth", "token.", "token_", "google_accounts", "account.", "account_", "secret.", "secret_"))
        or _CREDENTIAL_TOKEN.search(lower) is not None
        or lower.startswith("auth.")
        or lower.startswith("credentials.")
        or lower.endswith((".key", ".pem"))
    )


def _valid_codex_discovery_path(path: str) -> bool:
    """Apply the legacy rollout filename contract to new discovery only."""
    try:
        storage._valid_relative_path(path)
    except VaultError:
        return False
    return valid_relative("codex", path, "native")


def _known_native_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        *(default_source_root(tool) for tool in ("claude", "grok", "gemini", "codex")),
        home / ".local/share/opencode/storage",
        home / ".local/share/opencode",
        home / "Library/Application Support/opencode/storage",
        home / "Library/Application Support/Antigravity/User/globalStorage",
        home / ".config/Antigravity/User/globalStorage",
        home / ".antigravity",
    )


def _native_layout_signature(root: Path) -> bool:
    try:
        names = {entry.name for entry in os.scandir(root)}
    except OSError:
        return False
    credential_names = {".credentials.json", "oauth_creds.json", "settings.json", "config.toml"}
    if "projects" in names and names.intersection(credential_names):
        return True
    tmp = root / "tmp"
    if tmp.is_dir():
        try:
            with os.scandir(tmp) as entries:
                if any((Path(entry.path) / "chats").is_dir() for entry in entries if entry.is_dir(follow_symlinks=False)):
                    return True
        except OSError:
            return True
    if "sessions" in names and (root / "config.toml").is_file():
        return True
    for base in (root, root / "storage"):
        try:
            with os.scandir(base) as entries:
                if {entry.name for entry in entries if entry.is_dir(follow_symlinks=False)} >= {"session", "message", "part"}:
                    return True
        except OSError:
            continue
    return False


def _signature_covers(signature_root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(signature_root)
    except ValueError:
        return False
    if not relative.parts:
        return True
    return relative.parts[0].lower() in {"projects", "sessions", "tmp", "storage"}


def _validate_export_root(root: Path) -> None:
    candidate = root.resolve()
    home = Path.home().resolve()
    if candidate == home or home.is_relative_to(candidate):
        raise VaultError("export source root is a native tool root or user home")
    candidate_parts = tuple(part.lower() for part in candidate.parts)
    if any(part in _NATIVE_ROOT_NAMES for part in candidate_parts):
        raise VaultError("export source root is a native tool root or descendant")
    if "storage" in candidate_parts:
        storage_index = len(candidate_parts) - 1 - candidate_parts[::-1].index("storage")
        if any(part in {"session", "message", "part"} for part in candidate_parts[storage_index + 1:]):
            raise VaultError("export source root is an OpenCode native storage path")
    for native in _known_native_roots():
        if native is None:
            continue
        native_path = Path(native).expanduser().resolve()
        if candidate == native_path or candidate.is_relative_to(native_path) or native_path.is_relative_to(candidate):
            raise VaultError("export source root is a native tool root or its ancestor")
    probe = candidate
    while probe != Path(probe.anchor) and probe != home:
        if _signature_covers(probe, candidate) and _native_layout_signature(probe):
            raise VaultError("export source root has a native tool layout")
        probe = probe.parent


def _decoded_cwd(value: str) -> str:
    decoded = unquote(value)
    return decoded if decoded.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", decoded) else ""


def _root(path: Path) -> Path:
    root = Path(path).expanduser()
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise VaultError("source root must be an existing non-symlink directory")
    return root


def _validate_diagnostic_root(tool: str, root: Path) -> None:
    """Keep diagnostic discovery inside one explicitly selected flat log dir."""
    if tool not in _DIAGNOSTIC_TOOLS:
        raise VaultError("diagnostic source mode is only available for diagnostic tools")
    if any(part in {".", ".."} for part in root.parts):
        raise VaultError("diagnostic source root is not canonical")
    try:
        storage._check_path_components(root)
    except VaultError as exc:
        raise VaultError("diagnostic source root contains a symlink component") from exc

    try:
        parts = tuple(part.casefold() for part in root.parts)
        home = Path.home().expanduser()
    except (OSError, RuntimeError):
        raise VaultError("diagnostic source root could not be checked")

    if root == home or home.is_relative_to(root):
        raise VaultError("diagnostic source root cannot be the user home or an ancestor")

    app_roots = _DIAGNOSTIC_APP_ROOTS[tool]
    for index, part in enumerate(parts):
        if part not in _NATIVE_ROOT_NAMES:
            continue
        suffix = parts[index:]
        if part in app_roots and len(suffix) == 3 and suffix[1] == "cli" and suffix[2] in _DIAGNOSTIC_LOG_LEAVES:
            continue
        raise VaultError("diagnostic source root is a native app root; select its rollout or debug directory")

    candidate = root.resolve()
    for native in _known_native_roots():
        if native is None:
            continue
        native_path = Path(native).expanduser().resolve()
        if candidate == native_path or candidate.is_relative_to(native_path) or native_path.is_relative_to(candidate):
            raise VaultError("diagnostic source root is a native app root; select its rollout or debug directory")


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _format_for(path: str, tool: str) -> str:
    lower = path.lower()
    if tool in _DIAGNOSTIC_TOOLS and _DIAGNOSTIC_FILENAME.fullmatch(path) is not None:
        return "model-io-jsonl"
    if lower.endswith(".jsonl"):
        if tool == "claude":
            return "claude-jsonl"
        if tool == "grok":
            return "grok-acp-jsonl"
        return "generic-jsonl"
    if lower.endswith(".json"):
        if tool == "gemini":
            return "gemini-json"
        if tool == "opencode":
            return "opencode-json"
        return "common-json"
    if lower.endswith((".txt", ".md", ".markdown")):
        return "text"
    return "binary"


def _walk(root: Path, start: Path, *, skip_hidden: bool = False) -> tuple[list[str], list[str]]:
    files: list[str] = []
    notes: list[str] = []
    stack = [start]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            notes.append(f"unreadable directory: {_rel(root, current)}")
            continue
        for entry in entries:
            if skip_hidden and entry.name.startswith("."):
                continue
            relative = _rel(root, Path(entry.path))
            try:
                if entry.is_symlink():
                    notes.append(f"symlink member: {relative}")
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(relative)
                else:
                    notes.append(f"non-regular member: {relative}")
            except OSError:
                notes.append(f"unreadable member: {relative}")
    return sorted(files), notes


def _group(tool: str, root: Path, session_id: str, files: list[str], roots: list[str], *, source_mode: str = "native", notes: list[str] | None = None, cwd: str = "") -> SessionGroup:
    notes = list(notes or [])
    safe_files = [path for path in files if valid_relative(tool, path, source_mode)]
    if len(safe_files) != len(files):
        notes.append("member outside the adapter scope")
    formats = {path: _format_for(path, tool) for path in safe_files}
    return SessionGroup(
        tool=tool,
        source_root=root,
        session_id=session_id,
        files=tuple(sorted(safe_files)),
        root_entries=tuple(sorted(set(roots))),
        formats=formats,
        cwd=cwd,
        source_mode=source_mode,
        removable=not notes and bool(safe_files),
        notes=tuple(sorted(set(notes))),
    )


def _unreadable_group(tool: str, root: Path, relative: str, *, cwd: str = "", source_mode: str = "native") -> SessionGroup:
    return _group(
        tool,
        root,
        f"unreadable:{relative}",
        [],
        [] if relative == "." else [relative],
        cwd=cwd,
        source_mode=source_mode,
        notes=[f"unreadable directory: {relative}"],
    )


def _discovery_warning_group(
    tool: str,
    root: Path,
    notes: list[str],
    *,
    scope: str,
    source_mode: str = "native",
) -> SessionGroup:
    """Keep walk diagnostics separate from independently healthy file groups."""
    return _group(
        tool,
        root,
        f"discovery-warning:{scope}",
        [],
        [],
        source_mode=source_mode,
        notes=notes,
    )


def _discover_claude(root: Path) -> list[SessionGroup]:
    projects = root / "projects"
    if not projects.is_dir() or projects.is_symlink():
        return []
    groups: list[SessionGroup] = []
    try:
        project_entries = sorted(os.scandir(projects), key=lambda entry: entry.name)
    except OSError:
        return [_unreadable_group("claude", root, _rel(root, projects))]
    for project_entry in project_entries:
        project = Path(project_entry.path)
        try:
            if project_entry.is_symlink() or not project_entry.is_dir(follow_symlinks=False):
                continue
            entries = sorted(os.scandir(project), key=lambda item: item.name)
        except OSError:
            groups.append(_unreadable_group("claude", root, _rel(root, project)))
            continue
        for entry in entries:
            main = Path(entry.path)
            if not entry.name.endswith(".jsonl"):
                continue
            main_rel = _rel(root, main)
            notes: list[str] = []
            files: list[str] = []
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                notes.append(f"unsafe main transcript: {main_rel}")
            else:
                files.append(main_rel)
            sidecar = project / main.stem
            roots = [main_rel]
            if sidecar.exists() or sidecar.is_symlink():
                roots.append(_rel(root, sidecar))
                if sidecar.is_symlink():
                    notes.append(f"symlink companion root: {_rel(root, sidecar)}")
                elif sidecar.is_dir():
                    side_files, side_notes = _walk(root, sidecar)
                    files.extend(side_files)
                    notes.extend(side_notes)
                else:
                    notes.append(f"companion root is not a directory: {_rel(root, sidecar)}")
            groups.append(_group("claude", root, main.stem, files, roots, notes=notes))
    return groups


def _discover_grok(root: Path) -> list[SessionGroup]:
    sessions = root / "sessions"
    if not sessions.is_dir() or sessions.is_symlink():
        return []
    groups: list[SessionGroup] = []
    try:
        projects = sorted(sessions.iterdir(), key=lambda path: path.name)
    except OSError:
        return [_unreadable_group("grok", root, _rel(root, sessions))]
    for project in projects:
        try:
            if project.is_symlink() or not project.is_dir():
                continue
            sessions_in_project = sorted(project.iterdir(), key=lambda path: path.name)
        except OSError:
            groups.append(_unreadable_group("grok", root, _rel(root, project), cwd=_decoded_cwd(project.name)))
            continue
        for session in sessions_in_project:
            rel = _rel(root, session)
            if session.is_symlink():
                groups.append(_group("grok", root, session.name, [], [rel], cwd=_decoded_cwd(project.name), notes=[f"symlink session root: {rel}"]))
            elif session.is_dir():
                files, notes = _walk(root, session)
                groups.append(_group("grok", root, session.name, files, [rel], cwd=_decoded_cwd(project.name), notes=notes))
    return groups


def _discover_gemini(root: Path) -> list[SessionGroup]:
    chats_root = root / "tmp"
    if not chats_root.is_dir() or chats_root.is_symlink():
        return []
    groups: list[SessionGroup] = []
    try:
        projects = sorted(chats_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return [_unreadable_group("gemini", root, _rel(root, chats_root))]
    for project in projects:
        chats = project / "chats"
        try:
            if project.is_symlink() or not chats.is_dir() or chats.is_symlink():
                continue
            entries = sorted(chats.iterdir(), key=lambda path: path.name)
        except OSError:
            groups.append(_unreadable_group("gemini", root, _rel(root, chats)))
            continue
        for entry in entries:
            if not entry.name.startswith("session-") or entry.suffix not in {".json", ".jsonl"}:
                continue
            rel = _rel(root, entry)
            if entry.is_symlink() or not entry.is_file():
                groups.append(_group("gemini", root, entry.stem.removeprefix("session-"), [], [rel], notes=[f"unsafe session file: {rel}"]))
            else:
                groups.append(_group("gemini", root, entry.stem.removeprefix("session-"), [rel], [rel]))
    return groups


def _discover_codex(root: Path) -> list[SessionGroup]:
    groups: list[SessionGroup] = []
    for scope in ("sessions", "archived_sessions"):
        base = root / scope
        if not base.is_dir() or base.is_symlink():
            continue
        files, notes = _walk(root, base)
        for path in files:
            if _valid_codex_discovery_path(path):
                groups.append(_group("codex", root, Path(path).stem, [path], [path]))
        if notes:
            groups.append(_discovery_warning_group("codex", root, notes, scope=scope))
    return groups


def _discover_export(tool: str, root: Path) -> list[SessionGroup]:
    if root.name.lower() in _NATIVE_ROOT_NAMES:
        raise VaultError("export source root is a native tool root or user home")
    _validate_export_root(root)
    groups: list[SessionGroup] = []
    files, notes = _walk(root, root, skip_hidden=True)
    for path in files:
        if path.lower().endswith((".db", ".sqlite", ".sqlite3")):
            # Opaque shared databases are deliberately outside the export adapter.
            continue
        if not path.lower().endswith(_EXPORT_EXTENSIONS):
            continue
        if _is_forbidden_name(Path(path).name):
            continue
        groups.append(_group(tool, root, Path(path).stem, [path], [path], source_mode="export"))
    if notes:
        groups.append(_discovery_warning_group(tool, root, notes, scope=".", source_mode="export"))
    return groups


def _discover_diagnostic(tool: str, root: Path) -> list[SessionGroup]:
    """Discover only direct model-io files in one explicit diagnostic directory."""
    _validate_diagnostic_root(tool, root)
    groups: list[SessionGroup] = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return [_unreadable_group(tool, root, ".", source_mode="diagnostic")]
    for entry in entries:
        if _DIAGNOSTIC_FILENAME.fullmatch(entry.name) is None:
            continue
        relative = entry.name
        try:
            if entry.is_symlink():
                groups.append(_group(
                    tool,
                    root,
                    Path(entry.name).stem,
                    [],
                    [relative],
                    source_mode="diagnostic",
                    notes=[f"symlink diagnostic log: {relative}"],
                ))
            elif entry.is_file(follow_symlinks=False):
                groups.append(_group(
                    tool,
                    root,
                    Path(entry.name).stem,
                    [relative],
                    [relative],
                    source_mode="diagnostic",
                ))
        except OSError:
            groups.append(_group(
                tool,
                root,
                Path(entry.name).stem,
                [],
                [relative],
                source_mode="diagnostic",
                notes=[f"unreadable diagnostic log: {relative}"],
            ))
    return groups


def discover(tool: str, source_root: Path) -> list[SessionGroup]:
    """Discover metadata and paths only; no transcript/config bytes are read."""
    root = _root(source_root)
    if tool == "claude":
        return _discover_claude(root)
    if tool == "grok":
        return _discover_grok(root)
    if tool == "gemini":
        return _discover_gemini(root)
    if tool == "codex":
        return _discover_codex(root)
    if tool in _DIAGNOSTIC_TOOLS:
        return _discover_diagnostic(tool, root)
    if tool in _EXPORT_TOOLS:
        return _discover_export(tool, root)
    raise VaultError(f"unknown source tool: {tool}")


def _clean_text(value: str) -> str:
    return _CONTROL_CHARS.sub(" ", value).strip()


def _structured_text(value: Any, depth: int = 0, key: str = "") -> list[str]:
    """Read bounded tool arguments without serializing opaque payloads."""
    if depth > 6:
        return []
    lower_key = key.lower()
    if lower_key in {"data", "basedata", "base64", "image", "image_url", "inlinedata", "token", "password", "secret"}:
        return []
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text and len(text) <= MAX_RECORD_BYTES else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:64]:
            parts.extend(_structured_text(item, depth + 1, key))
        return parts
    if isinstance(value, dict):
        parts = []
        for child_key, child_value in list(value.items())[:64]:
            parts.extend(_structured_text(child_value, depth + 1, str(child_key)))
        return parts
    return []


def _text_parts(value: Any, depth: int = 0) -> list[str]:
    if depth > 32:
        return []
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_text_parts(item, depth + 1))
        return parts
    if not isinstance(value, dict):
        return []
    kind = value.get("type")
    if isinstance(kind, str) and (kind.startswith("image") or kind in {"input_image", "output_image", "inlineData"}):
        return []
    parts: list[str] = []
    for key in ("text", "message", "summary", "content", "parts", "state", "output", "delta", "command", "result", "resultDisplay", "error", "name", "input", "arguments", "rawInput", "sessionUpdate", "toolCalls", "toolCallId"):
        if key in value:
            if key in {"input", "arguments", "rawInput"}:
                parts.extend(_structured_text(value[key], depth + 1, key))
            else:
                parts.extend(_text_parts(value[key], depth + 1))
    return parts


_MODEL_IO_MESSAGE_KEYS = ("text", "content", "parts")
_MODEL_IO_TOOL_KEYS = {
    "name", "toolname", "command", "input", "arguments", "rawinput",
    "output", "result", "resultdisplay", "content", "text", "message",
    "path", "filepath", "file_path", "code", "description", "status",
    "stdout", "stderr", "exitcode", "error",
}
_MODEL_IO_HIDDEN_KEY_PARTS = (
    "authorization", "cookie", "credential", "password", "secret", "token",
    "api_key", "apikey", "provideroption", "reasoning", "header", "binary",
    "base64", "inlinedata",
)


def _model_io_hidden_key(key: str) -> bool:
    lower = key.casefold().replace("-", "_")
    return lower in {"data", "media", "image", "images"} or any(part in lower for part in _MODEL_IO_HIDDEN_KEY_PARTS)


def _model_io_hidden_content_kind(kind: Any) -> bool:
    if not isinstance(kind, str):
        return False
    normalized = kind.casefold().replace("-", "_")
    return normalized in _MODEL_IO_HIDDEN_CONTENT_KINDS or normalized.startswith((
        "reasoning_", "thinking_", "analysis_", "redacted_thinking_",
    ))


def _model_io_scalar(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    text = _clean_text(value)
    return [text] if text and len(text) <= MAX_RECORD_BYTES else []


def _model_io_message_text(value: Any, depth: int = 0) -> list[str]:
    """Read message content fields only; ignore metadata and media payloads."""
    if depth > 12:
        return []
    scalar = _model_io_scalar(value)
    if scalar:
        return scalar
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:128]:
            parts.extend(_model_io_message_text(item, depth + 1))
        return parts
    if not isinstance(value, dict):
        return []
    kind = value.get("type")
    if _model_io_hidden_content_kind(kind) or (isinstance(kind, str) and (
        kind.casefold().startswith(("image", "audio", "video"))
        or kind.casefold() in {"input_image", "output_image", "inline_data", "inlinedata", "binary"}
    )):
        return []
    parts = []
    for key in _MODEL_IO_MESSAGE_KEYS:
        if key in value:
            parts.extend(_model_io_message_text(value[key], depth + 1))
    return parts


def _model_io_tool_text(value: Any, depth: int = 0, key: str = "") -> list[str]:
    """Read useful tool call/result values while excluding secret-bearing fields."""
    if depth > 12 or _model_io_hidden_key(key):
        return []
    scalar = _model_io_scalar(value)
    if scalar:
        return scalar
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:128]:
            parts.extend(_model_io_tool_text(item, depth + 1, key))
        return parts
    if not isinstance(value, dict):
        return []
    kind = value.get("type")
    if _model_io_hidden_content_kind(kind) or (isinstance(kind, str) and (
        kind.casefold().startswith(("image", "audio", "video"))
        or kind.casefold() in {"input_image", "output_image", "inline_data", "inlinedata", "binary"}
    )):
        return []
    parts: list[str] = []
    for child_key, child_value in list(value.items())[:128]:
        normalized = str(child_key).casefold().replace("-", "_")
        if _model_io_hidden_key(str(child_key)):
            continue
        if normalized == "error":
            if isinstance(child_value, dict):
                parts.extend(_model_io_scalar(child_value.get("message")))
            continue
        if normalized in _MODEL_IO_TOOL_KEYS or (depth > 0 and normalized not in {"type", "role", "id"}):
            parts.extend(_model_io_tool_text(child_value, depth + 1, str(child_key)))
    return parts


def _model_io_messages(value: Any) -> list[str]:
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:128]:
            parts.extend(_model_io_message_text(item))
        return parts
    return _model_io_message_text(value)


def _model_io_body(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value.encode("utf-8", errors="ignore")) > MAX_RECORD_BYTES:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _model_io_error(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _model_io_scalar(value.get("message"))


def _model_io_record_text(record: dict[str, Any]) -> tuple[str, str] | None:
    record_type = record.get("type")
    if record_type not in {"model_io", "modelIOReset"}:
        return None
    parts: list[str] = []
    seen: set[str] = set()

    def add(values: list[str]) -> None:
        for value in values:
            if value not in seen:
                seen.add(value)
                parts.append(value)

    request = record.get("request")
    if isinstance(request, dict):
        for key in ("messages", "sdkMessages"):
            if key in request:
                add(_model_io_messages(request[key]))
        if "body" in request:
            body = _model_io_body(request["body"])
            if body is not None and "messages" in body:
                add(_model_io_messages(body["messages"]))
    if "sdkMessages" in record:
        add(_model_io_messages(record["sdkMessages"]))

    response = record.get("response")
    if isinstance(response, dict):
        add(_model_io_scalar(response.get("text")))
        for key in ("toolCalls", "toolResults"):
            if key in response:
                add(_model_io_tool_text(response[key], key=key))
        add(_model_io_error(response.get("error")))
    for key in ("toolCalls", "toolResults"):
        if key in record:
            add(_model_io_tool_text(record[key], key=key))
    if "error" in record:
        add(_model_io_error(record["error"]))
    if not parts:
        return None
    return (str(record_type), " ".join(parts))


def _record_text(record: Any, format: str) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    if format == "model-io-jsonl":
        return _model_io_record_text(record)
    if format == "claude-jsonl":
        record_type = str(record.get("type", "unknown"))
        message = record.get("message", record)
        parts = _text_parts(message.get("content") if isinstance(message, dict) and "content" in message else message)
        return (record_type, " ".join(parts)) if parts else None
    if format == "grok-acp-jsonl":
        envelope: Any = record.get("update", record)
        if envelope is record:
            wrapped_update = False
            for wrapper in ("params", "data"):
                candidate = record.get(wrapper)
                if isinstance(candidate, dict) and "update" in candidate:
                    envelope = candidate["update"]
                    wrapped_update = True
                    break
            if not wrapped_update:
                for wrapper in ("params", "data"):
                    candidate = record.get(wrapper)
                    if candidate is not None:
                        envelope = candidate
                        break
        session_update = envelope.get("sessionUpdate") if isinstance(envelope, dict) else None
        if isinstance(session_update, str):
            update_kind = session_update
        elif isinstance(session_update, dict):
            update_kind = session_update.get("kind")
        else:
            update_kind = None
        if update_kind in {"agent_thought_chunk", "hook_execution"}:
            return None
        if isinstance(envelope, dict):
            parts = _text_parts({
                key: value
                for key, value in envelope.items()
                if not (key == "sessionUpdate" and isinstance(value, str))
            })
        else:
            parts = _text_parts(envelope)
        return (str(record.get("type", "grok")), " ".join(parts)) if parts else None
    parts = _text_parts(record)
    return (str(record.get("type", record.get("role", "record"))), " ".join(parts)) if parts else None


def _skip(line: int, reason: str) -> dict[str, Any]:
    return {"line": line, "skipped": True, "reason": reason}


def _jsonl_records(stream: BinaryIO, format: str, max_record_bytes: int) -> Iterator[dict[str, Any]]:
    line_number = 0
    while True:
        raw = stream.readline(max_record_bytes + 1)
        if not raw:
            return
        line_number += 1
        if len(raw) > max_record_bytes:
            while not raw.endswith(b"\n"):
                raw = stream.readline(min(64 * 1024, max_record_bytes + 1))
                if not raw:
                    break
            yield _skip(line_number, "record exceeds maximum size")
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            yield _skip(line_number, "invalid JSON record")
            continue
        try:
            record = json.loads(decoded)
        except json.JSONDecodeError:
            yield _skip(line_number, "invalid JSON record")
            continue
        except ValueError:
            yield _skip(line_number, "unsupported JSON record")
            continue
        except RecursionError:
            yield _skip(line_number, "deeply nested JSON")
            continue
        readable = _record_text(record, format)
        if readable is None:
            yield _skip(line_number, "no readable text")
        else:
            yield {"line": line_number, "kind": readable[0], "text": readable[1]}


def _text_records(stream: BinaryIO, max_record_bytes: int) -> Iterator[dict[str, Any]]:
    line_number = 0
    while True:
        raw = stream.readline(max_record_bytes + 1)
        if not raw:
            return
        line_number += 1
        if len(raw) > max_record_bytes:
            while not raw.endswith(b"\n"):
                raw = stream.readline(min(64 * 1024, max_record_bytes + 1))
                if not raw:
                    break
            yield _skip(line_number, "record exceeds maximum size")
            continue
        try:
            text = _clean_text(raw.decode("utf-8"))
        except UnicodeDecodeError:
            yield _skip(line_number, "invalid UTF-8 text")
            continue
        if text:
            yield {"line": line_number, "kind": "text", "text": text}
        else:
            yield _skip(line_number, "empty text")


def _full_json_records(stream: BinaryIO, format: str) -> Iterator[dict[str, Any]]:
    data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        yield _skip(1, "JSON document exceeds maximum size")
        return
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        yield _skip(1, "invalid JSON document")
        return
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError:
        yield _skip(1, "invalid JSON document")
        return
    except ValueError:
        yield _skip(1, "unsupported JSON document")
        return
    except RecursionError:
        yield _skip(1, "deeply nested JSON")
        return
    messages = value.get("messages") if isinstance(value, dict) else value if isinstance(value, list) else None
    if not isinstance(messages, list) and isinstance(value, dict):
        messages = value.get("conversation") if isinstance(value, dict) else None
    if isinstance(messages, list):
        for index, message in enumerate(messages, 1):
            readable = _record_text(message, format)
            if readable is None:
                yield _skip(index, "no readable text")
            else:
                yield {"line": index, "kind": readable[0], "text": readable[1]}
        return
    readable = _record_text(value, format)
    if readable is None:
        yield _skip(1, "no readable text")
    else:
        yield {"line": 1, "kind": readable[0], "text": readable[1]}


def iter_text_records(stream: BinaryIO, format: str, max_record_bytes: int = MAX_RECORD_BYTES) -> Iterator[dict[str, Any]]:
    """Yield bounded readable records or explicit skipped diagnostics."""
    if isinstance(max_record_bytes, bool) or not isinstance(max_record_bytes, int) or max_record_bytes <= 0:
        raise VaultError("max_record_bytes must be positive")
    if format == "binary":
        yield _skip(1, "binary member is not decoded")
    elif format == "text":
        yield from _text_records(stream, max_record_bytes)
    elif format in {"claude-jsonl", "grok-acp-jsonl", "generic-jsonl", "codex-jsonl", "model-io-jsonl"}:
        yield from _jsonl_records(stream, format, max_record_bytes)
    elif format in {"gemini-json", "opencode-json", "common-json"}:
        yield from _full_json_records(stream, format)
    else:
        yield _skip(1, f"unknown format: {format}")


def _metadata_update(result: dict[str, str], obj: Any, depth: int = 0) -> None:
    if depth > 4 or not isinstance(obj, dict):
        return
    keys = {
        "session_id": ("sessionId", "session_id", "id"),
        "cwd": ("cwd", "workingDirectory", "projectPath"),
        "tool_version": ("version", "tool_version", "cliVersion"),
        "started_at": ("timestamp", "startTime", "startedAt", "createdAt"),
    }
    for target, candidates in keys.items():
        if target not in result:
            for key in candidates:
                value = obj.get(key)
                if isinstance(value, str) and value:
                    result[target] = value
                    break
    for value in obj.values():
        if isinstance(value, dict):
            _metadata_update(result, value, depth + 1)


def _recover_scalar_metadata(data: bytes, result: dict[str, str]) -> None:
    """Recover simple scalar fields when a bounded JSON snapshot is partial."""
    try:
        text = data.decode("utf-8", errors="ignore")
        patterns = {
            "session_id": ("sessionId", "session_id"),
            "cwd": ("cwd", "workingDirectory", "projectPath"),
            "tool_version": ("version", "tool_version", "cliVersion"),
            "started_at": ("timestamp", "startTime", "startedAt", "createdAt"),
        }
        for target, keys in patterns.items():
            if target in result:
                continue
            for key in keys:
                match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', text)
                if match:
                    try:
                        result[target] = json.loads('"' + match.group(1) + '"')
                    except json.JSONDecodeError:
                        result[target] = match.group(1)
                    break
    except (UnicodeError, RecursionError):
        pass


def metadata_from_bytes(data: bytes, format: str) -> dict[str, str]:
    """Best-effort metadata extraction from a bounded in-memory snapshot."""
    if not isinstance(data, bytes) or len(data) > MAX_JSON_BYTES:
        return {}
    result: dict[str, str] = {}
    if format == "model-io-jsonl":
        for line in data.splitlines()[:32]:
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                continue
            if not isinstance(value, dict) or value.get("type") not in {"model_io", "modelIOReset"}:
                continue
            if "session_id" not in result:
                for key in ("sessionId", "session_id"):
                    if isinstance(value.get(key), str) and value[key]:
                        result["session_id"] = value[key]
                        break
            if "started_at" not in result:
                for key in ("startedAt", "started_at"):
                    if isinstance(value.get(key), str) and value[key]:
                        result["started_at"] = value[key]
                        break
        return result
    if format in {"claude-jsonl", "grok-acp-jsonl", "generic-jsonl", "codex-jsonl"}:
        for line in data.splitlines()[:32]:
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                value = json.loads(decoded)
            except (json.JSONDecodeError, ValueError, RecursionError):
                continue
            _metadata_update(result, value)
        return result
    if format not in {"gemini-json", "opencode-json", "common-json"}:
        return result
    try:
        decoded = data.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        # Bundle snapshots provide only a bounded prefix. Recover simple
        # scalar metadata that appears before the prefix was cut, without
        # treating arbitrary transcript text as structured data.
        _recover_scalar_metadata(data, result)
        return result
    _metadata_update(result, value)
    return result
