from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import stat
import tarfile
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_vault import bundles
from codex_vault.storage import VaultError


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="codex-vault bundles ")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.source = self.root / "tool export with spaces"
        self.source.mkdir()
        self.vault = self.root / "private vault"
        self._groups: list[SimpleNamespace] = []

        self.discover_patch = mock.patch.object(bundles.sources, "discover", side_effect=self._discover)
        self.discover_patch.start()
        self.addCleanup(self.discover_patch.stop)

    def _discover(self, tool: str, source_root: Path) -> list[SimpleNamespace]:
        return [group for group in self._groups if group.tool == tool and group.source_root == source_root]

    def _age_selected_directories(self, root_entries: tuple[str, ...], mtime_ns: int) -> None:
        """Age only directory roots owned by a synthetic source group."""

        for relative in root_entries:
            root = self.source / relative
            if root.is_symlink() or not root.is_dir():
                continue
            for current, _directories, _files in os.walk(root, followlinks=False):
                path = Path(current)
                if path.is_symlink():
                    continue
                os.utime(path, ns=(mtime_ns, mtime_ns))

    def _group(
        self,
        tool: str,
        session_id: str,
        files: dict[str, bytes],
        *,
        formats: dict[str, str] | None = None,
        source_mode: str = "native",
        root_entries: tuple[str, ...] | None = None,
    ) -> SimpleNamespace:
        for relative, data in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            old = time.time_ns() - 2 * 86_400 * 1_000_000_000
            os.utime(path, ns=(old, old))
        group = SimpleNamespace(
            tool=tool,
            source_root=self.source,
            session_id=session_id,
            files=tuple(files),
            root_entries=root_entries or tuple(files),
            formats=formats or {relative: "jsonl" for relative in files},
            cwd="/synthetic/project",
            source_mode=source_mode,
            removable=True,
            notes=(),
        )
        self._groups.append(group)
        self._age_selected_directories(group.root_entries, old)
        return group

    def _replace_manifest(self, manifest: dict[str, object]) -> dict[str, object]:
        original_id = str(manifest["archive_id"])
        replacement = dict(manifest)
        replacement["archive_id"] = bundles._compute_archive_id(replacement)
        manifests = self.vault / "bundles/manifests"
        target = manifests / f"{replacement['archive_id']}.json"
        target.write_text(json.dumps(replacement), encoding="utf-8")
        original = manifests / f"{original_id}.json"
        if original != target:
            original.unlink()
        return replacement

    def test_claude_companions_roundtrip_and_search(self) -> None:
        files = {
            "projects/demo/session-a.jsonl": (
                json.dumps({"type": "user", "text": "한국어 검색어"}, ensure_ascii=False).encode() + b"\n"
            ),
            "projects/demo/session-a/subagents/worker.jsonl": b'{"type":"assistant","text":"companion output"}\n',
            "projects/demo/session-a/tool-results/result.txt": b"tool result companion\n",
        }
        self._group(
            "claude",
            "session-a",
            files,
            formats={
                "projects/demo/session-a.jsonl": "claude-jsonl",
                "projects/demo/session-a/subagents/worker.jsonl": "claude-jsonl",
                "projects/demo/session-a/tool-results/result.txt": "text",
            },
            root_entries=("projects/demo/session-a.jsonl", "projects/demo/session-a"),
        )

        archived = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        self.assertEqual(len(archived["archived"]), 1)
        manifest = bundles.list_archives(self.vault, tool="claude")[0]
        self.assertEqual(manifest["source_mode"], "native")
        self.assertEqual({item["path"] for item in manifest["files"]}, set(files))
        self.assertTrue(bundles.verify(self.vault, manifest["archive_id"])["ok"])

        result = bundles.search(self.vault, "검색어", tool="claude")
        self.assertEqual(result["matches"][0]["member_path"], "projects/demo/session-a.jsonl")
        self.assertEqual(result["matches"][0]["session_id"], "session-a")

        for relative in files:
            (self.source / relative).unlink()
        restored = self.root / "restored"
        result = bundles.restore(self.vault, manifest["archive_id"], restored, execute=True)
        self.assertTrue(result["ok"])
        for relative, data in files.items():
            self.assertEqual((restored / relative).read_bytes(), data)

    def test_glm_and_agy_exports_are_explicit_and_tagged(self) -> None:
        glm = self._group(
            "glm",
            "glm-export-1",
            {"glm/session.json": b'{"info":{"model":{"modelID":"glm-5.3"}},"messages":[]}\n'},
            formats={"glm/session.json": "json"},
            source_mode="export",
            root_entries=("glm/session.json",),
        )
        agy = self._group(
            "agy",
            "agy-export-1",
            {"agy/session.md": "# AGY export\n\nKorean 검색 테스트\n".encode("utf-8")},
            formats={"agy/session.md": "text"},
            source_mode="export",
            root_entries=("agy/session.md",),
        )

        bundles.archive("glm", self.source, self.vault, older_than_days=1, execute=True)
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        rows = bundles.list_archives(self.vault)
        self.assertEqual({row["tool"] for row in rows}, {"glm", "agy"})
        self.assertTrue(all(row["source_mode"] == "export" for row in rows))
        self.assertEqual({row["session_id"] for row in rows}, {glm.session_id, agy.session_id})

    def test_corrupt_blob_and_manifest_traversal_refuse_without_source_loss(self) -> None:
        self._group(
            "opencode",
            "session-1",
            {"exports/session.json": b'{"info":{},"messages":[]}\n'},
            formats={"exports/session.json": "json"},
            source_mode="export",
        )
        bundles.archive("opencode", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        blob = self.vault / manifest["blob"]
        blob.write_bytes(blob.read_bytes()[:-3])
        self.assertFalse(bundles.verify(self.vault, manifest["archive_id"])["ok"])
        with self.assertRaises(VaultError):
            bundles.restore(self.vault, manifest["archive_id"], self.root / "restore", execute=True)
        self.assertTrue((self.source / "exports/session.json").exists())

        injected = self.vault / "bundles/manifests" / ("f" * 64 + ".json")
        injected.write_text(json.dumps({**manifest, "archive_id": "f" * 64, "files": [{"path": "../secret", "sha256": "0" * 64, "size": 1, "mode": 0o600, "mtime_ns": 1}]}))
        with self.assertRaises(VaultError):
            bundles.list_archives(self.vault)

    def test_restore_conflict_preflight_writes_nothing_else(self) -> None:
        files = {"sessions/cwd/session-2/a.txt": b"a\n", "sessions/cwd/session-2/b.txt": b"b\n"}
        self._group(
            "grok",
            "session-2",
            files,
            formats={path: "text" for path in files},
            root_entries=("sessions/cwd/session-2",),
        )
        bundles.archive("grok", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore"
        conflict = destination / "sessions/cwd/session-2/b.txt"
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"different")

        with self.assertRaises(VaultError):
            bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertFalse((destination / "sessions/cwd/session-2/a.txt").exists())
        self.assertEqual(conflict.read_bytes(), b"different")

    def test_prune_requires_whole_group_inactive_and_preserves_active_group(self) -> None:
        files = {"projects/demo/session-b.jsonl": b"hello\n", "projects/demo/session-b/worker.jsonl": b"worker\n"}
        self._group("claude", "session-b", files, formats={path: "text" for path in files}, root_entries=("projects/demo/session-b.jsonl", "projects/demo/session-b"))
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        with mock.patch.object(bundles.activity, "probe_session_tree", return_value={"state": "active", "reason": "synthetic active"}, create=True), mock.patch.object(
            bundles.activity, "probe_session_file", return_value={"state": "active", "reason": "synthetic active"}
        ):
            result = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertFalse(result["removed"])
        self.assertTrue((self.source / "projects/demo/session-b.jsonl").exists())
        self.assertTrue((self.source / "projects/demo/session-b/worker.jsonl").exists())

    def test_empty_directory_metadata_roundtrips(self) -> None:
        files = {"projects/demo/session-c.jsonl": b"root\n"}
        self._group("claude", "session-c", files, formats={"projects/demo/session-c.jsonl": "text"}, root_entries=("projects/demo/session-c.jsonl", "projects/demo/session-c"))
        empty = self.source / "projects/demo/session-c" / "empty-child"
        empty.mkdir(parents=True)
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        os.chmod(empty, 0o750)
        os.utime(empty, ns=(old, old))
        (self.source / "projects/demo/session-c").mkdir(exist_ok=True)
        os.utime(self.source / "projects/demo/session-c", ns=(old, old))

        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        directory_paths = {item["path"] for item in manifest["directories"]}
        self.assertIn("projects/demo/session-c", directory_paths)
        self.assertIn("projects/demo/session-c/empty-child", directory_paths)
        shutil = __import__("shutil")
        shutil.rmtree(self.source / "projects/demo/session-c")
        destination = self.root / "directory restore"
        bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        restored_empty = destination / "projects/demo/session-c/empty-child"
        self.assertTrue(restored_empty.is_dir())
        self.assertEqual(stat.S_IMODE(restored_empty.stat().st_mode), 0o750)

    def test_unexpected_quarantine_member_is_preserved(self) -> None:
        files = {"projects/demo/session-d.jsonl": b"hello\n", "projects/demo/session-d/worker.jsonl": b"worker\n"}
        self._group("claude", "session-d", files, formats={path: "text" for path in files}, root_entries=("projects/demo/session-d.jsonl", "projects/demo/session-d"))
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        calls = {"n": 0}

        def probe(path: Path) -> dict[str, str]:
            calls["n"] += 1
            if path.name.startswith(".codex-vault-pending-bundle-"):
                (path / "unexpected.txt").write_text("unexpected")
            return {"state": "inactive", "reason": "synthetic inactive"}

        with mock.patch.object(bundles.activity, "probe_session_tree", side_effect=probe, create=True), mock.patch.object(
            bundles.activity, "probe_session_file", side_effect=probe
        ):
            result = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertTrue((self.source / "projects/demo/session-d.jsonl").exists())
        self.assertTrue(list(self.source.glob("projects/demo/.codex-vault-pending-bundle-*")))
        self.assertTrue(result["skipped"])

    def test_reappeared_original_preserves_all_quarantined_roots(self) -> None:
        files = {"projects/demo/session-e.jsonl": b"hello\n", "projects/demo/session-e/worker.jsonl": b"worker\n"}
        self._group("claude", "session-e", files, formats={path: "text" for path in files}, root_entries=("projects/demo/session-e.jsonl", "projects/demo/session-e"))
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        calls = {"n": 0}

        def probe(path: Path) -> dict[str, str]:
            calls["n"] += 1
            if path.name.startswith(".codex-vault-pending-bundle-"):
                (self.source / "projects/demo/session-e.jsonl").write_bytes(b"new original")
            return {"state": "inactive", "reason": "synthetic inactive"}

        with mock.patch.object(bundles.activity, "probe_session_tree", side_effect=probe, create=True), mock.patch.object(
            bundles.activity, "probe_session_file", side_effect=probe
        ):
            result = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertFalse(result["removed"])
        self.assertEqual((self.source / "projects/demo/session-e.jsonl").read_bytes(), b"new original")
        self.assertTrue(list(self.source.glob("projects/demo/.codex-vault-pending-bundle-*")))

    def test_corrupt_existing_manifest_blocks_rerun_before_prune(self) -> None:
        relative = "projects/demo/session-f.jsonl"
        self._group("claude", "session-f", {relative: b"preserve me\n"}, formats={relative: "text"})
        first = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        archive_id = first["archived"][0]["archive_id"]
        manifest_path = self.vault / "bundles/manifests" / f"{archive_id}.json"
        manifest_path.write_text("{}\n", encoding="utf-8")

        with mock.patch.object(bundles.activity, "probe_session_tree", return_value={"state": "inactive", "reason": "synthetic inactive"}), mock.patch.object(
            bundles.activity, "probe_session_file", return_value={"state": "inactive", "reason": "synthetic inactive"}
        ):
            with self.assertRaises(VaultError):
                bundles.archive(
                    "claude",
                    self.source,
                    self.vault,
                    older_than_days=1,
                    execute=True,
                    remove_originals=True,
                )
        self.assertEqual((self.source / relative).read_bytes(), b"preserve me\n")

    def test_restore_rejects_different_destination_created_during_publish(self) -> None:
        relative = "export/session.txt"
        self._group("agy", "race", {relative: b"archived bytes\n"}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore-race"
        real_publish = bundles.storage._publish_temp_no_replace

        def race_publish(temp: Path, target: Path, *, mode: int | None = 0o600) -> bool:
            if temp.name.startswith(".bundle-restore-"):
                target.write_bytes(b"racing different bytes\n")
                temp.unlink()
                return False
            return real_publish(temp, target, mode=mode)

        with mock.patch.object(bundles.storage, "_publish_temp_no_replace", side_effect=race_publish):
            with self.assertRaises(VaultError):
                bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertEqual((destination / relative).read_bytes(), b"racing different bytes\n")

    def test_restore_equal_destination_created_during_publish_is_skipped(self) -> None:
        relative = "export/equal.txt"
        payload = b"archived bytes\n"
        self._group("agy", "equal-race", {relative: payload}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore-equal-race"
        real_publish = bundles.storage._publish_temp_no_replace

        def race_publish(temp: Path, target: Path, *, mode: int | None = 0o600) -> bool:
            if temp.name.startswith(".bundle-restore-"):
                target.write_bytes(payload)
                temp.unlink()
                return False
            return real_publish(temp, target, mode=mode)

        with mock.patch.object(bundles.storage, "_publish_temp_no_replace", side_effect=race_publish):
            result = bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["skipped"], [relative])

    def test_restore_reuses_verified_identity_for_equal_export_files(self) -> None:
        relative = "export/reuse.txt"
        payload = b"archived bytes\n"
        self._group("agy", "reuse-export", {relative: payload}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore-reuse-export"
        target = destination / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        real_hash = bundles.storage._hash_path
        with mock.patch.object(bundles.storage, "_hash_path", wraps=real_hash) as hashed:
            result = bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["skipped"], [relative])
        self.assertEqual(hashed.call_count, 1)

    def test_restore_reuses_verified_identity_for_equal_native_files(self) -> None:
        files = {
            "projects/demo/session-reuse.jsonl": b"a\n",
            "projects/demo/session-reuse/worker.jsonl": b"b\n",
        }
        self._group(
            "claude",
            "session-reuse",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-reuse.jsonl", "projects/demo/session-reuse"),
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore-reuse-native"
        for relative, payload in files.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        real_hash = bundles.storage._hash_path
        with mock.patch.object(bundles.storage, "_hash_path", wraps=real_hash) as hashed:
            result = bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertEqual(result["restored"], [])
        self.assertEqual(set(result["skipped"]), set(files))
        self.assertEqual(hashed.call_count, len(files))

    def test_restore_rehashes_mutated_equal_file_even_when_mtime_is_restored(self) -> None:
        relative = "export/mutated-after-preflight.txt"
        payload = b"payload\n"
        self._group("agy", "mutated-preflight", {relative: payload}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore-mutated-preflight"
        target = destination / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        original_mtime = target.stat().st_mtime_ns
        real_preflight = bundles._restore_preflight
        calls = 0

        def mutate_after_second_preflight(
            root: Path,
            checked_manifest: dict[str, object],
            trusted_files: dict[str, tuple[int, int, int, int, int, int]] | None = None,
        ) -> dict[str, str]:
            nonlocal calls
            result = real_preflight(root, checked_manifest, trusted_files)
            calls += 1
            if calls == 2:
                target.write_bytes(b"changed\n")
                os.utime(target, ns=(original_mtime, original_mtime))
            return result

        with mock.patch.object(bundles, "_restore_preflight", side_effect=mutate_after_second_preflight):
            with self.assertRaises(VaultError):
                bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertEqual(calls, 2)
        self.assertEqual(target.read_bytes(), b"changed\n")

    def test_restore_reads_tar_once_for_multiple_members(self) -> None:
        files = {f"sessions/cwd/stream-once/{name}.txt": f"{name}\n".encode() for name in ("a", "b", "c")}
        self._group(
            "grok",
            "stream-once",
            files,
            formats={path: "text" for path in files},
            root_entries=("sessions/cwd/stream-once",),
        )
        bundles.archive("grok", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        real_open = bundles.tarfile.open
        opened = 0

        def counted_open(*args: object, **kwargs: object) -> tarfile.TarFile:
            nonlocal opened
            opened += 1
            return real_open(*args, **kwargs)

        with mock.patch.object(bundles.tarfile, "open", side_effect=counted_open):
            bundles.restore(self.vault, manifest["archive_id"], self.root / "restore-once", execute=True)
        self.assertEqual(opened, 1)

    def test_tar_metadata_mismatch_is_rejected(self) -> None:
        relative = "export/metadata.txt"
        self._group("agy", "metadata", {relative: b"payload\n"}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        old_blob = self.vault / manifest["blob"]

        raw_tar = io.BytesIO()
        with gzip.open(old_blob, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r|") as archive:
            member = next(iter(archive))
            payload = archive.extractfile(member).read()
        with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            changed = tarfile.TarInfo(relative)
            changed.size = len(payload)
            changed.mode = 0o777
            changed.mtime = member.mtime
            archive.addfile(changed, io.BytesIO(payload))
        encoded = io.BytesIO()
        with gzip.GzipFile(fileobj=encoded, mode="wb", mtime=0) as compressed:
            compressed.write(raw_tar.getvalue())
        blob_bytes = encoded.getvalue()
        blob_sha = hashlib.sha256(blob_bytes).hexdigest()
        new_blob = self.vault / "bundles/blobs" / f"{blob_sha}.tar.gz"
        new_blob.write_bytes(blob_bytes)
        manifest["blob"] = f"bundles/blobs/{blob_sha}.tar.gz"
        manifest["blob_sha256"] = blob_sha
        manifest["compressed_bytes"] = len(blob_bytes)
        replacement = self._replace_manifest(manifest)

        self.assertFalse(bundles.verify(self.vault, str(replacement["archive_id"]))["ok"])

    def test_manifest_root_must_be_an_actual_strict_session_root(self) -> None:
        relative = "projects/demo/session-g.jsonl"
        self._group("claude", "session-g", {relative: b"payload\n"}, formats={relative: "text"})
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        manifest["root_entries"] = ["projects/demo"]
        self._replace_manifest(manifest)

        with self.assertRaises(VaultError):
            bundles.list_archives(self.vault)

    def test_manifest_unhashable_format_is_reported_as_vault_error(self) -> None:
        relative = "export/format.txt"
        self._group("agy", "format", {relative: b"payload\n"}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        manifest["files"][0]["format"] = []
        self._replace_manifest(manifest)

        with self.assertRaises(VaultError):
            bundles.list_archives(self.vault)

    def test_native_restore_rejects_partial_existing_session_root(self) -> None:
        files = {
            "projects/demo/session-h.jsonl": b"main\n",
            "projects/demo/session-h/one.txt": b"one\n",
            "projects/demo/session-h/two.txt": b"two\n",
        }
        self._group(
            "claude",
            "session-h",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-h.jsonl", "projects/demo/session-h"),
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "partial-native"
        existing = destination / "projects/demo/session-h/one.txt"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(files["projects/demo/session-h/one.txt"])

        with self.assertRaises(VaultError):
            bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertFalse((destination / "projects/demo/session-h.jsonl").exists())
        self.assertFalse((destination / "projects/demo/session-h/two.txt").exists())

    def test_filename_session_id_is_not_replaced_by_nested_message_id(self) -> None:
        relative = "projects/demo/session-i.jsonl"
        self._group(
            "claude",
            "session-i",
            {relative: b'{"id":"unrelated-message-id","text":"hello"}\n'},
            formats={relative: "claude-jsonl"},
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        self.assertEqual(bundles.list_archives(self.vault)[0]["session_id"], "session-i")

    def test_inactive_claude_group_prunes_and_restores_with_empty_directories(self) -> None:
        files = {
            "projects/demo/session-j.jsonl": b"main\n",
            "projects/demo/session-j/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-j",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-j.jsonl", "projects/demo/session-j"),
        )
        empty = self.source / "projects/demo/session-j/empty"
        empty.mkdir(parents=True)
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        os.chmod(empty, 0o750)
        os.utime(empty, ns=(old, old))
        os.utime(empty.parent, ns=(old, old))

        with mock.patch.object(bundles.activity, "probe_session_tree", return_value={"state": "inactive", "reason": "synthetic inactive"}), mock.patch.object(
            bundles.activity, "probe_session_file", return_value={"state": "inactive", "reason": "synthetic inactive"}
        ):
            result = bundles.archive(
                "claude",
                self.source,
                self.vault,
                older_than_days=1,
                execute=True,
                remove_originals=True,
            )
        manifest = result["archived"][0]
        self.assertEqual(result["removed"], [manifest["archive_id"]])
        self.assertFalse((self.source / "projects/demo/session-j.jsonl").exists())
        self.assertFalse((self.source / "projects/demo/session-j").exists())

        bundles.restore(self.vault, manifest["archive_id"], self.source, execute=True)
        for relative, payload in files.items():
            self.assertEqual((self.source / relative).read_bytes(), payload)
        self.assertTrue(empty.is_dir())
        self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o750)

    def test_fresh_empty_claude_companion_blocks_prune_until_aged(self) -> None:
        files = {
            "projects/demo/session-fresh.jsonl": b"main\n",
            "projects/demo/session-fresh/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-fresh",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-fresh.jsonl", "projects/demo/session-fresh"),
        )
        empty = self.source / "projects/demo/session-fresh/empty-companion"
        empty.mkdir()
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        fresh = time.time_ns() - bundles._HOUR_NS // 2
        os.utime(empty, ns=(fresh, fresh))
        os.utime(empty.parent, ns=(old, old))

        scanned = bundles.scan("claude", self.source, older_than_days=1)
        self.assertFalse(scanned["candidates"][0]["eligible"])
        self.assertEqual(scanned["candidates"][0]["reason"], "too_new")

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_tree", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_file", return_value=inactive
        ):
            archived = bundles.archive(
                "claude",
                self.source,
                self.vault,
                older_than_days=0,
                execute=True,
                remove_originals=True,
            )
        manifest = archived["archived"][0]
        self.assertEqual(archived["removed"], [])
        self.assertTrue(empty.is_dir())

        with mock.patch.object(bundles.activity, "probe_session_tree", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_file", return_value=inactive
        ):
            preview = bundles.prune(self.vault, manifest["archive_id"], execute=False)
            executed = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertFalse(preview["candidates"][0]["eligible"])
        self.assertIn("less than one hour", preview["candidates"][0]["reason"])
        self.assertEqual(executed["removed"], [])
        self.assertTrue(empty.is_dir())

        with mock.patch.object(bundles.time, "time_ns", return_value=fresh + bundles._HOUR_NS + 1), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ), mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive):
            aged = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertEqual(aged["removed"], [manifest["archive_id"]])
        self.assertFalse((self.source / "projects/demo/session-fresh.jsonl").exists())
        self.assertFalse((self.source / "projects/demo/session-fresh").exists())

    def test_fresh_nested_grok_directory_blocks_prune_until_aged(self) -> None:
        files = {"sessions/cwd/session-fresh/updates.jsonl": b"updates\n"}
        self._group(
            "grok",
            "session-fresh",
            files,
            formats={path: "text" for path in files},
            root_entries=("sessions/cwd/session-fresh",),
        )
        nested = self.source / "sessions/cwd/session-fresh/tool-results/empty"
        nested.mkdir(parents=True)
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        fresh = time.time_ns() - bundles._HOUR_NS // 2
        os.utime(nested, ns=(fresh, fresh))
        os.utime(nested.parent, ns=(old, old))
        os.utime(nested.parent.parent, ns=(old, old))

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_tree", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_file", return_value=inactive
        ):
            archived = bundles.archive(
                "grok",
                self.source,
                self.vault,
                older_than_days=0,
                execute=True,
                remove_originals=True,
            )
        manifest = archived["archived"][0]
        self.assertEqual(archived["removed"], [])
        self.assertTrue(nested.is_dir())

        with mock.patch.object(bundles.activity, "probe_session_tree", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_file", return_value=inactive
        ):
            preview = bundles.prune(self.vault, manifest["archive_id"], execute=False)
            executed = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertFalse(preview["candidates"][0]["eligible"])
        self.assertIn("less than one hour", preview["candidates"][0]["reason"])
        self.assertEqual(executed["removed"], [])
        self.assertTrue(nested.is_dir())

        with mock.patch.object(bundles.time, "time_ns", return_value=fresh + bundles._HOUR_NS + 1), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ), mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive):
            aged = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertEqual(aged["removed"], [manifest["archive_id"]])
        self.assertFalse((self.source / "sessions/cwd/session-fresh").exists())

    def test_prune_rechecks_all_quarantined_identities_before_first_unlink(self) -> None:
        files = {
            "projects/demo/session-k.jsonl": b"main\n",
            "projects/demo/session-k/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-k",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-k.jsonl", "projects/demo/session-k"),
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        real_verify = bundles._verify_quarantine_members

        def mutate_after_verify(quarantine: Path, checked_manifest: dict[str, object]) -> object:
            identities = real_verify(quarantine, checked_manifest)
            staged = quarantine / "session-k" / "worker.txt"
            staged.write_bytes(b"changed after verification\n")
            return identities

        with mock.patch.object(bundles.activity, "probe_session_tree", return_value={"state": "inactive", "reason": "synthetic inactive"}), mock.patch.object(
            bundles.activity, "probe_session_file", return_value={"state": "inactive", "reason": "synthetic inactive"}
        ), mock.patch.object(
            bundles, "_verify_quarantine_members", side_effect=mutate_after_verify
        ):
            result = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertEqual(
            (self.source / "projects/demo/session-k/worker.txt").read_bytes(),
            b"changed after verification\n",
        )
        self.assertTrue(result["skipped"])

    def test_archive_rechecks_early_members_after_whole_snapshot(self) -> None:
        files = {
            "sessions/cwd/session-l/a.txt": b"first\n",
            "sessions/cwd/session-l/b.txt": b"second\n",
        }
        self._group(
            "grok",
            "session-l",
            files,
            formats={path: "text" for path in files},
            root_entries=("sessions/cwd/session-l",),
        )
        real_capture = bundles._capture_directories
        calls = 0

        def mutate_during_final_walk(root: Path, group: object, file_paths: set[str]) -> list[dict[str, object]]:
            nonlocal calls
            calls += 1
            result = real_capture(root, group, file_paths)
            # Candidate eligibility now captures directory metadata before
            # the archive snapshot, so the snapshot's final walk is call 3.
            if calls == 3:
                (self.source / "sessions/cwd/session-l/a.txt").write_bytes(b"changed late\n")
            return result

        with mock.patch.object(bundles, "_capture_directories", side_effect=mutate_during_final_walk):
            result = bundles.archive("grok", self.source, self.vault, older_than_days=1, execute=True)
        self.assertEqual(bundles.list_archives(self.vault), [])
        self.assertIn("source changed", result["skipped"][0]["reason"])

    def test_archive_skips_one_group_failure_and_continues_batch(self) -> None:
        self._group("agy", "broken", {"export/broken.txt": b"broken\n"}, formats={"export/broken.txt": "text"}, source_mode="export")
        self._group("agy", "healthy", {"export/healthy.txt": b"healthy\n"}, formats={"export/healthy.txt": "text"}, source_mode="export")
        real_snapshot = bundles._snapshot_bundle

        def fail_one(root: Path, group: object, blobs_dir: Path) -> object:
            if getattr(group, "session_id") == "broken":
                raise VaultError("synthetic changed group")
            return real_snapshot(root, group, blobs_dir)

        with mock.patch.object(bundles, "_snapshot_bundle", side_effect=fail_one):
            result = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)

        self.assertEqual([item["session_id"] for item in result["archived"]], ["healthy"])
        self.assertEqual(result["skipped"][0]["session_id"], "broken")
        self.assertIn("synthetic changed group", result["skipped"][0]["reason"])

    def test_native_restore_rechecks_for_new_descendants_after_staging(self) -> None:
        files = {
            "projects/demo/session-m.jsonl": b"main\n",
            "projects/demo/session-m/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-m",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-m.jsonl", "projects/demo/session-m"),
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        destination = self.root / "restore-resumed"
        real_stage = bundles._stage_restore_bundle

        def resume_during_staging(bundle_root: Path, checked_manifest: dict[str, object], destination_root: Path) -> tuple[Path, dict[str, Path]]:
            staged = real_stage(bundle_root, checked_manifest, destination_root)
            unexpected = destination / "projects/demo/session-m/unexpected.txt"
            unexpected.parent.mkdir(parents=True)
            unexpected.write_bytes(b"new tool output\n")
            return staged

        with mock.patch.object(bundles, "_stage_restore_bundle", side_effect=resume_during_staging):
            with self.assertRaises(VaultError):
                bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)
        self.assertFalse((destination / "projects/demo/session-m.jsonl").exists())
        self.assertFalse((destination / "projects/demo/session-m/worker.txt").exists())
        self.assertEqual(
            (destination / "projects/demo/session-m/unexpected.txt").read_bytes(),
            b"new tool output\n",
        )

    def test_activity_detected_after_quarantine_restores_every_root(self) -> None:
        files = {
            "projects/demo/session-n.jsonl": b"main\n",
            "projects/demo/session-n/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-n",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-n.jsonl", "projects/demo/session-n"),
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]

        def probe(path: Path) -> dict[str, str]:
            if path.name.startswith(".codex-vault-pending-bundle-"):
                return {"state": "active", "reason": "opened after rename"}
            return {"state": "inactive", "reason": "synthetic inactive"}

        with mock.patch.object(bundles.activity, "probe_session_tree", side_effect=probe), mock.patch.object(
            bundles.activity, "probe_session_file", side_effect=probe
        ):
            result = bundles.prune(self.vault, manifest["archive_id"], execute=True)
        self.assertEqual(result["removed"], [])
        self.assertTrue(any(item.get("reason") == "opened after rename" for item in result["skipped"]))
        for relative, payload in files.items():
            self.assertEqual((self.source / relative).read_bytes(), payload)
        self.assertFalse(list(self.source.glob("projects/demo/.codex-vault-pending-bundle-*")))

    def test_identical_archive_rerun_reuses_strict_published_manifest(self) -> None:
        relative = "export/idempotent.txt"
        self._group("agy", "idempotent", {relative: b"same\n"}, formats={relative: "text"}, source_mode="export")
        first = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        second = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)

        self.assertEqual(first["archived"][0], second["archived"][0])
        self.assertEqual(len(bundles.list_archives(self.vault)), 1)

    def test_prune_failure_after_first_unlink_keeps_recovery_quarantine(self) -> None:
        files = {
            "projects/demo/session-o.jsonl": b"main\n",
            "projects/demo/session-o/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-o",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-o.jsonl", "projects/demo/session-o"),
        )
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        real_unlink = bundles.os.unlink
        calls = 0

        def add_late_member(path: Path | str, *args: object, **kwargs: object) -> None:
            nonlocal calls
            real_unlink(path, *args, **kwargs)
            calls += 1
            if calls == 1:
                staged_file = Path(path)
                (staged_file.parent / "unexpected-after-unlink.txt").write_text("preserve", encoding="utf-8")

        with mock.patch.object(bundles.activity, "probe_session_tree", return_value={"state": "inactive", "reason": "synthetic inactive"}), mock.patch.object(
            bundles.activity, "probe_session_file", return_value={"state": "inactive", "reason": "synthetic inactive"}
        ), mock.patch.object(bundles.os, "unlink", side_effect=add_late_member):
            with self.assertRaises(VaultError):
                bundles.prune(self.vault, manifest["archive_id"], execute=True)

        self.assertFalse((self.source / "projects/demo/session-o.jsonl").exists())
        self.assertFalse((self.source / "projects/demo/session-o").exists())
        quarantines = list(self.source.glob("projects/demo/.codex-vault-pending-bundle-*"))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(
            (quarantines[0] / "session-o/unexpected-after-unlink.txt").read_text(encoding="utf-8"),
            "preserve",
        )
        self.assertTrue(bundles.verify(self.vault, manifest["archive_id"])["ok"])

    def test_prune_setup_failure_skips_group_and_continues_batch(self) -> None:
        for session_id in ("setup-fails", "setup-healthy"):
            relative = f"projects/demo/{session_id}.jsonl"
            self._group("claude", session_id, {relative: f"{session_id}\n".encode()}, formats={relative: "text"})
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        real_mkdir = bundles.storage._mkdir_private
        failed = False

        def fail_first_quarantine(path: Path, *args: object, **kwargs: object) -> Path:
            nonlocal failed
            if path.name.startswith(".codex-vault-pending-bundle-") and not failed:
                failed = True
                raise OSError("synthetic quarantine setup failure")
            return real_mkdir(path, *args, **kwargs)

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ), mock.patch.object(bundles.storage, "_mkdir_private", side_effect=fail_first_quarantine):
            result = bundles.prune(self.vault, execute=True, tool="claude")

        self.assertTrue(failed)
        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(len([item for item in result["skipped"] if "setup failure" in item.get("reason", "")]), 1)
        remaining = list((self.source / "projects/demo").glob("setup-*.jsonl"))
        self.assertEqual(len(remaining), 1)
        self.assertFalse(list((self.source / "projects/demo").glob(".codex-vault-pending-bundle-*")))

    def test_prune_parent_sync_failure_before_deletion_rolls_back(self) -> None:
        relative = "projects/demo/session-sync-before.jsonl"
        payload = b"preserve before deletion\n"
        self._group("claude", "session-sync-before", {relative: payload}, formats={relative: "text"})
        manifest = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        real_fsync = bundles.storage._fsync_directory
        sync_calls = 0

        def fail_first_sync(path: Path) -> None:
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 1:
                raise VaultError("synthetic parent sync failure")
            real_fsync(path)

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ), mock.patch.object(bundles.storage, "_fsync_directory", side_effect=fail_first_sync):
            result = bundles.prune(self.vault, manifest["archive_id"], execute=True)

        self.assertGreaterEqual(sync_calls, 2)
        self.assertEqual(result["removed"], [])
        self.assertEqual((self.source / relative).read_bytes(), payload)
        self.assertFalse(list((self.source / "projects/demo").glob(".codex-vault-pending-bundle-*")))

    def test_prune_parent_sync_failure_after_deletion_reports_removed_state(self) -> None:
        relative = "projects/demo/session-sync-after.jsonl"
        payload = b"removed before durability failure\n"
        self._group("claude", "session-sync-after", {relative: payload}, formats={relative: "text"})
        manifest = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        real_fsync = bundles.storage._fsync_directory
        sync_calls = 0

        def fail_second_sync(path: Path) -> None:
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                raise VaultError("synthetic post-deletion sync failure")
            real_fsync(path)

        result = {"removed": [], "skipped": [], "removed_source_bytes": 0}
        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ), mock.patch.object(bundles.storage, "_fsync_directory", side_effect=fail_second_sync):
            with self.assertRaisesRegex(VaultError, "source was removed.*durability") as raised:
                bundles._prune_one(self.vault, self.vault / "bundles", manifest, result)

        self.assertEqual(sync_calls, 2)
        self.assertNotIn("retained", str(raised.exception))
        self.assertEqual(result["removed"], [manifest["archive_id"]])
        self.assertEqual(result["removed_source_bytes"], len(payload))
        self.assertEqual(result["skipped"], [])
        self.assertFalse((self.source / relative).exists())
        self.assertFalse(list((self.source / "projects/demo").glob(".codex-vault-pending-bundle-*")))

    def test_file_rollback_never_replaces_a_raced_original(self) -> None:
        parent = self.root / "rollback-file"
        quarantine = parent / ".pending"
        quarantine.mkdir(parents=True)
        original = parent / "session.jsonl"
        staged = quarantine / "session.jsonl"
        staged.write_bytes(b"archived original\n")
        real_lstat = bundles._lstat
        injected = False

        def race(path: Path) -> os.stat_result | None:
            nonlocal injected
            result = real_lstat(path)
            if path == original and result is None and not injected:
                original.write_bytes(b"new live bytes\n")
                injected = True
            return result

        outcome = {"archive_id": "a" * 64, "skipped": []}
        with mock.patch.object(bundles, "_lstat", side_effect=race):
            restored = bundles._restore_quarantine([(original, staged)], quarantine, outcome, "synthetic rollback")

        self.assertFalse(restored)
        self.assertEqual(original.read_bytes(), b"new live bytes\n")
        self.assertEqual(staged.read_bytes(), b"archived original\n")
        self.assertTrue(any(item.get("quarantine") == str(quarantine) for item in outcome["skipped"]))

    def test_directory_rollback_never_replaces_a_raced_original(self) -> None:
        parent = self.root / "rollback-directory"
        quarantine = parent / ".pending"
        staged = quarantine / "session"
        staged.mkdir(parents=True)
        (staged / "history.txt").write_text("archived", encoding="utf-8")
        original = parent / "session"
        real_lstat = bundles._lstat
        raced_inode: int | None = None

        def race(path: Path) -> os.stat_result | None:
            nonlocal raced_inode
            result = real_lstat(path)
            if path == original and result is None and raced_inode is None:
                original.mkdir()
                raced_inode = original.stat().st_ino
            return result

        outcome = {"archive_id": "b" * 64, "skipped": []}
        with mock.patch.object(bundles, "_lstat", side_effect=race):
            restored = bundles._restore_quarantine([(original, staged)], quarantine, outcome, "synthetic rollback")

        self.assertFalse(restored)
        self.assertEqual(original.stat().st_ino, raced_inode)
        self.assertTrue(staged.is_dir())
        self.assertEqual((staged / "history.txt").read_text(encoding="utf-8"), "archived")

    def test_changed_source_is_ineligible_before_prune_rename(self) -> None:
        relative = "projects/demo/session-p.jsonl"
        self._group("claude", "session-p", {relative: b"first\n"}, formats={relative: "text"})
        bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)
        (self.source / relative).write_bytes(b"first\nresumed\n")

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ):
            preview = bundles.prune(self.vault, execute=False, tool="claude")
            with mock.patch.object(bundles.os, "rename", wraps=bundles.os.rename) as rename:
                executed = bundles.prune(self.vault, execute=True, tool="claude")

        self.assertFalse(preview["candidates"][0]["eligible"])
        self.assertIn("changed", preview["candidates"][0]["reason"])
        self.assertEqual(executed["removed"], [])
        rename.assert_not_called()
        self.assertEqual((self.source / relative).read_bytes(), b"first\nresumed\n")

    def test_historical_manifest_is_skipped_while_current_version_prunes(self) -> None:
        relative = "projects/demo/session-q.jsonl"
        source = self.source / relative
        self._group("claude", "session-q", {relative: b"version one\n"}, formats={relative: "text"})
        first = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        source.write_bytes(b"version two\n")
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        os.utime(source, ns=(old, old))
        second = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        self.assertNotEqual(first["archive_id"], second["archive_id"])

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles.activity, "probe_session_file", return_value=inactive), mock.patch.object(
            bundles.activity, "probe_session_tree", return_value=inactive
        ):
            result = bundles.prune(self.vault, execute=True, tool="claude")

        self.assertEqual(result["removed"], [second["archive_id"]])
        self.assertTrue(any(item["archive_id"] == first["archive_id"] and not item["eligible"] for item in result["candidates"]))
        self.assertFalse(source.exists())

    def test_source_changed_after_candidate_check_is_skipped_and_batch_continues(self) -> None:
        changed_relative = "projects/demo/session-race.jsonl"
        stable_relative = "projects/demo/session-stable.jsonl"
        self._group("claude", "session-race", {changed_relative: b"old\n"}, formats={changed_relative: "text"})
        self._group("claude", "session-stable", {stable_relative: b"stable\n"}, formats={stable_relative: "text"})
        archived = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"]
        changed_id = next(item["archive_id"] for item in archived if item["session_id"] == "session-race")
        stable_id = next(item["archive_id"] for item in archived if item["session_id"] == "session-stable")
        real_verify = bundles._verify_current_source
        calls: dict[str, int] = {}

        def change_after_candidate(manifest: dict[str, object]) -> object:
            archive_id = str(manifest["archive_id"])
            calls[archive_id] = calls.get(archive_id, 0) + 1
            if archive_id == changed_id and calls[archive_id] == 2:
                (self.source / changed_relative).write_bytes(b"new live bytes\n")
            return real_verify(manifest)

        inactive = {"state": "inactive", "reason": "synthetic inactive"}
        with mock.patch.object(bundles, "_verify_current_source", side_effect=change_after_candidate), mock.patch.object(
            bundles.activity, "probe_session_file", return_value=inactive
        ), mock.patch.object(bundles.activity, "probe_session_tree", return_value=inactive):
            result = bundles.prune(self.vault, execute=True, tool="claude")

        self.assertIn(stable_id, result["removed"])
        self.assertNotIn(changed_id, result["removed"])
        self.assertTrue(any(item.get("archive_id") == changed_id and "changed" in item.get("reason", "") for item in result["skipped"]))
        self.assertEqual((self.source / changed_relative).read_bytes(), b"new live bytes\n")
        self.assertFalse((self.source / stable_relative).exists())

    def test_search_reads_each_manifest_and_tar_once(self) -> None:
        for index in range(2):
            relative = f"export/search-{index}.txt"
            self._group("agy", f"search-{index}", {relative: f"needle {index}\n".encode()}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        real_read = bundles._read_manifest
        real_tar_open = bundles.tarfile.open
        manifest_reads = 0
        tar_opens = 0

        def count_manifest(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal manifest_reads
            manifest_reads += 1
            return real_read(*args, **kwargs)

        def count_tar(*args: object, **kwargs: object) -> tarfile.TarFile:
            nonlocal tar_opens
            tar_opens += 1
            return real_tar_open(*args, **kwargs)

        with mock.patch.object(bundles, "_read_manifest", side_effect=count_manifest), mock.patch.object(
            bundles.tarfile, "open", side_effect=count_tar
        ):
            result = bundles.search(self.vault, "needle", limit=10, tool="agy")

        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(manifest_reads, 2)
        self.assertEqual(tar_opens, 2)

    def test_search_limit_does_not_bypass_tail_integrity_and_filters_scan_count(self) -> None:
        selected = self._group(
            "agy",
            "selected",
            {"export/selected.txt": b"needle one\nneedle two\n"},
            formats={"export/selected.txt": "text"},
            source_mode="export",
        )
        selected.cwd = "/chosen/project"
        other = self._group(
            "agy",
            "other",
            {"export/other.txt": b"needle other\n"},
            formats={"export/other.txt": "text"},
            source_mode="export",
        )
        other.cwd = "/different/project"
        archived = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        selected_manifest = next(item for item in archived["archived"] if item["session_id"] == "selected")

        clean = bundles.search(self.vault, "needle", limit=1, project="chosen", tool="agy")
        self.assertEqual(clean["scanned_archives"], 1)
        self.assertTrue(clean["truncated"])

        blob = self.vault / selected_manifest["blob"]
        with blob.open("ab") as stream:
            stream.write(b"corrupt compressed tail")
        with self.assertRaises(VaultError):
            bundles.search(self.vault, "needle", limit=1, project="chosen", tool="agy")

    def test_invalid_deflate_is_normalized_to_vault_error(self) -> None:
        relative = "export/invalid-deflate.txt"
        self._group("glm", "invalid-deflate", {relative: b"searchable\n"}, formats={relative: "text"}, source_mode="export")
        manifest = bundles.archive("glm", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        blob = self.vault / manifest["blob"]
        invalid_gzip_tail = bytes.fromhex("1f8b08000000000002ffffffff0000000000000000")
        changed_bytes = blob.read_bytes() + invalid_gzip_tail
        blob.write_bytes(changed_bytes)
        changed = dict(manifest)
        changed["compressed_bytes"] = len(changed_bytes)
        changed["blob_sha256"] = hashlib.sha256(changed_bytes).hexdigest()

        with self.assertRaisesRegex(VaultError, "bundle tar.gz is corrupt") as raised:
            bundles._stream_verified_blob(blob, changed)

        self.assertIsInstance(raised.exception.__cause__, zlib.error)

    def test_text_decoder_value_error_is_not_mislabeled_as_archive_corruption(self) -> None:
        relative = "export/decoder.jsonl"
        self._group(
            "glm",
            "decoder-failure",
            {relative: b'{"text":"safe archive bytes"}\n'},
            formats={relative: "generic-jsonl"},
            source_mode="export",
        )
        manifest = bundles.archive("glm", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]

        with mock.patch.object(
            bundles.sources,
            "iter_text_records",
            side_effect=ValueError("private record contents must not be reported"),
        ):
            with self.assertRaisesRegex(VaultError, "^bundle text record decoding/processing failed$") as raised:
                bundles.search(self.vault, "safe", tool="glm")

        self.assertNotIn("private record", str(raised.exception))
        self.assertNotIn("tar.gz", str(raised.exception))
        self.assertTrue(bundles.verify(self.vault, manifest["archive_id"])["ok"])

    def test_text_callback_value_error_is_sanitized(self) -> None:
        relative = "export/callback.txt"
        self._group("glm", "callback-failure", {relative: b"safe archive bytes\n"}, formats={relative: "text"}, source_mode="export")
        manifest = bundles.archive("glm", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        blob = self.vault / manifest["blob"]

        def fail_callback(_entry: dict[str, object], _record: dict[str, object]) -> None:
            raise ValueError("private callback contents must not be reported")

        with self.assertRaisesRegex(VaultError, "^bundle text record decoding/processing failed$") as raised:
            bundles._stream_verified_blob(blob, manifest, record_callback=fail_callback)

        self.assertNotIn("private callback", str(raised.exception))

    def test_verified_search_counts_binary_members_as_skipped(self) -> None:
        files = {
            "sessions/cwd/binary-session/updates.jsonl": b'{"text":"needle"}\n',
            "sessions/cwd/binary-session/attachment.bin": b"\x00\xffbinary",
        }
        self._group(
            "grok",
            "binary-session",
            files,
            formats={
                "sessions/cwd/binary-session/updates.jsonl": "grok-acp-jsonl",
                "sessions/cwd/binary-session/attachment.bin": "binary",
            },
            root_entries=("sessions/cwd/binary-session",),
        )
        bundles.archive("grok", self.source, self.vault, older_than_days=1, execute=True)

        result = bundles.search(self.vault, "needle", tool="grok")

        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["skipped_records"], 1)

    def test_search_unicode_casefold_snippet_keeps_the_match(self) -> None:
        relative = "export/unicode.txt"
        payload = (("ß" * 100) + "TARGET suffix\n").encode("utf-8")
        self._group("agy", "unicode", {relative: payload}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)

        result = bundles.search(self.vault, "target", tool="agy")

        self.assertIn("TARGET", result["matches"][0]["snippet"])

    def test_show_exact_limit_is_not_truncated_and_still_verifies_full_blob(self) -> None:
        relative = "export/show.txt"
        self._group("agy", "show", {relative: b"first\nsecond\n"}, formats={relative: "text"}, source_mode="export")
        manifest = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]

        result = bundles.show(self.vault, manifest["archive_id"], limit=2)

        self.assertEqual(len(result["records"]), 2)
        self.assertFalse(result["truncated"])

    def test_restore_readonly_directory_applies_mode_after_files(self) -> None:
        files = {
            "projects/demo/session-r.jsonl": b"main\n",
            "projects/demo/session-r/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-r",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-r.jsonl", "projects/demo/session-r"),
        )
        sidecar = self.source / "projects/demo/session-r"
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        os.chmod(sidecar, 0o500)
        os.utime(sidecar, ns=(old, old))
        manifest = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        destination = self.root / "readonly-restore"

        bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)

        restored = destination / "projects/demo/session-r"
        self.assertEqual((restored / "worker.txt").read_bytes(), b"worker\n")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o500)

    def test_restore_write_only_file_keeps_temp_readable_until_publish(self) -> None:
        relative = "export/write-only.txt"
        self._group("agy", "write-only", {relative: b"payload\n"}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)
        manifest = bundles.list_archives(self.vault)[0]
        old_blob = self.vault / manifest["blob"]
        raw_tar = io.BytesIO()
        with gzip.open(old_blob, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r|") as archive:
            member = next(iter(archive))
            payload = archive.extractfile(member).read()
        with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            changed = tarfile.TarInfo(relative)
            changed.size = len(payload)
            changed.mode = 0o200
            changed.mtime = member.mtime
            changed.pax_headers = dict(member.pax_headers)
            archive.addfile(changed, io.BytesIO(payload))
        encoded = io.BytesIO()
        with gzip.GzipFile(fileobj=encoded, mode="wb", mtime=0) as compressed:
            compressed.write(raw_tar.getvalue())
        blob_bytes = encoded.getvalue()
        blob_sha = hashlib.sha256(blob_bytes).hexdigest()
        (self.vault / "bundles/blobs" / f"{blob_sha}.tar.gz").write_bytes(blob_bytes)
        manifest["files"][0]["mode"] = 0o200
        manifest["blob"] = f"bundles/blobs/{blob_sha}.tar.gz"
        manifest["blob_sha256"] = blob_sha
        manifest["compressed_bytes"] = len(blob_bytes)
        replacement = self._replace_manifest(manifest)
        destination = self.root / "write-only-restore"

        bundles.restore(self.vault, str(replacement["archive_id"]), destination, execute=True)

        restored = destination / relative
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o200)

    def test_restore_failure_removes_files_published_by_this_attempt(self) -> None:
        files = {
            "projects/demo/session-s.jsonl": b"main\n",
            "projects/demo/session-s/one.txt": b"one\n",
            "projects/demo/session-s/two.txt": b"two\n",
        }
        self._group(
            "claude",
            "session-s",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-s.jsonl", "projects/demo/session-s"),
        )
        manifest = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        destination = self.root / "failed-restore"
        real_copy = bundles._copy_verified_stage
        calls = 0

        def fail_second(staged: Path, output_fd: int, entry: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                os.close(output_fd)
                raise OSError("synthetic ENOSPC")
            real_copy(staged, output_fd, entry)

        with mock.patch.object(bundles, "_copy_verified_stage", side_effect=fail_second):
            with self.assertRaises(OSError):
                bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)

        self.assertFalse((destination / "projects/demo/session-s.jsonl").exists())
        self.assertFalse((destination / "projects/demo/session-s").exists())

    def test_restore_plaintext_stage_is_destination_adjacent_not_in_vault(self) -> None:
        relative = "export/near-destination.txt"
        self._group("agy", "near-destination", {relative: b"payload\n"}, formats={relative: "text"}, source_mode="export")
        manifest = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        destination = self.root / "destination-stage-root"
        real_stage = bundles._stage_restore_bundle
        observed: list[Path] = []

        def capture_stage(bundle_root: Path, checked_manifest: dict[str, object], destination_root: Path) -> tuple[Path, dict[str, Path]]:
            result = real_stage(bundle_root, checked_manifest, destination_root)
            observed.append(result[0])
            return result

        with mock.patch.object(bundles, "_stage_restore_bundle", side_effect=capture_stage):
            bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)

        self.assertEqual(observed[0].parent, destination.parent)
        self.assertFalse(observed[0].exists())
        self.assertFalse(list((self.vault / "bundles").glob(".bundle-restore-stage-*")))

    def test_restore_directory_metadata_race_never_touches_symlink_target(self) -> None:
        files = {
            "projects/demo/session-meta.jsonl": b"main\n",
            "projects/demo/session-meta/worker.txt": b"worker\n",
        }
        self._group(
            "claude",
            "session-meta",
            files,
            formats={path: "text" for path in files},
            root_entries=("projects/demo/session-meta.jsonl", "projects/demo/session-meta"),
        )
        source_sidecar = self.source / "projects/demo/session-meta"
        old = time.time_ns() - 2 * 86_400 * 1_000_000_000
        os.chmod(source_sidecar, 0o500)
        os.utime(source_sidecar, ns=(old, old))
        manifest = bundles.archive("claude", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        destination = self.root / "metadata-race"
        restored_sidecar = destination / "projects/demo/session-meta"
        displaced = destination / "projects/demo/session-meta-displaced"
        sentinel = self.root / "outside-sentinel"
        sentinel.mkdir()
        sentinel_time = old - 123_000_000
        os.chmod(sentinel, 0o755)
        os.utime(sentinel, ns=(sentinel_time, sentinel_time))
        real_preflight = bundles._restore_preflight
        calls = 0

        def replace_after_final_preflight(
            root: Path,
            checked_manifest: dict[str, object],
            trusted_files: dict[str, tuple[int, int, int, int, int, int]] | None = None,
        ) -> dict[str, str]:
            nonlocal calls
            result = real_preflight(root, checked_manifest, trusted_files)
            calls += 1
            if calls == 3:
                os.rename(restored_sidecar, displaced)
                restored_sidecar.symlink_to(sentinel, target_is_directory=True)
            return result

        with mock.patch.object(bundles, "_restore_preflight", side_effect=replace_after_final_preflight):
            with self.assertRaises(VaultError):
                bundles.restore(self.vault, manifest["archive_id"], destination, execute=True)

        sentinel_stat = sentinel.stat()
        self.assertEqual(stat.S_IMODE(sentinel_stat.st_mode), 0o755)
        self.assertEqual(sentinel_stat.st_mtime_ns, sentinel_time)

    def test_exact_resolve_ignores_unrelated_damaged_manifest(self) -> None:
        relative = "export/resolve.txt"
        self._group("agy", "resolve", {relative: b"ok\n"}, formats={relative: "text"}, source_mode="export")
        manifest = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        damaged = self.vault / "bundles/manifests" / (("0" * 64) + ".json")
        damaged.write_text("{}\n", encoding="utf-8")
        bundle_root = self.vault / "bundles"

        resolved = bundles._resolve(bundle_root, manifest["archive_id"])

        self.assertEqual(resolved["archive_id"], manifest["archive_id"])

    def test_verify_reports_damaged_manifest_and_empty_namespace(self) -> None:
        relative = "export/verify.txt"
        self._group("agy", "verify", {relative: b"ok\n"}, formats={relative: "text"}, source_mode="export")
        manifest = bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        damaged = self.vault / "bundles/manifests" / (("0" * 64) + ".json")
        damaged.write_text("{}\n", encoding="utf-8")

        result = bundles.verify(self.vault)

        self.assertFalse(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["verified"][0]["archive_id"], manifest["archive_id"])
        self.assertEqual(result["failed"][0]["archive_id"], "0" * 64)
        self.assertEqual(result["failed"][0]["manifest"], str(damaged))

        empty_vault = self.root / "empty-vault"
        empty_vault.mkdir()
        self.assertEqual(bundles.verify(empty_vault), {"ok": True, "verified": [], "failed": [], "count": 0})

    def test_stats_include_retained_sources_and_net_storage(self) -> None:
        relative = "export/stats.txt"
        payload = b"retained source\n"
        self._group("agy", "stats", {relative: payload}, formats={relative: "text"}, source_mode="export")
        bundles.archive("agy", self.source, self.vault, older_than_days=1, execute=True)

        result = bundles.stats(self.vault)

        self.assertEqual(result["bundle_source_bytes_retained"], len(payload))
        self.assertEqual(result["bundle_net_storage_bytes"], result["bundle_vault_bytes"] + len(payload))
        self.assertGreater(result["bundle_allocated_bytes"], 0)

    def test_diagnostic_manifest_cannot_expand_log_scope_or_use_generic_reader(self) -> None:
        relative = "model-io-sess-boundary.jsonl"
        self._group("zcode", Path(relative).stem, {relative: b'{"type":"model_io"}\n'},
                    formats={relative: "model-io-jsonl"}, source_mode="diagnostic")
        manifest = bundles.archive("zcode", self.source, self.vault, older_than_days=1, execute=True)["archived"][0]
        self.assertTrue(bundles.verify(self.vault, manifest["archive_id"])["ok"])
        for mutation in ("reader", "nested", "tool", "mode", "log_id", "directory"):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(manifest))
                if mutation == "reader":
                    changed["files"][0]["format"] = "generic-jsonl"
                elif mutation == "nested":
                    changed["files"][0]["path"] = "nested/" + relative
                    changed["root_entries"] = ["nested/" + relative]
                elif mutation == "tool":
                    changed["tool"] = "export"
                elif mutation == "mode":
                    changed["source_mode"] = "native"
                elif mutation == "log_id":
                    changed["session_id"] = "another-log"
                else:
                    changed["directories"] = [{"path": "unrelated", "mode": 0o700, "mtime_ns": 1}]
                changed["archive_id"] = bundles._compute_archive_id(changed)
                with self.assertRaises(VaultError):
                    bundles._validate_manifest(changed)


class BundleRealAdapterTests(unittest.TestCase):
    def test_empty_grok_group_does_not_block_valid_group_scan_or_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-vault real adapter ") as raw:
            root = Path(raw)
            source = root / "grok"
            (source / "sessions/cwd/empty").mkdir(parents=True)
            valid = source / "sessions/cwd/valid/updates.jsonl"
            valid.parent.mkdir(parents=True)
            valid.write_bytes(b'{"update":{"text":"searchable"}}\n')
            old = time.time_ns() - 2 * 86_400 * 1_000_000_000
            os.utime(valid, ns=(old, old))
            os.utime(valid.parent, ns=(old, old))
            vault = root / "vault"

            scanned = bundles.scan("grok", source, older_than_days=1)
            by_session = {item["session_id"]: item for item in scanned["candidates"]}
            self.assertFalse(by_session["empty"]["eligible"])
            self.assertIn("no recognized regular files", by_session["empty"]["reason"])
            self.assertTrue(by_session["valid"]["eligible"])

            archived = bundles.archive("grok", source, vault, older_than_days=1, execute=True)
            self.assertEqual([item["session_id"] for item in archived["archived"]], ["valid"])


if __name__ == "__main__":
    unittest.main()
