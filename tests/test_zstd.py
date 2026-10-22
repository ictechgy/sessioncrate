from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_vault import storage
from codex_vault import zstd_io
from codex_vault.search import search
from tests.helpers import make_codex_home, make_session, write_session_bytes

try:
    from compression import zstd as stdlib_zstd
except ImportError:
    stdlib_zstd = None


_EMBEDDED_PAYLOAD = (
    b'{"type":"session_meta","session_id":"embedded-zstd","cwd":"/tmp/zstd",'
    b'"tool_version":"test","started_at":"2026-01-01T00:00:00Z"}\n'
    b'{"type":"event_msg","payload":{"type":"user_message",'
    b'"message":"\xed\x95\x9c\xea\xb5\xad\xec\x96\xb4 \xea\xb2\x80\xec\x83\x89"}}\n'
)
_EMBEDDED_ZSTD = bytes.fromhex(
    "28b52ffd20d5350500a289222880c5e974233202d680a0b322c02995bff80d8916"
    "b4e925f264d1b6629039880242e93ccd24408e0eca75bdc1c816122c307964110"
    "00ce208b8be7d1fc8bcf251cb077d8433b2e0f4f687e56b69ab55ae13d4b941e"
    "396c62d02c739d7377c719b82ddbc7d3db5d58be66c6692cd200c6f8fcd89256"
    "e8c5b907b7d84da61af7cd2849897afdfc9ea2b0209002da40e5a2ec15456bf8"
    "1846d23585e33f59cb695623601"
)


def _jsonl(*records: dict) -> bytes:
    return b"".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )


def _records() -> tuple[dict, dict]:
    return (
        {
            "type": "session_meta",
            "session_id": "zstd-session",
            "cwd": "/tmp/zstd-project",
            "tool_version": "zstd-test",
            "started_at": "2026-10-23T00:00:00Z",
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "압축된 한국어 검색 기록"},
        },
    )


def _write_old(path: Path, data: bytes, *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    stamp = time.time_ns() - 2 * 86_400 * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return path


def _write_embedded_archive(base: Path, *, with_source: bool = True) -> tuple[Path, Path, dict]:
    home = make_codex_home(base)
    relative = "sessions/project/rollout-embedded.jsonl.zst"
    source = home / relative
    stamp = time.time_ns() - 2 * 86_400 * 1_000_000_000
    if with_source:
        _write_old(source, _EMBEDDED_ZSTD, mode=0o400)
        stamp = source.stat().st_mtime_ns
    digest = hashlib.sha256(_EMBEDDED_ZSTD).hexdigest()
    archive_id = storage._archive_id(str(home), relative, digest)
    vault = base / "vault"
    blobs = vault / "blobs"
    manifests = vault / "manifests"
    blobs.mkdir(parents=True)
    manifests.mkdir()
    blob = blobs / f"{digest}.jsonl.gz"
    with gzip.GzipFile(filename=str(blob), mode="wb", compresslevel=0, mtime=0) as stream:
        stream.write(_EMBEDDED_ZSTD)
    manifest = {
        "archive_id": archive_id,
        "blob_sha256": digest,
        "blob": f"blobs/{digest}.jsonl.gz",
        "source_root": str(home),
        "relative_path": relative,
        "session_id": "embedded-zstd",
        "cwd": "/tmp/zstd",
        "tool_version": "test",
        "started_at": "2026-01-01T00:00:00Z",
        "raw_bytes": len(_EMBEDDED_ZSTD),
        "decoded_bytes": len(_EMBEDDED_PAYLOAD),
        "compressed_bytes": blob.stat().st_size,
        "source_mtime_ns": stamp,
        "source_mode": 0o400,
        "archived_at": "2026-10-23T00:00:01+00:00",
    }
    (manifests / f"{archive_id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return vault, source, manifest


@unittest.skipUnless(stdlib_zstd is not None, "Python 3.14 Zstandard backend is unavailable")
class ZstdArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="sessioncrate zstd ")
        self.addCleanup(self._temporary.cleanup)
        self.tmp_path = Path(self._temporary.name)

    def test_archives_searches_prunes_and_restores_original_zstd_bytes(self) -> None:
        home = make_codex_home(self.tmp_path)
        decoded = _jsonl(*_records())
        original = stdlib_zstd.compress(decoded)
        source = _write_old(
            home / "sessions/project/rollout-zstd.jsonl.zst", original, mode=0o400
        )
        vault = self.tmp_path / "vault"

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["errors"], [])
        manifest = storage.list_archives(vault)[0]
        self.assertEqual(manifest["raw_bytes"], len(original))
        self.assertEqual(manifest["decoded_bytes"], len(decoded))
        self.assertEqual(manifest["session_id"], "zstd-session")
        with gzip.open(vault / manifest["blob"], "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(search(vault, "한국어")["matches"][0]["session_id"], "zstd-session")

        inactive = {"state": "inactive", "reason": "no open file handles"}
        with mock.patch.object(storage, "probe_session_file", return_value=inactive):
            pruned = storage.prune(vault, manifest["archive_id"], execute=True)
        self.assertEqual(pruned["removed"], [manifest["relative_path"]])
        self.assertFalse(source.exists())

        restored = storage.restore(vault, manifest["archive_id"], execute=True)
        self.assertTrue(restored["restored"])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o400)

    def test_accepts_concatenated_zstd_frames(self) -> None:
        home = make_codex_home(self.tmp_path)
        metadata, message = _records()
        first = _jsonl(metadata)
        second = _jsonl(message)
        original = stdlib_zstd.compress(first) + stdlib_zstd.compress(second)
        _write_old(home / "sessions/project/rollout-concat.jsonl.zst", original)
        vault = self.tmp_path / "vault"

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["errors"], [])
        manifest = storage.list_archives(vault)[0]
        self.assertEqual(manifest["decoded_bytes"], len(first) + len(second))
        self.assertTrue(search(vault, "한국어")["matches"])

    def test_rejects_corrupt_truncated_trailing_and_missing_metadata_before_publication(self) -> None:
        metadata, message = _records()
        valid = stdlib_zstd.compress(_jsonl(metadata, message))
        cases = {
            "corrupt": b"not-zstandard",
            "truncated": valid[:-1],
            "trailing": valid + b"trailing-garbage",
            "missing-meta": stdlib_zstd.compress(_jsonl(message)),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                case_root = self.tmp_path / name
                home = make_codex_home(case_root)
                source = _write_old(
                    home / f"sessions/project/rollout-{name}.jsonl.zst", payload
                )
                vault = case_root / "vault"
                inactive = {"state": "inactive", "reason": "no open file handles"}
                with mock.patch.object(storage, "probe_session_file", return_value=inactive):
                    result = storage.archive(
                        home,
                        vault,
                        older_than_days=1,
                        execute=True,
                        remove_originals=True,
                    )
                self.assertEqual(result["archived"], [])
                self.assertEqual(len(result["errors"]), 1)
                self.assertTrue(source.exists())
                self.assertEqual(storage.list_archives(vault), [])
                self.assertEqual(list((vault / "blobs").glob("*.gz")), [])

    def test_decoded_output_limit_rejects_archive_before_publication(self) -> None:
        home = make_codex_home(self.tmp_path)
        decoded = _jsonl(*_records()) + b"x" * 256
        source = _write_old(
            home / "sessions/project/rollout-limit.jsonl.zst",
            stdlib_zstd.compress(decoded),
        )
        vault = self.tmp_path / "vault"

        with mock.patch.object(storage, "_zstd_decoded_limit", return_value=64):
            result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["archived"], [])
        self.assertIn("safe decoded size limit", result["errors"][0]["reason"])
        self.assertTrue(source.exists())
        self.assertEqual(storage.list_archives(vault), [])

    def test_reader_rejects_manifest_decoded_size_mismatch(self) -> None:
        home = make_codex_home(self.tmp_path)
        decoded = _jsonl(*_records())
        _write_old(
            home / "sessions/project/rollout-mismatch.jsonl.zst",
            stdlib_zstd.compress(decoded),
        )
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = storage.list_archives(vault)[0]
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        manifest["decoded_bytes"] += 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(storage.VaultError, "decoded size mismatch"):
            list(storage.iter_archive_lines(vault, manifest))

    def test_default_raw_iterator_rejects_overlong_zstd_record_with_small_patched_cap(self) -> None:
        home = make_codex_home(self.tmp_path)
        metadata, _ = _records()
        first = _jsonl(metadata)
        decoded = first + b"x" * 512 + b"\n"
        _write_old(
            home / "sessions/project/rollout-long-record.jsonl.zst",
            stdlib_zstd.compress(decoded),
        )
        vault = self.tmp_path / "vault"
        storage.archive(home, vault, older_than_days=1, execute=True)
        manifest = storage.list_archives(vault)[0]

        with mock.patch.object(storage, "_MAX_ZSTD_RECORD_BYTES", len(first) + 16):
            with self.assertRaisesRegex(storage.VaultError, "Zstandard JSONL record"):
                list(storage.iter_archive_lines(vault, manifest))


class ZstdCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="sessioncrate zstd compat ")
        self.addCleanup(self._temporary.cleanup)
        self.tmp_path = Path(self._temporary.name)

    def test_scan_recognizes_zstd_without_opening_file_content(self) -> None:
        home = make_codex_home(self.tmp_path)
        source = _write_old(
            home / "sessions/project/rollout-metadata-only.jsonl.zst", b"not-read"
        )

        with mock.patch.object(storage, "_open_nofollow", side_effect=AssertionError("opened")):
            result = storage.scan(home, older_than_days=1)

        self.assertEqual(result["candidates"][0]["relative_path"], str(source.relative_to(home)))
        self.assertTrue(result["candidates"][0]["eligible"])

    def test_default_raw_iterator_always_passes_a_finite_readline_size(self) -> None:
        vault, _, manifest = _write_embedded_archive(self.tmp_path)
        line = b"synthetic record\n"
        manifest = {**manifest, "decoded_bytes": len(line)}

        class FakeReader:
            def __init__(self) -> None:
                self.sizes: list[int] = []
                self.lines = iter((line, b""))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def readline(self, size: int) -> bytes:
                self.sizes.append(size)
                return next(self.lines)

        reader = FakeReader()
        with mock.patch.object(zstd_io, "is_available", return_value=True), mock.patch.object(
            zstd_io, "open_reader", return_value=reader
        ), mock.patch.object(storage, "_verify_gzip", return_value=None), mock.patch.object(
            storage, "_MAX_ZSTD_RECORD_BYTES", 123
        ):
            self.assertEqual(list(storage.iter_archive_lines(vault, manifest)), [line])

        self.assertEqual(reader.sizes, [124, 124])

    def test_reader_rejects_decoded_size_overrun_before_reading_again(self) -> None:
        vault, _, manifest = _write_embedded_archive(self.tmp_path)
        first = b"first\n"
        second = b"second\n"
        manifest = {**manifest, "decoded_bytes": len(first)}

        class OverrunReader:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def readline(self, size: int) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    return first
                if self.calls == 2:
                    return second
                raise AssertionError("reader was called after decoded size overrun")

        reader = OverrunReader()
        with mock.patch.object(zstd_io, "is_available", return_value=True), mock.patch.object(
            zstd_io, "open_reader", return_value=reader
        ), mock.patch.object(storage, "_verify_gzip", return_value=None):
            with self.assertRaisesRegex(storage.VaultError, "decoded size mismatch"):
                list(storage.iter_archive_lines(vault, manifest))

        self.assertEqual(reader.calls, 2)

    def test_missing_decoder_reports_zstd_error_and_plain_archive_still_succeeds(self) -> None:
        home = make_codex_home(self.tmp_path)
        zstd_source = _write_old(
            home / "sessions/a/rollout-zstd.jsonl.zst", _EMBEDDED_ZSTD
        )
        make_session(home, "sessions/z/rollout-plain.jsonl")
        vault = self.tmp_path / "vault"
        real_stream_archive = storage._stream_archive
        streamed_sources: list[Path] = []

        def track_stream(path: Path, *args, **kwargs):
            streamed_sources.append(path)
            return real_stream_archive(path, *args, **kwargs)

        with mock.patch.object(zstd_io, "_zstd", None), mock.patch.object(
            storage, "_stream_archive", side_effect=track_stream
        ):
            result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(len(result["archived"]), 1)
        self.assertTrue(result["archived"][0]["relative_path"].endswith(".jsonl"))
        self.assertEqual(result["errors"][0]["relative_path"], str(zstd_source.relative_to(home)))
        self.assertIn("Zstandard content requires Python 3.14+", result["errors"][0]["reason"])
        self.assertTrue(zstd_source.exists())
        self.assertEqual([path.name for path in streamed_sources], ["rollout-plain.jsonl"])

    def test_missing_decoder_blocks_search_but_not_verify_restore_or_prune(self) -> None:
        vault, source, manifest = _write_embedded_archive(self.tmp_path)
        destination = self.tmp_path / "restored elsewhere"

        with mock.patch.object(zstd_io, "_zstd", None):
            self.assertTrue(storage.verify(vault, manifest["archive_id"])["ok"])
            restored = storage.restore(
                vault, manifest["archive_id"], destination_home=destination, execute=True
            )
            self.assertTrue(restored["restored"])
            restored_path = destination / manifest["relative_path"]
            self.assertEqual(restored_path.read_bytes(), _EMBEDDED_ZSTD)
            self.assertEqual(stat.S_IMODE(restored_path.stat().st_mode), 0o400)
            with mock.patch.object(
                storage,
                "_verify_gzip",
                side_effect=AssertionError("Zstandard search preverified a large gzip blob"),
            ):
                with self.assertRaisesRegex(
                    storage.VaultError, r"Zstandard content requires Python 3.14\+"
                ):
                    search(vault, "한국어")
            inactive = {"state": "inactive", "reason": "no open file handles"}
            with mock.patch.object(storage, "probe_session_file", return_value=inactive):
                result = storage.prune(vault, manifest["archive_id"], execute=True)

        self.assertEqual(result["removed"], [manifest["relative_path"]])
        self.assertFalse(source.exists())

    def test_plain_archive_does_not_decode_the_verified_temp_copy(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "plain-single-pass-vault"

        with mock.patch.object(
            storage,
            "_metadata_from_archived_temp",
            side_effect=AssertionError("plain JSONL used the Zstandard semantic pass"),
        ):
            result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["archived"]), 1)

    def test_zstd_manifest_requires_bounded_positive_decoded_bytes(self) -> None:
        vault, _, manifest = _write_embedded_archive(self.tmp_path)
        manifest_path = vault / "manifests" / f"{manifest['archive_id']}.json"
        for value in (None, 0, -1, True, 9 * 1024 * 1024 * 1024):
            with self.subTest(value=value):
                changed = dict(manifest)
                if value is None:
                    changed.pop("decoded_bytes")
                else:
                    changed["decoded_bytes"] = value
                manifest_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(storage.VaultError, "decoded_bytes"):
                    storage.resolve_archive(vault, manifest["archive_id"])

    def test_plain_manifest_remains_unchanged(self) -> None:
        home = make_codex_home(self.tmp_path)
        make_session(home)
        vault = self.tmp_path / "plain-vault"

        result = storage.archive(home, vault, older_than_days=1, execute=True)

        self.assertEqual(result["errors"], [])
        self.assertNotIn("decoded_bytes", storage.list_archives(vault)[0])


if __name__ == "__main__":
    unittest.main()
