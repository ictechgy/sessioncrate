"""Best-effort open-file activity probe for one selected session file.

This module deliberately does not inspect Codex state, process arguments or
session contents.  It asks the platform ``lsof`` utility about exactly one
regular file and fails closed whenever the result is incomplete or ambiguous.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any


_LSOF_TIMEOUT_SECONDS = 5


def _unknown(reason: str) -> dict[str, str]:
    return {"state": "unknown", "reason": reason}


def _parse_lsof_result(completed: Any) -> dict[str, str]:
    returncode = getattr(completed, "returncode", None)
    stdout = getattr(completed, "stdout", None)
    stderr = getattr(completed, "stderr", None)
    if not isinstance(returncode, int) or not isinstance(stdout, str) or not isinstance(stderr, str):
        return _unknown("lsof returned malformed data")
    if stderr.strip():
        return _unknown("lsof emitted a warning or error")
    if returncode == 1 and not stdout.strip():
        return {"state": "inactive", "reason": "lsof found no open descriptor"}
    # Recursive +D selection may return 1 when only some descendants have
    # open descriptors. A valid opener still blocks removal in that case.
    if returncode not in (0, 1):
        return _unknown("lsof returned an ambiguous status")
    lines = stdout.splitlines()
    if not lines or any(not line for line in lines):
        return _unknown("lsof returned malformed output")
    process_record = False
    for line in lines:
        if line.startswith("p") and line[1:].isdigit():
            process_record = True
            continue
        if line.startswith("f") and len(line) > 1:
            continue
        return _unknown("lsof returned malformed output")
    if not process_record:
        return _unknown("lsof returned no process record")
    return {"state": "active", "reason": "lsof found an open descriptor"}


def _probe_selected(path: Path, *, directory: bool) -> dict[str, str]:
    try:
        absolute = Path(path).absolute()
    except (OSError, TypeError, ValueError):
        return _unknown("target path is invalid")
    try:
        target_stat = absolute.lstat()
    except FileNotFoundError:
        return _unknown("target file is missing")
    except OSError:
        return _unknown("target file could not be inspected")
    if stat.S_ISLNK(target_stat.st_mode):
        return _unknown("target file is a symlink")
    if directory:
        if not stat.S_ISDIR(target_stat.st_mode):
            return _unknown("target is not a directory")
    elif not stat.S_ISREG(target_stat.st_mode):
        return _unknown("target file is not regular")

    executable = shutil.which("lsof")
    if not executable:
        return _unknown("lsof is unavailable")
    selection = ["+D", str(absolute)] if directory else ["--", str(absolute)]
    command = [executable, "-nP", "-Fpf", *selection]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=_LSOF_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _unknown("lsof timed out")
    except (OSError, subprocess.SubprocessError, SyntaxError, ValueError):
        return _unknown("lsof could not be executed")
    return _parse_lsof_result(completed)


def probe_session_file(path: Path) -> dict[str, str]:
    """Inspect one regular file; only clean lsof absence is inactive."""
    return _probe_selected(path, directory=False)


def probe_session_tree(path: Path) -> dict[str, str]:
    """Inspect a selected directory and descendants, including directory FDs.

    Callers must first validate their selected session boundary and reject
    symlinks inside the tree. Incomplete recursive inspection fails closed.
    This reports OS-open-file state, not an AI tool's logical session state.
    """
    return _probe_selected(path, directory=True)


__all__ = ["probe_session_file", "probe_session_tree"]
