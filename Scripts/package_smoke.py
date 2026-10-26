#!/usr/bin/env python3
"""Install a built wheel in an isolated venv and exercise the public CLI.

This smoke check creates synthetic Codex records and explicit export files in a
temporary directory, then exercises both archive paths through the installed CLI.
It never reads a checkout's user data, credentials, configuration, or sessions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import venv
import zipfile


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _fail(message: str, result: subprocess.CompletedProcess[str] | None = None) -> None:
    if result is not None:
        detail = (result.stdout + result.stderr).strip()
        if detail:
            message = f"{message}: {detail[-1000:]}"
    raise RuntimeError(message)


def _json_command(
    executable: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict:
    result = _run([str(executable), *args], cwd=cwd, env=env)
    if result.returncode:
        _fail("sessioncrate command failed", result)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        _fail(f"sessioncrate returned invalid JSON ({error})", result)


def _wheel_evidence(wheel: Path) -> dict[str, object]:
    forbidden_fragments = (
        "/reviews/",
        "/build/",
        "/dist/",
        "/.venv/",
        "/.git/",
        "/__pycache__/",
    )
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if any("codex-vault" in name for name in names):
            raise RuntimeError("wheel contains a legacy codex-vault entry point or path")
        bad = [name for name in names if any(fragment in f"/{name}" for fragment in forbidden_fragments)]
        if bad:
            raise RuntimeError(f"wheel contains excluded paths: {bad}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError("wheel must contain exactly one metadata file")
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        if "Requires-Dist:" in metadata:
            raise RuntimeError("runtime dependencies unexpectedly present in wheel metadata")
        entrypoint_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entrypoint_names) != 1:
            raise RuntimeError("wheel must contain one console entry point file")
        entrypoints = archive.read(entrypoint_names[0]).decode("utf-8")
        if "sessioncrate = codex_vault.cli:main" not in entrypoints:
            raise RuntimeError("sessioncrate console entry point is missing")
        if "codex-vault" in entrypoints:
            raise RuntimeError("legacy codex-vault console entry point is present")
        if not any(name == "codex_vault/__init__.py" for name in names):
            raise RuntimeError("wheel is missing the runtime package")
        return {
            "wheel": wheel.name,
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "wheel_files": len(names),
            "runtime_dependencies": 0,
            "console_scripts": ["sessioncrate"],
        }


def _sdist_evidence(sdist: Path) -> dict[str, object]:
    required = {
        "README.md",
        "README.ko.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "docs/compatibility.md",
        "docs/naming.md",
        "docs/release.md",
        "Scripts/package_smoke.py",
        "Scripts/verify.sh",
        "tests/helpers.py",
        "tests/__init__.py",
    }

    def excluded(name: str) -> bool:
        parts = Path(name).parts
        return (
            "reviews" in parts
            or ".git" in parts
            or "build" in parts
            or "dist" in parts
            or ".venv" in parts
            or "__pycache__" in parts
            or any(part == ".env" or part.startswith(".env.") for part in parts)
            or name.endswith((".pyc", ".pyo"))
        )

    # Read member names only. Never extract or execute untrusted archive data.
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = archive.getnames()
    if not names:
        raise RuntimeError("source distribution is empty")
    top_level = names[0].split("/", 1)[0]
    relative_names = {
        name[len(top_level) + 1:]
        for name in names
        if name.startswith(top_level + "/")
    }
    missing = sorted(required - relative_names)
    if missing:
        raise RuntimeError(f"source distribution is missing required files: {missing}")
    bad = sorted(name for name in relative_names if excluded(name))
    if bad:
        raise RuntimeError(f"source distribution contains excluded paths: {bad}")
    return {
        "sdist": sdist.name,
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
        "sdist_files": len(relative_names),
    }


def _synthetic_record() -> bytes:
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": "11111111-2222-3333-4444-555555555555",
                "cwd": "/synthetic/sessioncrate-smoke",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "sessioncrate smoke 한글 검색"}],
            },
        },
    ]
    return b"".join((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8") for record in records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="path to an already-built sessioncrate wheel")
    parser.add_argument(
        "--sdist",
        type=Path,
        help="optional source distribution to inspect without extracting or executing it",
    )
    args = parser.parse_args(argv)
    wheel = args.wheel.expanduser().resolve()
    if wheel.suffix != ".whl" or not wheel.is_file():
        parser.error("wheel must be an existing .whl file")
    sdist = args.sdist.expanduser().resolve() if args.sdist else None
    if sdist is not None and (not sdist.name.endswith((".tar.gz", ".tgz")) or not sdist.is_file()):
        parser.error("--sdist must be an existing .tar.gz or .tgz file")

    checkout = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="sessioncrate-package-smoke-") as raw_temp:
        temp = Path(raw_temp)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        venv_dir = temp / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = _run(
            [str(venv_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            cwd=temp,
            env=environment,
        )
        if install.returncode:
            _fail("isolated wheel installation failed", install)

        import_check = _run(
            [
                str(venv_python),
                "-c",
                (
                    "import codex_vault, codex_vault.bundles, codex_vault.search, "
                    "codex_vault.sources, codex_vault.storage, codex_vault.zstd_io; "
                    "print(codex_vault.__file__)"
                ),
            ],
            cwd=temp,
            env=environment,
        )
        if import_check.returncode:
            _fail("installed package import failed", import_check)
        imported = Path(import_check.stdout.strip()).resolve()
        if checkout in imported.parents:
            raise RuntimeError(f"package imported from checkout: {imported}")
        if venv_dir.resolve() not in imported.parents:
            raise RuntimeError(f"package imported outside isolated venv: {imported}")

        version = _run([str(venv_dir / ("Scripts/sessioncrate.exe" if os.name == "nt" else "bin/sessioncrate")), "--version"], cwd=temp, env=environment)
        if version.returncode or version.stdout.strip() != "sessioncrate 0.1.0":
            _fail("installed console script version check failed", version)

        source = temp / "codex-home" / "sessions" / "2025" / "01" / "01" / "rollout-smoke.jsonl"
        source.parent.mkdir(parents=True)
        raw = _synthetic_record()
        source.write_bytes(raw)
        old = time.time() - 60 * 86400
        os.utime(source, (old, old))
        vault = temp / "vault"
        destination = temp / "restored"
        command = venv_dir / ("Scripts/sessioncrate.exe" if os.name == "nt" else "bin/sessioncrate")
        common = ["--codex-home", str(source.parents[4]), "--vault", str(vault), "--json"]
        archive = _json_command(command, [*common, "archive", "--execute"], cwd=temp, env=environment)
        if archive.get("errors") or not archive.get("archived"):
            raise RuntimeError("synthetic archive did not create an archive")
        listed = _json_command(command, [*common, "list"], cwd=temp, env=environment)
        archives = listed.get("archives", [])
        if len(archives) != 1:
            raise RuntimeError(f"expected one synthetic archive, got {len(archives)}")
        archive_id = archives[0]["archive_id"]
        search = _json_command(command, [*common, "search", "한글 검색"], cwd=temp, env=environment)
        if not search.get("matches"):
            raise RuntimeError("synthetic search found no match")
        prune = _json_command(command, [*common, "prune"], cwd=temp, env=environment)
        if prune.get("errors"):
            raise RuntimeError(f"synthetic prune preview failed: {prune['errors']}")
        restore = _json_command(
            command,
            [*common, "restore", archive_id, "--destination-home", str(destination), "--execute"],
            cwd=temp,
            env=environment,
        )
        if restore.get("errors"):
            raise RuntimeError(f"synthetic restore failed: {restore['errors']}")
        restored = destination / source.relative_to(source.parents[4])
        if not restored.is_file() or restored.read_bytes() != raw:
            raise RuntimeError("synthetic restored bytes do not match source bytes")

        export_source = temp / "explicit-export"
        export_source.mkdir()
        export_file = export_source / "chat.md"
        export_raw = "# SessionCrate export smoke 한글 검색\n\nexport bytes\n".encode("utf-8")
        export_file.write_bytes(export_raw)
        os.utime(export_file, (old, old))
        export_destination = temp / "restored-export"
        export_common = [
            "--source-root",
            str(export_source),
            "--tool",
            "export",
            "--vault",
            str(vault),
            "--json",
        ]
        export_archive = _json_command(
            command,
            [*export_common, "archive", "--execute"],
            cwd=temp,
            env=environment,
        )
        if export_archive.get("errors") or not export_archive.get("archived"):
            raise RuntimeError("synthetic export archive did not create an archive")
        export_listing = _json_command(command, [*export_common, "list"], cwd=temp, env=environment)
        export_archives = [item for item in export_listing.get("archives", []) if item.get("tool") == "export"]
        if len(export_archives) != 1:
            raise RuntimeError(f"expected one synthetic export archive, got {len(export_archives)}")
        export_archive_id = export_archives[0]["archive_id"]
        export_search = _json_command(
            command,
            [*export_common, "search", "export bytes"],
            cwd=temp,
            env=environment,
        )
        if not export_search.get("matches"):
            raise RuntimeError("synthetic export search found no match")
        export_restore = _json_command(
            command,
            [
                *export_common,
                "restore",
                export_archive_id,
                "--destination-home",
                str(export_destination),
                "--execute",
            ],
            cwd=temp,
            env=environment,
        )
        if export_restore.get("errors"):
            raise RuntimeError(f"synthetic export restore failed: {export_restore['errors']}")
        restored_export = export_destination / "chat.md"
        if not restored_export.is_file() or restored_export.read_bytes() != export_raw:
            raise RuntimeError("synthetic export restored bytes do not match source bytes")

        evidence = _wheel_evidence(wheel)
        if sdist is not None:
            evidence.update(_sdist_evidence(sdist))
        evidence.update({
            "imported_from": str(imported),
            "archive_id_prefix": archive_id[:12],
            "search_matches": len(search["matches"]),
            "restore_bytes": len(raw),
            "export_archive_id_prefix": export_archive_id[:12],
            "export_search_matches": len(export_search["matches"]),
            "export_restore_bytes": len(export_raw),
            "prune_preview": True,
        })
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
