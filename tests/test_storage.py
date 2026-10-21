from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_vault import storage
from tests.helpers import make_codex_home, make_session, write_session_bytes


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="codex-vault storage ")
        self.addCleanup(self._temporary.cleanup)
        self.tmp_path = Path(self._temporary.name)

    def _manifest(self, vault: Path) -> dict:
        entries = storage.list_archives(vault)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_scan_is_metadata_only_and_reports_age(self) -> None:
        home = make_codex_home(self.tmp_path)
        old = make_session(home)
        make_session(home, "sessions/project/rollout-fresh.jsonl", age_seconds=1)
        result = storage.scan(home, older_than_days=1)
        by_path = {item["relative_path"]: item for item in result["candidates"]}
        self.assertTrue(by_path["sessions/project/rollout-synthetic.jsonl"]["eligible"])
        self.assertEqual(by_path["sessions/project/rollout-synthetic.jsonl"]["source_bytes"], old.stat().st_size)
        self.assertFalse(by_path["sessions/project/rollout-fresh.jsonl"]["eligible"])
        self.assertEqual(by_path["sessions/project/rollout-fresh.jsonl"]["reason"], "too_new")

    def test_dry_run_does_not_create_vault_or_open_invalid_session(self) -> None:
        home = make_codex_home(self.tmp_path)
        write_session_bytes(home, "sessions/project/rollout-invalid.jsonl", b"not json\n")
        vault = self.tmp_path / "vault"
        result = storage.archive(home, vault, older_than_days=1, execute=False)
        self.assertFalse(result["execute"])
        self.assertFalse(vault.exists())
        self.assertTrue(result["candidates"])

    def test_archive_roundtrip_gzip_and_manifest(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, mode=0o640)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        result = storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        self.assertTrue(result["execute"])
        self.assertEqual(manifest["raw_bytes"], len(original))
        self.assertLess(manifest["compressed_bytes"], manifest["raw_bytes"])
        self.assertEqual(manifest["source_root"], str(home))
        self.assertEqual(manifest["relative_path"], "sessions/project/rollout-synthetic.jsonl")
        self.assertEqual(stat.S_IMODE((vault / "manifests" / f"{manifest['archive_id']}.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((vault / manifest["blob"]).stat().st_mode), 0o600)
        self.assertTrue(storage.verify(vault, manifest["archive_id"])["ok"])
        self.assertFalse(storage.restore(vault, manifest["archive_id"], execute=False)["execute"])
        source.unlink()
        restored = storage.restore(vault, manifest["archive_id"], execute=True)
        restored_path = home / manifest["relative_path"]
        self.assertTrue(restored["ok"])
        self.assertEqual(restored_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(restored_path.stat().st_mode), 0o640)
        with gzip.open(vault / manifest["blob"], "rb") as stream:
            self.assertEqual(stream.read(), restored_path.read_bytes())

    def test_archive_preserves_permissions_of_existing_vault_root(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "existing vault root"
        vault.mkdir(mode=0o750)
        os.chmod(vault, 0o750)

        storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(stat.S_IMODE(vault.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((vault / "blobs").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((vault / "manifests").stat().st_mode), 0o700)

    def test_rearchive_preserves_existing_manifest_and_blob_permissions(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        paths = [vault / "manifests" / f"{manifest['archive_id']}.json", vault / manifest["blob"]]
        for path in paths:
            os.chmod(path, 0o400)
        copy = make_session(home, "sessions/2026/01/01/rollout-identical-copy.jsonl")
        copy.write_bytes(source.read_bytes())
        os.utime(copy, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(len(result["archived"]), 2)
        self.assertEqual(result["errors"], [])
        self.assertTrue(storage.verify(vault)["ok"])
        self.assertEqual([stat.S_IMODE(path.stat().st_mode) for path in paths], [0o400, 0o400])

    def test_publish_helper_applies_mode_zero_after_opening_temp(self) -> None:
        temp = self.tmp_path / "publish temp"
        target = self.tmp_path / "published target"
        temp.write_bytes(b"payload")
        os.chmod(temp, 0o600)

        published = storage._publish_temp_no_replace(temp, target, mode=0)

        self.assertTrue(published)
        self.assertFalse(temp.exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0)

    def test_deduplicates_identical_blob_but_keeps_distinct_archive_ids(self) -> None:
        home = make_codex_home(self.tmp_path)
        first = make_session(home)
        second = make_session(home, "archived_sessions/project/rollout-copy.jsonl")
        second.write_bytes(first.read_bytes())
        stamp = time.time_ns() - 2 * 86_400 * 1_000_000_000
        os.utime(second, ns=(stamp, stamp))
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        entries = storage.list_archives(vault)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len({item["archive_id"] for item in entries}), 2)
        self.assertEqual(len({item["blob_sha256"] for item in entries}), 1)
        self.assertEqual(len(list((vault / "blobs").glob("*.jsonl.gz"))), 1)

    def test_corrupt_blob_refuses_verify_restore_and_keeps_source(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        blob.write_bytes(blob.read_bytes()[:-8])
        self.assertFalse(storage.verify(vault, manifest["archive_id"])["ok"])
        with self.assertRaises(storage.VaultError):
            storage.restore(vault, manifest["archive_id"], execute=True)
        self.assertEqual(source.read_bytes(), original)

    def test_source_mutation_refuses_archive_and_keeps_original(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        original_safe_regular = storage._safe_regular
        calls = {"n": 0}

        def mutate_after_stat(path: Path, *, label: str):
            result = original_safe_regular(path, label=label)
            if path == source:
                calls["n"] += 1
                if calls["n"] == 2:
                    source.write_bytes(source.read_bytes() + b"\nmutation")
            return result

        with mock.patch.object(storage, "_safe_regular", side_effect=mutate_after_stat):
            result = storage.archive(home, vault, older_than_days=1, execute=True)
        self.assertEqual(result["archived"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("changed while archiving", result["errors"][0]["reason"])
        self.assertTrue(source.exists())

    def test_symlink_source_is_rejected_and_manifest_traversal_is_skipped(self) -> None:
        home = make_codex_home(self.tmp_path)
        real = make_session(home, "sessions/project/rollout-real.jsonl")
        link = home / "sessions/project/rollout-link.jsonl"
        link.symlink_to(real)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        injected = vault / "manifests" / ("f" * 64 + ".json")
        injected.write_text(json.dumps({**manifest, "archive_id": "f" * 64, "blob": "../outside.gz"}), encoding="utf-8")
        with self.assertRaises(storage.VaultError):
            storage.list_archives(vault)
        candidates = {item["relative_path"]: item for item in storage.scan(home)["candidates"]}
        self.assertFalse(candidates["sessions/project/rollout-link.jsonl"]["eligible"])
        self.assertEqual(candidates["sessions/project/rollout-link.jsonl"]["reason"], "symlink")
        self.assertFalse(storage.verify(vault)["ok"])

    def test_later_invalid_source_cleans_prepared_temps(self) -> None:
        home = make_codex_home(self.tmp_path)
        first = make_session(home, "sessions/a/rollout-good.jsonl")
        invalid = write_session_bytes(home, "sessions/m/rollout-invalid.jsonl", b"not json\n")
        second = make_session(home, "sessions/z/rollout-good.jsonl")
        vault = self.tmp_path / "vault"

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(
            {item["relative_path"] for item in result["archived"]},
            {str(first.relative_to(home)), str(second.relative_to(home))},
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["relative_path"], str(invalid.relative_to(home)))
        self.assertIn("invalid JSON", result["errors"][0]["reason"])
        self.assertTrue(first.exists())
        self.assertTrue(invalid.exists())
        self.assertTrue(second.exists())
        self.assertEqual(list((vault / "blobs").glob(".vault-blob-*")), [])
        self.assertTrue(storage.verify(vault)["ok"])

    def test_restore_conflict_preserves_existing_destination(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        destination = self.tmp_path / "other home"
        target = destination / manifest["relative_path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"different existing content")
        with self.assertRaises(storage.VaultError):
            storage.restore(vault, manifest["archive_id"], destination, execute=True)
        self.assertEqual(target.read_bytes(), b"different existing content")

    def test_restore_publishes_mode_zero_after_writing_temp(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest["source_mode"] = 0
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        storage._replace_json_atomic(manifest_path, manifest)
        source.unlink()

        result = storage.restore(vault, manifest["archive_id"], execute=True)

        self.assertTrue(result["restored"])
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0)

    def test_restore_publish_race_does_not_report_different_destination_as_restored(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        source.unlink()
        raced_bytes = b"new live bytes created during restore"
        real_link = storage.os.link

        def publish_after_destination_appears(src, dst, *args, **kwargs):
            destination = Path(dst)
            destination.write_bytes(raced_bytes)
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(storage.os, "link", side_effect=publish_after_destination_appears):
            with self.assertRaisesRegex(storage.VaultError, "different bytes"):
                storage.restore(vault, manifest["archive_id"], execute=True)

        self.assertEqual(source.read_bytes(), raced_bytes)

    def test_restore_publish_symlink_race_does_not_chmod_target_or_report_success(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        source.unlink()
        sentinel = self.tmp_path / "sentinel"
        sentinel.write_bytes(b"must not be changed")
        os.chmod(sentinel, 0o644)
        real_link = storage.os.link

        def replace_published_path_with_symlink(src, dst, *args, **kwargs):
            result = real_link(src, dst, *args, **kwargs)
            target = Path(dst)
            target.unlink()
            target.symlink_to(sentinel)
            return result

        with mock.patch.object(storage.os, "link", side_effect=replace_published_path_with_symlink):
            with self.assertRaisesRegex(storage.VaultError, "publication race"):
                storage.restore(vault, manifest["archive_id"], execute=True)

        self.assertTrue(source.is_symlink())
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o644)

    def test_rearchive_same_bytes_refreshes_source_metadata(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, mode=0o600)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        first = self._manifest(vault)

        os.chmod(source, 0o640)
        refreshed_mtime = time.time_ns() - 3 * 86_400 * 1_000_000_000
        os.utime(source, ns=(refreshed_mtime, refreshed_mtime))
        storage.archive(home, vault, older_than_days=1, execute=True)
        refreshed = self._manifest(vault)

        self.assertEqual(refreshed["archive_id"], first["archive_id"])
        self.assertEqual(refreshed["source_mode"], 0o640)
        self.assertEqual(refreshed["source_mtime_ns"], refreshed_mtime)
        with mock.patch.object(
            storage,
            "probe_session_file",
            return_value={"state": "inactive", "reason": "no open file handles"},
        ):
            preview = storage.prune(vault, refreshed["archive_id"], execute=False)
        self.assertTrue(preview["candidates"][0]["eligible"])

    def test_manifest_replace_symlink_race_does_not_chmod_target(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, mode=0o600)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        sentinel = self.tmp_path / "manifest sentinel"
        sentinel.write_bytes(b"must keep its mode")
        os.chmod(sentinel, 0o644)
        os.chmod(source, 0o640)
        refreshed_mtime = time.time_ns() - 3 * 86_400 * 1_000_000_000
        os.utime(source, ns=(refreshed_mtime, refreshed_mtime))
        real_replace = storage.os.replace

        def replace_manifest_with_symlink(src, dst, *args, **kwargs):
            result = real_replace(src, dst, *args, **kwargs)
            target = Path(dst)
            target.unlink()
            target.symlink_to(sentinel)
            return result

        with mock.patch.object(storage.os, "replace", side_effect=replace_manifest_with_symlink):
            result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["archived"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(manifest_path.is_symlink())
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o644)

    def test_lock_refuses_concurrent_mutation(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        import fcntl

        lock = (vault / ".lock").open("r+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(storage.VaultError):
                storage.archive(home, vault, older_than_days=1, execute=True)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def test_prune_preserves_active_source_and_stats_count_actual_bytes(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, age_seconds=2 * 86400)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        preview = storage.prune(vault, manifest["archive_id"], execute=False)
        self.assertFalse(preview["execute"])
        self.assertTrue(source.exists())
        busy = {'state': 'active', 'reason': 'session file is open'}
        with mock.patch.object(storage, "probe_session_file", return_value=busy):
            result = storage.prune(vault, manifest["archive_id"], execute=True)
        self.assertEqual(result['removed'], [])
        self.assertEqual(result['skipped'][0]['activity'], 'active')
        self.assertTrue(source.exists())
        with mock.patch.object(storage, "probe_session_file", return_value=busy):
            result = storage.archive(home, vault, older_than_days=1, execute=True, remove_originals=True)
        self.assertEqual(result['removed'], [])
        self.assertEqual(len(result['skipped']), 1)
        self.assertTrue(source.exists())
        stats = storage.stats(vault)
        self.assertGreater(stats["blob_bytes"], 0)
        self.assertGreater(stats["manifest_bytes"], 0)
        self.assertEqual(stats["vault_bytes"], stats["blob_bytes"] + stats["manifest_bytes"])
        self.assertEqual(stats["source_bytes_retained"], source.stat().st_size)
        self.assertEqual(stats["managed_bytes"], stats["vault_bytes"] + stats["source_bytes_retained"])
        self.assertEqual(stats["net_storage_bytes"], stats["managed_bytes"])

    def test_prune_inactive_session_while_unrelated_codex_runs(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        running = mock.Mock(stdout="/Applications/Codex.app/Contents/MacOS/Codex\n", returncode=0, stderr="")
        with mock.patch.object(storage, 'probe_session_file',
                               return_value={'state': 'inactive', 'reason': 'no open file handles'}):
            with mock.patch('subprocess.run', return_value=running):
                result = storage.prune(vault, execute=True)
        self.assertEqual(result['removed'], [str(source.relative_to(home))])
        self.assertFalse(source.exists())
        self.assertTrue(storage.verify(vault)['ok'])

    def test_mixed_batch_skips_only_active_or_unknown_sessions(self) -> None:
        home = make_codex_home(self.tmp_path)
        idle = make_session(home, 'sessions/p/rollout-idle.jsonl')
        active = make_session(home, 'sessions/p/rollout-active.jsonl')
        unknown = make_session(home, 'sessions/p/rollout-unknown.jsonl')
        vault = self.tmp_path / 'vault'
        storage.archive(home, vault, older_than_days=1, execute=True)

        def probe(path):
            if 'rollout-active' in path.name:
                return {'state': 'active', 'reason': 'session file is open'}
            if 'rollout-unknown' in path.name:
                return {'state': 'unknown', 'reason': 'cannot inspect file activity'}
            return {'state': 'inactive', 'reason': 'no open file handles'}

        with mock.patch.object(storage, 'probe_session_file', side_effect=probe):
            preview = storage.prune(vault)
            result = storage.prune(vault, execute=True)
        by_path = {item['relative_path']: item for item in preview['candidates']}
        self.assertTrue(by_path[str(idle.relative_to(home))]['eligible'])
        self.assertFalse(by_path[str(active.relative_to(home))]['eligible'])
        self.assertFalse(by_path[str(unknown.relative_to(home))]['eligible'])
        self.assertFalse(idle.exists())
        self.assertTrue(active.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual(len(result['removed']), 1)
        self.assertEqual(len(result['skipped']), 2)

    def test_prune_batch_skips_source_changed_after_preview_and_continues(self) -> None:
        home = make_codex_home(self.tmp_path)
        sources = [
            make_session(home, f"sessions/p/rollout-{name}.jsonl", session_id=f"session-{name}")
            for name in ("a", "b", "c")
        ]
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifests = sorted(storage.list_archives(vault), key=lambda item: item["archive_id"])
        changed_manifest = manifests[1]
        changed = home / changed_manifest["relative_path"]
        real_remove = storage._remove_source_atomically
        mutated = False

        def mutate_selected_then_remove(path, manifest):
            nonlocal mutated
            if manifest["archive_id"] == changed_manifest["archive_id"] and not mutated:
                path.write_bytes(path.read_bytes() + b"resumed after preview\n")
                mutated = True
            return real_remove(path, manifest)

        with mock.patch.object(
            storage,
            "probe_session_file",
            return_value={"state": "inactive", "reason": "no open file handles"},
        ):
            with mock.patch.object(
                storage, "_remove_source_atomically", side_effect=mutate_selected_then_remove
            ):
                result = storage.prune(vault, execute=True)

        self.assertTrue(mutated)
        self.assertTrue(changed.exists())
        self.assertEqual(
            set(result["removed"]),
            {str(path.relative_to(home)) for path in sources if path != changed},
        )
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["archive_id"], changed_manifest["archive_id"])
        self.assertIn("source metadata changed", result["skipped"][0]["reason"])

    def test_archive_remove_originals_preserves_busy_file_and_removes_idle_file(self) -> None:
        home = make_codex_home(self.tmp_path)
        idle = make_session(home, 'sessions/p/rollout-idle.jsonl')
        busy = make_session(home, 'sessions/p/rollout-busy.jsonl')
        vault = self.tmp_path / 'vault'

        def probe(path):
            return {'state': 'active', 'reason': 'session file is open'} if path == busy else {
                'state': 'inactive', 'reason': 'no open file handles'}

        with mock.patch.object(storage, 'probe_session_file', side_effect=probe):
            result = storage.archive(home, vault, older_than_days=1, execute=True, remove_originals=True)
        self.assertFalse(idle.exists())
        self.assertTrue(busy.exists())
        self.assertEqual(len(result['archived']), 2)
        self.assertEqual(len(result['removed']), 1)
        self.assertEqual(result['skipped'][0]['relative_path'], str(busy.relative_to(home)))
        self.assertTrue(storage.verify(vault)['ok'])

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is unavailable')
    def test_real_open_file_is_preserved_while_closed_source_is_pruned(self) -> None:
        home = make_codex_home(self.tmp_path)
        idle = make_session(home, 'sessions/p/rollout-idle.jsonl')
        busy = make_session(home, 'sessions/p/rollout-busy.jsonl')
        vault = self.tmp_path / 'vault'
        storage.archive(home, vault, older_than_days=1, execute=True)
        with busy.open('rb'):
            result = storage.prune(vault, execute=True)
        self.assertFalse(idle.exists())
        self.assertTrue(busy.exists())
        self.assertEqual(result['removed'], [str(idle.relative_to(home))])
        self.assertEqual(result['skipped'][0]['activity'], 'active')
        self.assertTrue(storage.verify(vault)['ok'])

    def test_manifest_identity_and_canonical_path_tampering_is_rejected(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        tampered = dict(manifest)
        tampered["relative_path"] = "./" + manifest["relative_path"]
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaises(storage.VaultError):
            storage.list_archives(vault)

    def test_manifest_identity_tampering_is_rejected_when_source_root_is_gone(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        tampered = dict(manifest)
        tampered["source_root"] = str(self.tmp_path / "gone codex home")
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaises(storage.VaultError):
            storage.list_archives(vault)

    def test_iterator_rejects_untrusted_manifest_identity(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        tampered = dict(manifest)
        tampered["archive_id"] = "a" * 64

        with self.assertRaises(storage.VaultError):
            list(storage.iter_archive_lines(vault, tampered))

    def test_process_guard_mutating_quarantine_preserves_pending_file(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, age_seconds=2 * 86400)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        def guard(path):
            if path.name.startswith('.codex-vault-pending-'):
                path.write_bytes(path.read_bytes() + b"writer raced")
            return {'state': 'inactive', 'reason': 'no open file handles'}

        manifest = self._manifest(vault)
        with mock.patch.object(storage, "probe_session_file", side_effect=guard):
            with self.assertRaises(storage.VaultError):
                storage.prune(vault, manifest["archive_id"], execute=True)
        pending_files = list(source.parent.glob(".codex-vault-pending-*"))
        self.assertEqual(len(pending_files), 1)
        self.assertEqual(pending_files[0].read_bytes(), original + b"writer raced")
        self.assertFalse(source.exists())

    def test_session_opened_during_quarantine_is_skipped_and_source_restored(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / 'vault'
        storage.archive(home, vault, older_than_days=1, execute=True)

        def probe(path):
            if path.name.startswith('.codex-vault-pending-'):
                return {'state': 'active', 'reason': 'session opened during cleanup'}
            return {'state': 'inactive', 'reason': 'no open file handles'}

        with mock.patch.object(storage, 'probe_session_file', side_effect=probe):
            result = storage.prune(vault, execute=True)
        self.assertEqual(result['removed'], [])
        self.assertEqual(result['skipped'][0]['activity'], 'active')
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(source.parent.glob('.codex-vault-pending-*')))

    def test_rollback_race_preserves_new_live_file_and_quarantined_archive_copy(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        raced_bytes = b"new live bytes created during rollback"
        real_rename = storage.os.rename
        real_link = storage.os.link

        def inject_before_rollback(operation, src, dst, *args, **kwargs):
            if Path(src).name.startswith(".codex-vault-pending-") and Path(dst) == source:
                source.write_bytes(raced_bytes)
            return operation(src, dst, *args, **kwargs)

        def probe(path):
            if path.name.startswith(".codex-vault-pending-"):
                return {"state": "active", "reason": "session opened during cleanup"}
            return {"state": "inactive", "reason": "no open file handles"}

        with mock.patch.object(storage, "probe_session_file", side_effect=probe):
            with mock.patch.object(
                storage.os,
                "rename",
                side_effect=lambda src, dst, *args, **kwargs: inject_before_rollback(
                    real_rename, src, dst, *args, **kwargs
                ),
            ):
                with mock.patch.object(
                    storage.os,
                    "link",
                    side_effect=lambda src, dst, *args, **kwargs: inject_before_rollback(
                        real_link, src, dst, *args, **kwargs
                    ),
                ):
                    with self.assertRaisesRegex(storage.VaultError, "retained in quarantine"):
                        storage.prune(vault, execute=True)

        self.assertEqual(source.read_bytes(), raced_bytes)
        pending_files = list(source.parent.glob(".codex-vault-pending-*"))
        self.assertEqual(len(pending_files), 1)
        self.assertEqual(pending_files[0].read_bytes(), original)

    def test_rollback_rejects_pending_symlink_swap_and_keeps_raced_paths(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        sentinel = self.tmp_path / "rollback sentinel"
        sentinel.write_bytes(b"raced path")
        real_link = storage.os.link

        def swap_pending_before_link(src, dst, *args, **kwargs):
            pending = Path(src)
            if pending.name.startswith(".codex-vault-pending-"):
                pending.unlink()
                pending.symlink_to(sentinel)
            return real_link(src, dst, *args, **kwargs)

        def probe(path):
            if path.name.startswith(".codex-vault-pending-"):
                return {"state": "active", "reason": "session opened during cleanup"}
            return {"state": "inactive", "reason": "no open file handles"}

        with mock.patch.object(storage, "probe_session_file", side_effect=probe):
            with mock.patch.object(storage.os, "link", side_effect=swap_pending_before_link):
                with self.assertRaisesRegex(storage.VaultError, "retained in quarantine"):
                    storage.prune(vault, execute=True)

        pending_files = list(source.parent.glob(".codex-vault-pending-*"))
        self.assertEqual(len(pending_files), 1)
        self.assertTrue(pending_files[0].is_symlink())
        self.assertTrue(source.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"raced path")


if __name__ == "__main__":
    unittest.main()
