"""Synthetic fixtures for storage and search tests.

These helpers deliberately create a throwaway Codex-shaped directory.  They
never inspect the user's real ``~/.codex`` directory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def make_codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "synthetic codex home"
    (home / "sessions").mkdir(parents=True)
    (home / "archived_sessions").mkdir()
    return home


def make_session(
    home: Path,
    relative_path: str = "sessions/project/rollout-synthetic.jsonl",
    *,
    session_id: str = "session-synthetic",
    cwd: str = "/tmp/project",
    tool_version: str = "test-codex",
    started_at: str = "2026-01-01T00:00:00Z",
    lines: list[dict] | None = None,
    age_seconds: int = 2 * 86400,
    mode: int = 0o600,
) -> Path:
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    records = lines or [
        {
            "type": "session_meta",
            "session_id": session_id,
            "cwd": cwd,
            "tool_version": tool_version,
            "started_at": started_at,
        },
        {"type": "message", "role": "user", "text": "보관할 세션 내용 " * 100},
    ]
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, mode)
    stamp = time.time_ns() - age_seconds * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return path


def write_session_bytes(
    home: Path,
    relative_path: str,
    payload: bytes,
    *,
    age_seconds: int = 2 * 86400,
    mode: int = 0o600,
) -> Path:
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.chmod(path, mode)
    stamp = time.time_ns() - age_seconds * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return path
