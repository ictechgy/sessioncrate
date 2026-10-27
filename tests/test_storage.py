from __future__ import annotations

import errno
import fcntl
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_vault import storage
from tests.helpers import make_codex_home, make_session, write_session_bytes


def _gzip_bytes(payload: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", compresslevel=9, mtime=0) as stream:
        stream.write(payload)
    return output.getvalue()


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="codex-vault storage ")
        self.addCleanup(self._temporary.cleanup)
        self.tmp_path = Path(self._temporary.name)

    def _manifest(self, vault: Path) -> dict:
        entries = storage.list_archives(vault)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def _replace_blob_with_invalid_deflate(self, vault: Path, manifest: dict) -> Path:
        blob = vault / manifest["blob"]
        original_size = blob.stat().st_size
        gzip_header = bytes.fromhex("1f8b08000000000000ff")
        corrupt = gzip_header + b"\x07"
        self.assertLessEqual(len(corrupt), original_size)
        blob.write_bytes(corrupt.ljust(original_size, b"\x00"))
        return blob

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

    def test_archive_always_reports_remove_originals_request(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"

        preview = storage.archive(
            home, vault, older_than_days=1, execute=False, remove_originals=True
        )
        executed = storage.archive(
            home, vault, older_than_days=1, execute=True, remove_originals=False
        )

        self.assertTrue(preview["remove_originals_requested"])
        self.assertFalse(executed["remove_originals_requested"])

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

    def test_mkdir_private_does_not_chmod_root_created_by_a_racer(self) -> None:
        target = self.tmp_path / "raced root"
        real_mkdir = storage.os.mkdir
        raced = False
        created_paths: list[tuple[Path, tuple[int, int]]] = []

        def concurrent_create(path, mode=0o777, *args, **kwargs):
            nonlocal raced
            if Path(path) == target and not raced:
                raced = True
                real_mkdir(path, 0o750, *args, **kwargs)
                os.chmod(path, 0o750)
                raise FileExistsError(errno.EEXIST, "concurrent directory creation", path)
            return real_mkdir(path, mode, *args, **kwargs)

        with mock.patch.object(storage.os, "mkdir", side_effect=concurrent_create):
            result = storage._mkdir_private(
                target,
                tighten_existing=False,
                created_paths=created_paths,
            )

        self.assertTrue(raced)
        self.assertEqual(result, target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
        self.assertEqual(created_paths, [])

    def test_mkdir_private_tracks_only_owned_intermediate_directories(self) -> None:
        top = self.tmp_path / "created top"
        raced_parent = top / "raced parent"
        target = raced_parent / "created leaf"
        real_mkdir = storage.os.mkdir
        raced = False
        created_paths: list[tuple[Path, tuple[int, int]]] = []

        def concurrent_create(path, mode=0o777, *args, **kwargs):
            nonlocal raced
            if Path(path) == raced_parent and not raced:
                raced = True
                real_mkdir(path, 0o750, *args, **kwargs)
                os.chmod(path, 0o750)
                raise FileExistsError(errno.EEXIST, "concurrent directory creation", path)
            return real_mkdir(path, mode, *args, **kwargs)

        with mock.patch.object(storage.os, "mkdir", side_effect=concurrent_create):
            storage._mkdir_private(
                target,
                tighten_existing=False,
                created_paths=created_paths,
            )

        self.assertTrue(raced)
        self.assertEqual(stat.S_IMODE(raced_parent.stat().st_mode), 0o750)
        self.assertEqual([path for path, _identity in created_paths], [top, target])
        for path, identity in created_paths:
            current = path.stat()
            self.assertEqual(identity, (current.st_dev, current.st_ino))
            self.assertEqual(stat.S_IMODE(current.st_mode), 0o700)

    def test_mkdir_private_reports_owned_path_when_tightening_fails(self) -> None:
        target = self.tmp_path / "created before tighten failure"
        created_paths: list[tuple[Path, tuple[int, int]]] = []

        with mock.patch.object(
            storage,
            "_tighten_directory",
            side_effect=storage.VaultError("synthetic tightening failure"),
        ):
            with self.assertRaisesRegex(storage.VaultError, "synthetic tightening failure"):
                storage._mkdir_private(
                    target,
                    tighten_existing=False,
                    created_paths=created_paths,
                )

        self.assertEqual(len(created_paths), 1)
        self.assertEqual(created_paths[0][0], target)
        target_stat = target.stat()
        self.assertEqual(created_paths[0][1], (target_stat.st_dev, target_stat.st_ino))
        storage._cleanup_created_directories(created_paths)
        self.assertFalse(target.exists())

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

    def test_invalid_deflate_is_reported_as_failed_verification(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        self._replace_blob_with_invalid_deflate(vault, manifest)

        result = storage.verify(vault, manifest["archive_id"])

        self.assertFalse(result["ok"])
        self.assertIn("corrupt or truncated", result["error"])
        self.assertEqual(source.read_bytes(), original)

    def test_reader_normalizes_invalid_deflate_error(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        self._replace_blob_with_invalid_deflate(vault, manifest)

        with mock.patch.object(storage, "_verify_gzip", return_value=None):
            with self.assertRaisesRegex(storage.VaultError, "cannot read archive blob"):
                list(storage.iter_archive_lines(vault, manifest))

        self.assertEqual(source.read_bytes(), original)

    def test_reader_rejects_same_size_blob_replacement_after_preverification(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        original = source.read_bytes()
        replacement_payload = original.replace(b"session-synthetic", b"session-synthetix")
        replacement_bytes = _gzip_bytes(replacement_payload)
        replacement = self.tmp_path / "replacement.jsonl.gz"
        replacement.write_bytes(replacement_bytes)
        self.assertEqual(len(replacement_payload), manifest["raw_bytes"])
        self.assertEqual(replacement.stat().st_size, blob.stat().st_size)
        real_verify = storage._verify_gzip

        def replace_after_verify(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            os.replace(replacement, blob)
            return result

        with mock.patch.object(storage, "_verify_gzip", side_effect=replace_after_verify):
            with self.assertRaisesRegex(storage.VaultError, "changed while being read"):
                list(storage.iter_archive_lines(vault, manifest))

    def test_reader_rejects_symlink_replacement_after_preverification(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        replacement = self.tmp_path / "replacement.jsonl.gz"
        replacement.write_bytes(blob.read_bytes())
        real_verify = storage._verify_gzip

        def replace_after_verify(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            blob.unlink()
            blob.symlink_to(replacement)
            return result

        with mock.patch.object(storage, "_verify_gzip", side_effect=replace_after_verify):
            with self.assertRaisesRegex(storage.VaultError, "changed while being read"):
                list(storage.iter_archive_lines(vault, manifest))

        self.assertEqual(source.read_bytes(), gzip.decompress(replacement.read_bytes()))

    def test_reader_rejects_in_place_mutation_after_preverification(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        original = source.read_bytes()
        replacement_payload = original.replace(b"session-synthetic", b"session-synthetix")
        replacement_bytes = _gzip_bytes(replacement_payload)
        self.assertEqual(len(replacement_bytes), blob.stat().st_size)
        real_verify = storage._verify_gzip

        def mutate_after_verify(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            with blob.open("r+b") as stream:
                stream.write(replacement_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            return result

        with mock.patch.object(storage, "_verify_gzip", side_effect=mutate_after_verify):
            with self.assertRaisesRegex(storage.VaultError, "changed while being read"):
                list(storage.iter_archive_lines(vault, manifest))

    def test_reader_hashes_bytes_consumed_after_preverification(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        original = source.read_bytes()
        replacement_payload = original.replace(b"session-synthetic", b"session-synthetix")
        replacement_bytes = _gzip_bytes(replacement_payload)
        self.assertEqual(len(replacement_bytes), blob.stat().st_size)
        real_verify = storage._verify_gzip

        def mutate_after_verify(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            with blob.open("r+b") as stream:
                stream.write(replacement_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            return result

        with mock.patch.object(storage, "_verify_gzip", side_effect=mutate_after_verify):
            with mock.patch.object(storage, "_require_unchanged_open_blob", return_value=None):
                with self.assertRaisesRegex(storage.VaultError, "digest or size changed"):
                    list(storage.iter_archive_lines(vault, manifest))

    def test_reader_closes_anchored_blob_descriptor(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        real_open = storage._open_nofollow
        opened: list[int] = []

        def track_open(path: Path, flags: int) -> int:
            fd = real_open(path, flags)
            if path == blob:
                opened.append(fd)
            return fd

        with mock.patch.object(storage, "_open_nofollow", side_effect=track_open):
            self.assertTrue(list(storage.iter_archive_lines(vault, manifest)))

        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_verify_gzip_stops_reading_after_decoded_size_overrun(self) -> None:
        payload = random.Random(0).randbytes(3 * 1024 * 1024)
        encoded = _gzip_bytes(payload)

        class CountingReader(io.BytesIO):
            def __init__(self, value: bytes):
                super().__init__(value)
                self.bytes_read = 0

            def read(self, size: int = -1) -> bytes:
                value = super().read(size)
                self.bytes_read += len(value)
                return value

        source = CountingReader(encoded)
        with self.assertRaisesRegex(storage.VaultError, "size mismatch"):
            storage._verify_gzip(
                self.tmp_path / "synthetic archive.gz",
                hashlib.sha256(payload).hexdigest(),
                1024,
                fileobj=source,
            )

        self.assertLess(source.bytes_read, len(encoded) // 2)

    def test_restore_rejects_decoded_overrun_before_writing_that_chunk(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        blob = vault / manifest["blob"]
        blob.write_bytes(_gzip_bytes(original + random.Random(1).randbytes(2 * 1024 * 1024)))
        source.unlink()
        destination = self.tmp_path / "restore destination"
        retained_temp = self.tmp_path / "retained restore temp"
        temp_fd = os.open(retained_temp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        real_unlink = Path.unlink

        def retain_temp(path: Path, *args, **kwargs):
            if path == retained_temp:
                return None
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(storage, "_verify_one", return_value={"ok": True}), mock.patch.object(
            storage.tempfile,
            "mkstemp",
            return_value=(temp_fd, str(retained_temp)),
        ), mock.patch.object(Path, "unlink", autospec=True, side_effect=retain_temp):
            with self.assertRaisesRegex(storage.VaultError, "size|digest"):
                storage.restore(vault, manifest["archive_id"], destination, execute=True)

        self.assertEqual(retained_temp.read_bytes(), b"")
        self.assertFalse((destination / manifest["relative_path"]).exists())

    def test_restore_read_failure_removes_all_owned_destination_directories(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        source.unlink()
        destination = self.tmp_path / "new restore root" / "nested root"

        class FailingReader:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=-1):
                raise OSError("synthetic restore read failure")

        with mock.patch.object(storage, "_verify_one", return_value={"ok": True}), mock.patch.object(
            storage.gzip,
            "open",
            return_value=FailingReader(),
        ):
            with self.assertRaisesRegex(storage.VaultError, "corrupt during restore"):
                storage.restore(vault, manifest["archive_id"], destination, execute=True)

        self.assertFalse(destination.exists())
        self.assertFalse(destination.parent.exists())

    def test_restore_failure_preserves_raced_foreign_destination_directory(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        source.unlink()
        destination = self.tmp_path / "raced restore root"
        target_parent = destination / Path(manifest["relative_path"]).parent
        sentinel = target_parent / "foreign.txt"
        foreign_mtime = time.time_ns() - 5_000_000_000

        def replace_owned_directory(*args, **kwargs):
            target_parent.rmdir()
            target_parent.mkdir(mode=0o711)
            os.chmod(target_parent, 0o711)
            sentinel.write_bytes(b"foreign bytes")
            os.utime(target_parent, ns=(foreign_mtime, foreign_mtime))
            raise OSError("synthetic tempfile failure")

        with mock.patch.object(storage.tempfile, "mkstemp", side_effect=replace_owned_directory):
            with self.assertRaisesRegex(OSError, "synthetic tempfile failure"):
                storage.restore(vault, manifest["archive_id"], destination, execute=True)

        self.assertEqual(sentinel.read_bytes(), b"foreign bytes")
        raced_stat = target_parent.stat()
        self.assertEqual(stat.S_IMODE(raced_stat.st_mode), 0o711)
        self.assertEqual(raced_stat.st_mtime_ns, foreign_mtime)

    def test_restore_normalizes_invalid_deflate_without_publishing_destination(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        self._replace_blob_with_invalid_deflate(vault, manifest)
        destination = self.tmp_path / "restore home"
        target = destination / manifest["relative_path"]
        target.parent.mkdir(parents=True)
        sentinel = target.parent / "sentinel"
        sentinel.write_bytes(b"keep")
        before = {path.name: path.read_bytes() for path in target.parent.iterdir()}

        with mock.patch.object(storage, "_verify_one", return_value={"ok": True}):
            with self.assertRaisesRegex(storage.VaultError, "corrupt during restore"):
                storage.restore(vault, manifest["archive_id"], destination, execute=True)

        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(target.exists())
        self.assertEqual(
            {path.name: path.read_bytes() for path in target.parent.iterdir()},
            before,
        )

    def test_archive_rejects_setuid_mode_before_publishing(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, mode=0o755)
        os.chmod(source, 0o4755)
        vault = self.tmp_path / "vault"

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["archived"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("source mode is unsupported", result["errors"][0]["reason"])
        self.assertTrue(source.exists())
        self.assertEqual(list((vault / "blobs").iterdir()), [])
        self.assertEqual(list((vault / "manifests").iterdir()), [])

    def test_rearchive_rejects_setgid_mode_and_preserves_valid_manifest(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home, mode=0o755)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        original_manifest = manifest_path.read_bytes()
        os.chmod(source, 0o2755)

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["archived"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("source mode is unsupported", result["errors"][0]["reason"])
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertTrue(storage.verify(vault, manifest["archive_id"])["ok"])

    def test_invalid_manifest_metadata_never_publishes_or_replaces_manifest(self) -> None:
        real_manifest = storage._new_manifest

        def invalid_manifest(**kwargs):
            manifest = real_manifest(**kwargs)
            manifest["source_mtime_ns"] = -1
            return manifest

        for existing in (False, True):
            with self.subTest(existing=existing):
                base = self.tmp_path / str(existing)
                home = make_codex_home(base)
                source = make_session(home)
                vault = base / "vault"
                if existing:
                    storage.archive(home, vault, older_than_days=1, execute=True)
                before = {path.relative_to(vault): path.read_bytes()
                          for path in vault.rglob('*') if path.is_file() and path.name != '.lock'}
                # Inject invalid snapshot metadata directly: macOS filesystems
                # do not consistently preserve a negative nanosecond utime.
                with mock.patch.object(storage, "_new_manifest", side_effect=invalid_manifest):
                    result = storage.archive(home, vault, older_than_days=1, execute=True)
                self.assertEqual(result["archived"], [])
                self.assertTrue(result["errors"])
                after = {path.relative_to(vault): path.read_bytes()
                         for path in vault.rglob('*') if path.is_file() and path.name != '.lock'}
                self.assertEqual(after, before)
                self.assertTrue(source.exists())
                self.assertEqual(len(storage.list_archives(vault)), int(existing))

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

    def test_session_metadata_decoder_limits_do_not_abort_independent_candidates(self) -> None:
        home = make_codex_home(self.tmp_path)
        invalid = make_session(home, "sessions/a/rollout-limited.jsonl", session_id="synthetic-decoder-limit")
        healthy = make_session(home, "sessions/z/rollout-healthy.jsonl")
        original_loads = json.loads
        for error_type in (ValueError, RecursionError):
            with self.subTest(error=error_type.__name__):
                vault = self.tmp_path / error_type.__name__

                def limited_loads(value, **kwargs):
                    if "synthetic-decoder-limit" in value:
                        raise error_type("synthetic decoder detail must stay private")
                    return original_loads(value, **kwargs)

                with mock.patch.object(storage.json, "loads", side_effect=limited_loads):
                    result = storage.archive(home, vault, older_than_days=1, execute=True)
                self.assertEqual([item["relative_path"] for item in result["archived"]], [healthy.relative_to(home).as_posix()])
                self.assertEqual(len(result["errors"]), 1)
                self.assertIn("invalid JSON", result["errors"][0]["reason"])
                self.assertNotIn("synthetic decoder detail", result["errors"][0]["reason"])
                self.assertTrue(invalid.exists())
                self.assertTrue(healthy.exists())
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

    def test_restore_preview_preflights_existing_destinations_without_writes(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)

        identical = storage.restore(vault, manifest["archive_id"], execute=False)
        self.assertTrue(identical["skipped"])
        self.assertFalse(identical["restored"])
        self.assertEqual(source.read_bytes(), original)

        target = self.tmp_path / "preview home" / manifest["relative_path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"different existing content")
        with self.assertRaisesRegex(storage.VaultError, "different bytes"):
            storage.restore(vault, manifest["archive_id"], self.tmp_path / "preview home", execute=False)
        self.assertEqual(target.read_bytes(), b"different existing content")

        absent_root = self.tmp_path / "absent preview home"
        preview = storage.restore(vault, manifest["archive_id"], absent_root, execute=False)
        self.assertFalse(preview["skipped"])
        self.assertFalse(absent_root.exists())

    def test_restore_preview_shares_read_lock_while_execute_requires_exclusive(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        lock_fd = os.open(vault / ".lock", os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            preview = storage.restore(
                vault, manifest["archive_id"], execute=False
            )
            self.assertTrue(preview["skipped"])
            with self.assertRaisesRegex(storage.VaultError, "vault is locked"):
                storage.restore(vault, manifest["archive_id"], execute=True)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_restore_preview_rejects_unsafe_destination_without_following_it(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        destination = self.tmp_path / "preview home"
        target = destination / manifest["relative_path"]
        target.parent.mkdir(parents=True)
        sentinel = self.tmp_path / "preview sentinel"
        sentinel.write_bytes(b"keep")
        os.chmod(sentinel, 0o644)
        target.symlink_to(sentinel)

        with self.assertRaisesRegex(storage.VaultError, "unsafe non-regular|symlink path component"):
            storage.restore(vault, manifest["archive_id"], destination, execute=False)

        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o644)

    def test_restore_preview_rejects_destination_replaced_while_hashing(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        replacement = self.tmp_path / "preview replacement"
        replacement.write_bytes(source.read_bytes())
        real_hash_stream = storage._hash_stream

        def replace_after_hash(stream):
            result = real_hash_stream(stream)
            os.replace(replacement, source)
            return result

        with mock.patch.object(storage, "_hash_stream", side_effect=replace_after_hash):
            with self.assertRaisesRegex(storage.VaultError, "changed while being read"):
                storage.restore(vault, manifest["archive_id"], execute=False)

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

    def test_lock_open_race_does_not_follow_symlink_or_chmod_target(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        lock_path = vault / ".lock"
        sentinel = self.tmp_path / "lock sentinel"
        sentinel.write_bytes(b"keep")
        os.chmod(sentinel, 0o644)
        real_open = storage.os.open
        raced = False

        def race_open(path, flags, *args, **kwargs):
            nonlocal raced
            if Path(path) == lock_path and not raced:
                raced = True
                lock_path.unlink()
                lock_path.symlink_to(sentinel)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(storage.os, "open", side_effect=race_open):
            with self.assertRaises(storage.VaultError):
                storage.list_archives(vault)

        self.assertTrue(raced)
        self.assertTrue(lock_path.is_symlink())
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o644)

    def test_lock_accepts_concurrently_created_regular_lock_without_chmod(self) -> None:
        vault = self.tmp_path / "vault"
        vault.mkdir()
        lock_path = vault / ".lock"
        real_open = storage.os.open
        raced = False

        def concurrent_create(path, flags, *args, **kwargs):
            nonlocal raced
            if Path(path) == lock_path and flags & os.O_EXCL and not raced:
                raced = True
                fd = real_open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o640)
                os.close(fd)
                os.chmod(lock_path, 0o640)
                raise FileExistsError(errno.EEXIST, "concurrent lock creation")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(storage.os, "open", side_effect=concurrent_create):
            with storage._vault_lock(vault, exclusive=True, create=False) as locked:
                self.assertEqual(locked, vault)

        self.assertTrue(raced)
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o640)

    def test_lock_setup_failure_closes_new_descriptor(self) -> None:
        vault = self.tmp_path / "vault"
        vault.mkdir()
        lock_path = vault / ".lock"
        real_open = storage.os.open
        opened: list[int] = []

        def track_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if Path(path) == lock_path:
                opened.append(fd)
            return fd

        with mock.patch.object(storage.os, "open", side_effect=track_open):
            with mock.patch.object(storage.os, "fchmod", side_effect=OSError("chmod failed")):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    with storage._vault_lock(vault, exclusive=True, create=False):
                        self.fail("lock body must not run")

        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

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

    def test_stats_include_only_regular_unfinished_temps(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home, age_seconds=2 * 86400)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        baseline = storage.stats(vault)
        blobs = vault / "blobs"
        manifests = vault / "manifests"
        blob_temp = blobs / ".vault-blob-incomplete.jsonl.gz"
        manifest_temp = manifests / ".vault-tmp-incomplete.json"
        blob_temp.write_bytes(b"blob temp")
        manifest_temp.write_bytes(b"manifest temp")
        outside = self.tmp_path / "outside"
        outside.write_bytes(b"must not be counted")
        (blobs / ".vault-blob-link").symlink_to(outside)
        (manifests / ".vault-tmp-link").symlink_to(outside)

        result = storage.stats(vault)

        self.assertEqual(result["temporary_files"], 2)
        self.assertEqual(result["temporary_bytes"], blob_temp.stat().st_size + manifest_temp.stat().st_size)
        self.assertGreater(result["temporary_allocated_bytes"], 0)
        self.assertEqual(result["blob_count"], 1)
        self.assertEqual(result["archives"], baseline["archives"])
        self.assertEqual(result["manifest_bytes"], baseline["manifest_bytes"])
        self.assertEqual(result["blob_bytes"], baseline["blob_bytes"])
        self.assertEqual(result["vault_bytes"] - baseline["vault_bytes"], result["temporary_bytes"])
        self.assertEqual(result["allocated_bytes"] - baseline["allocated_bytes"], result["temporary_allocated_bytes"])
        self.assertEqual(result["vault_bytes"], result["blob_bytes"] + result["manifest_bytes"] + result["temporary_bytes"])
        self.assertEqual(result["managed_bytes"], result["vault_bytes"] + result["source_bytes_retained"])
        self.assertTrue(outside.exists())

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

    def test_prune_fatal_second_removal_preserves_partial_batch_result(self) -> None:
        home = make_codex_home(self.tmp_path)
        for name in ("a", "b", "c"):
            make_session(
                home,
                f"sessions/p/rollout-batch-{name}.jsonl",
                session_id=f"batch-{name}",
            )
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        ordered = sorted(storage.list_archives(vault), key=lambda item: item["archive_id"])
        real_fsync = storage._fsync_directory
        sync_calls = 0

        def fail_second_removal_before_unlink(path: Path) -> None:
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 3:
                raise OSError("synthetic second removal failure")
            real_fsync(path)

        inactive = {"state": "inactive", "reason": "no open file handles"}
        with mock.patch.object(
            storage, "probe_session_file", return_value=inactive
        ), mock.patch.object(
            storage, "_fsync_directory", side_effect=fail_second_removal_before_unlink
        ), self.assertRaises(storage.BatchOperationError) as raised:
            storage.prune(vault, execute=True)

        partial = raised.exception.partial_result
        self.assertFalse(partial["ok"])
        self.assertEqual(partial["removed"], [ordered[0]["relative_path"]])
        self.assertEqual(
            partial["removed_source_bytes"], ordered[0]["raw_bytes"]
        )
        self.assertEqual(partial["errors"][-1]["archive_id"], ordered[1]["archive_id"])
        self.assertIn("second removal failure", partial["errors"][-1]["reason"])
        self.assertFalse((home / ordered[0]["relative_path"]).exists())
        self.assertTrue((home / ordered[1]["relative_path"]).exists())
        self.assertTrue((home / ordered[2]["relative_path"]).exists())

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

    def test_archive_remove_originals_fatal_failure_preserves_partial_result(self) -> None:
        home = make_codex_home(self.tmp_path)
        paths = [
            make_session(
                home,
                f"sessions/p/rollout-remove-{name}.jsonl",
                session_id=f"remove-{name}",
            )
            for name in ("a", "b", "c")
        ]
        vault = self.tmp_path / "vault"
        real_remove = storage._remove_source_atomically
        calls = 0

        def fail_second_removal(path: Path, manifest: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise storage.VaultError("synthetic fatal source removal")
            real_remove(path, manifest)

        inactive = {"state": "inactive", "reason": "no open file handles"}
        with mock.patch.object(
            storage, "probe_session_file", return_value=inactive
        ), mock.patch.object(
            storage, "_remove_source_atomically", side_effect=fail_second_removal
        ), self.assertRaises(storage.BatchOperationError) as raised:
            storage.archive(
                home,
                vault,
                older_than_days=1,
                execute=True,
                remove_originals=True,
            )

        partial = raised.exception.partial_result
        self.assertEqual(len(partial["archived"]), 3)
        self.assertEqual(partial["removed"], [str(paths[0].relative_to(home))])
        first_manifest = next(
            item
            for item in partial["archived"]
            if item["relative_path"] == str(paths[0].relative_to(home))
        )
        self.assertEqual(partial["removed_source_bytes"], first_manifest["raw_bytes"])
        self.assertEqual(partial["errors"][-1]["relative_path"], str(paths[1].relative_to(home)))
        self.assertIn("fatal source removal", partial["errors"][-1]["reason"])
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())
        self.assertTrue(paths[2].exists())

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

    def test_prefix_resolution_ignores_unrelated_malformed_manifest(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        other_digit = "0" if manifest["archive_id"][0] != "0" else "1"
        unrelated = vault / "manifests" / f"{other_digit * 64}.json"
        unrelated.write_text("{}", encoding="utf-8")

        resolved = storage.resolve_archive(vault, manifest["archive_id"][:12])

        self.assertEqual(resolved["archive_id"], manifest["archive_id"])

    def test_prefix_resolution_rejects_matching_malformed_manifest(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        manifest_path.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(storage.VaultError, "missing required fields"):
            storage.resolve_archive(vault, manifest["archive_id"][:12])

    def test_manifest_reader_rejects_oversized_file_before_decoding(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        with manifest_path.open("r+b") as stream:
            stream.truncate(storage._MAX_MANIFEST_BYTES + 1)

        with self.assertRaisesRegex(storage.VaultError, "manifest exceeds maximum size"):
            storage.resolve_archive(vault, manifest["archive_id"])

    def test_manifest_reader_bounded_read_rejects_growth_after_stat(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        real_fdopen = storage.os.fdopen
        grown = False

        def grow_before_read(fd: int, *args, **kwargs):
            nonlocal grown
            stream = real_fdopen(fd, *args, **kwargs)
            if not grown:
                grown = True
                with manifest_path.open("r+b") as writer:
                    writer.truncate(storage._MAX_MANIFEST_BYTES + 1)
            return stream

        with mock.patch.object(storage.os, "fdopen", side_effect=grow_before_read):
            with self.assertRaisesRegex(storage.VaultError, "manifest exceeds maximum size"):
                storage.resolve_archive(vault, manifest["archive_id"])
        self.assertTrue(grown)

    def test_manifest_reader_normalizes_pathological_json_errors(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"

        pathological = (
            b'{"raw_bytes":'
            + b"9" * 5000
            + b"}"
        )
        manifest_path.write_bytes(pathological)
        with self.assertRaisesRegex(storage.VaultError, "manifest is invalid JSON") as integer_error:
            storage.resolve_archive(vault, manifest["archive_id"])
        self.assertNotIn("9999999999", str(integer_error.exception))

        manifest_path.write_bytes(b"[" * 1_000_000 + b"]" * 1_000_000)
        with self.assertRaisesRegex(storage.VaultError, "manifest is invalid JSON"):
            storage.resolve_archive(vault, manifest["archive_id"])

    def test_prune_malformed_manifest_fails_before_any_batch_deletion(self) -> None:
        home = make_codex_home(self.tmp_path)
        first = make_session(home, "sessions/a/rollout-first.jsonl")
        second = make_session(home, "sessions/b/rollout-second.jsonl")
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifests = storage.list_archives(vault)
        malformed = vault / "manifests" / f"{manifests[-1]['archive_id']}.json"
        malformed.write_text("{}", encoding="utf-8")

        with mock.patch.object(storage, "_remove_source_atomically") as remove:
            with self.assertRaisesRegex(storage.VaultError, "missing required fields"):
                storage.prune(vault, execute=True)

        remove.assert_not_called()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

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

    def test_prune_preview_skips_unreadable_source_and_continues(self) -> None:
        home = make_codex_home(self.tmp_path)
        blocked = make_session(home, "sessions/a/rollout-blocked.jsonl")
        readable = make_session(home, "sessions/z/rollout-readable.jsonl")
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        real_hash_path = storage._hash_path

        def hash_path(path: Path):
            if path == blocked:
                raise PermissionError("source is unreadable")
            return real_hash_path(path)

        inactive = {"state": "inactive", "reason": "no open file handles"}
        with mock.patch.object(storage, "_hash_path", side_effect=hash_path):
            with mock.patch.object(storage, "probe_session_file", return_value=inactive):
                result = storage.prune(vault, execute=False)

        by_path = {item["relative_path"]: item for item in result["candidates"]}
        self.assertFalse(by_path[str(blocked.relative_to(home))]["eligible"])
        self.assertIn("unreadable", by_path[str(blocked.relative_to(home))]["reason"])
        self.assertTrue(by_path[str(readable.relative_to(home))]["eligible"])

    def test_prune_skips_blob_decompression_when_source_is_absent_or_metadata_changed(self) -> None:
        home = make_codex_home(self.tmp_path)
        absent = make_session(home, "sessions/a/rollout-absent.jsonl")
        changed = make_session(home, "sessions/b/rollout-changed.jsonl")
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        absent.unlink()
        changed.write_bytes(changed.read_bytes() + b"changed")

        with mock.patch.object(
            storage,
            "_verify_one",
            side_effect=AssertionError("blob should not be decompressed"),
        ):
            result = storage.prune(vault, execute=False)

        by_path = {item["relative_path"]: item for item in result["candidates"]}
        self.assertFalse(by_path[str(absent.relative_to(home))]["eligible"])
        self.assertIn("does not exist", by_path[str(absent.relative_to(home))]["reason"])
        self.assertFalse(by_path[str(changed.relative_to(home))]["eligible"])
        self.assertEqual(by_path[str(changed.relative_to(home))]["reason"], "source metadata changed")

    def test_prune_preview_skips_source_path_oserror_and_continues(self) -> None:
        home = make_codex_home(self.tmp_path)
        blocked = make_session(home, "sessions/a/rollout-blocked.jsonl")
        readable = make_session(home, "sessions/z/rollout-readable.jsonl")
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        real_source_path = storage._source_path_for_manifest

        def source_path(manifest: dict) -> Path:
            if manifest["relative_path"] == str(blocked.relative_to(home)):
                raise PermissionError("source path is inaccessible")
            return real_source_path(manifest)

        inactive = {"state": "inactive", "reason": "no open file handles"}
        with mock.patch.object(storage, "_source_path_for_manifest", side_effect=source_path):
            with mock.patch.object(storage, "probe_session_file", return_value=inactive):
                result = storage.prune(vault, execute=False)

        by_path = {item["relative_path"]: item for item in result["candidates"]}
        self.assertFalse(by_path[str(blocked.relative_to(home))]["eligible"])
        self.assertIn("inaccessible", by_path[str(blocked.relative_to(home))]["reason"])
        self.assertTrue(by_path[str(readable.relative_to(home))]["eligible"])

    def test_archive_removal_skips_source_path_oserror_before_quarantine(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"

        with mock.patch.object(
            storage,
            "_source_path_for_manifest",
            side_effect=PermissionError("source path is inaccessible"),
        ):
            result = storage.archive(
                home,
                vault,
                older_than_days=1,
                execute=True,
                remove_originals=True,
            )

        self.assertEqual(len(result["archived"]), 1)
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("inaccessible", result["skipped"][0]["reason"])
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(storage.verify(vault)["ok"])

    def test_pre_unlink_directory_fsync_failure_restores_source(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        original = source.read_bytes()
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        inactive = {"state": "inactive", "reason": "no open file handles"}

        with mock.patch.object(storage, "probe_session_file", return_value=inactive):
            with mock.patch.object(
                storage,
                "_fsync_directory",
                side_effect=[OSError("pre-unlink fsync failed"), None],
            ):
                with self.assertRaisesRegex(storage.VaultError, "source restored"):
                    storage.prune(vault, manifest["archive_id"], execute=True)

        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(source.parent.glob(".codex-vault-pending-*")), [])
        self.assertTrue(storage.verify(vault, manifest["archive_id"])["ok"])

    def test_post_unlink_directory_fsync_failure_reports_source_removed(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = make_session(home)
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = self._manifest(vault)
        inactive = {"state": "inactive", "reason": "no open file handles"}

        with mock.patch.object(storage, "probe_session_file", return_value=inactive):
            with mock.patch.object(
                storage,
                "_fsync_directory",
                side_effect=[None, OSError("post-unlink fsync failed")],
            ):
                with self.assertRaisesRegex(
                    storage.BatchOperationError,
                    "source was removed but removal durability could not be confirmed",
                ) as raised:
                    storage.prune(vault, manifest["archive_id"], execute=True)

        partial = raised.exception.partial_result
        self.assertFalse(partial["ok"])
        self.assertEqual(partial["removed"], [manifest["relative_path"]])
        self.assertEqual(partial["removed_source_bytes"], manifest["raw_bytes"])
        self.assertEqual(partial["errors"][-1]["archive_id"], manifest["archive_id"])
        self.assertTrue(partial["errors"][-1]["source_removed"])
        self.assertFalse(source.exists())
        self.assertEqual(list(source.parent.glob(".codex-vault-pending-*")), [])
        self.assertTrue(storage.verify(vault, manifest["archive_id"])["ok"])

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
            with self.assertRaises(storage.BatchOperationError) as raised:
                storage.prune(vault, manifest["archive_id"], execute=True)
        pending_files = list(source.parent.glob(".codex-vault-pending-*"))
        self.assertEqual(len(pending_files), 1)
        self.assertEqual(pending_files[0].read_bytes(), original + b"writer raced")
        partial = raised.exception.partial_result
        self.assertEqual(partial["removed"], [])
        self.assertEqual(partial["errors"][-1]["quarantine"], str(pending_files[0]))
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

    def test_missing_pending_during_activity_probe_stops_batch_with_partial_result(self) -> None:
        home = make_codex_home(self.tmp_path)
        for name in ("a", "b", "c"):
            make_session(
                home,
                f"sessions/p/rollout-disappears-{name}.jsonl",
                session_id=f"disappears-{name}",
            )
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        ordered = sorted(storage.list_archives(vault), key=lambda item: item["archive_id"])
        pending_probes = 0

        def remove_second_pending(path: Path) -> dict[str, str]:
            nonlocal pending_probes
            if path.name.startswith(".codex-vault-pending-"):
                pending_probes += 1
                if pending_probes == 2:
                    path.unlink()
                    return {"state": "active", "reason": "synthetic concurrent removal"}
            return {"state": "inactive", "reason": "no open file handles"}

        with mock.patch.object(
            storage, "probe_session_file", side_effect=remove_second_pending
        ), self.assertRaises(storage.BatchOperationError) as raised:
            storage.prune(vault, execute=True)

        partial = raised.exception.partial_result
        self.assertEqual(partial["removed"], [ordered[0]["relative_path"]])
        self.assertEqual(partial["removed_source_bytes"], ordered[0]["raw_bytes"])
        self.assertEqual(partial["errors"][-1]["archive_id"], ordered[1]["archive_id"])
        self.assertFalse(partial["errors"][-1]["source_removed"])
        self.assertTrue(partial["errors"][-1]["source_missing"])
        self.assertFalse((home / ordered[0]["relative_path"]).exists())
        self.assertFalse((home / ordered[1]["relative_path"]).exists())
        self.assertTrue((home / ordered[2]["relative_path"]).exists())
        self.assertTrue(storage.verify(vault)["ok"])

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
