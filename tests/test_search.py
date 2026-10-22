import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_vault.search as search_module
from codex_vault.search import VaultError, search, show
from codex_vault.storage import _archive_id


def _archive_fixture(tmp_path: Path, records: list[dict | bytes]) -> tuple[Path, dict]:
    vault = tmp_path / "vault"
    blob_dir = vault / "blobs"
    blob_dir.mkdir(parents=True)
    raw = b"".join(
        record if isinstance(record, bytes) else json.dumps(record, ensure_ascii=False).encode() + b"\n"
        for record in records
    )
    digest = hashlib.sha256(raw).hexdigest()
    source_root = str(tmp_path / "codex")
    relative_path = "sessions/2026/10/16/rollout-test.jsonl"
    archive_id = _archive_id(source_root, relative_path, digest)
    blob = blob_dir / f"{digest}.jsonl.gz"
    with gzip.open(blob, "wb") as stream:
        stream.write(raw)
    manifest = {
        "archive_id": archive_id,
        "blob_sha256": digest,
        "blob": f"blobs/{digest}.jsonl.gz",
        "source_root": source_root,
        "relative_path": relative_path,
        "session_id": "session-korean",
        "cwd": "/tmp/project-korean",
        "tool_version": "0.1",
        "started_at": "2026-10-16T00:00:00Z",
        "raw_bytes": len(raw),
        "compressed_bytes": blob.stat().st_size,
        "source_mtime_ns": 1,
        "source_mode": 0o600,
        "archived_at": "2026-10-16T00:00:01Z",
    }
    return vault, manifest


def _fixture_lines(vault_dir: Path, manifest: dict, max_record_bytes: int = 1024 * 1024):
    with gzip.open(vault_dir / manifest["blob"], "rb") as stream:
        line_number = 0
        while True:
            line = stream.readline(max_record_bytes + 1)
            if not line:
                return
            line_number += 1
            if len(line) > max_record_bytes:
                while not line.endswith(b"\n"):
                    line = stream.readline(64 * 1024)
                    if not line:
                        break
                yield line_number, None
            else:
                yield line_number, line


def _write_manifest(vault: Path, manifest: dict) -> None:
    manifests = vault / "manifests"
    manifests.mkdir(exist_ok=True)
    (manifests / f"{manifest['archive_id']}.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )


class SearchTests(unittest.TestCase):
    def setUp(self):
        # These fixtures exercise search's bounded reader independently of the
        # storage implementation; integration tests cover its helper wiring.
        self._storage_lines = search_module._storage_iter_archive_lines
        search_module._storage_iter_archive_lines = _fixture_lines
        self.addCleanup(self._restore_storage_reader)

    def _restore_storage_reader(self):
        search_module._storage_iter_archive_lines = self._storage_lines

    def test_casefold_span_maps_unicode_expansions_without_per_character_offsets(self):
        self.assertEqual(search_module._casefold_span("STRASSE", "straße"), (0, 7))
        self.assertEqual(search_module._casefold_span("ß", "s"), (0, 1))
        self.assertEqual(search_module._casefold_span("ß" * 1000 + "TARGET", "target"), (1000, 1006))
        self.assertIsNone(search_module._casefold_span("straße", "missing"))

    def test_searches_readable_codex_records_and_filters_literal_unicode(self):
        records = [
            {"type": "response_item", "payload": {"type": "message", "content": [
                {"type": "input_text", "text": "한국어 검색어: foo.bar"},
                {"type": "output_text", "text": "결과입니다"},
            ]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": '{"cmd":"foo.bar"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "명령 결과 foo.bar"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "input": "입력", "output": "도구 foo.bar"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": [
                {"type": "output_text", "text": "custom foo.bar"},
                {"type": "image", "data": "base64-secret"},
            ]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "사용자 foo.bar"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "에이전트 foo.bar"}},
            {"type": "event_msg", "payload": {"type": "compacted", "summary": "요약 foo.bar"}},
            {"type": "compacted", "payload": {"message": "top-level foo.bar"}},
            {"type": "session_meta", "payload": {"encrypted_content": "foo.bar"}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), records)
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                result = search(vault, "FOO.BAR")
                self.assertEqual(result["query"], "FOO.BAR")
                self.assertEqual(result["scanned_archives"], 1)
                self.assertEqual(len(result["matches"]), 9)
                self.assertTrue(all("foo.bar" in match["snippet"].lower() for match in result["matches"]))
                self.assertEqual(search(vault, "한국어")["matches"][0]["session_id"], "session-korean")
                self.assertTrue(search(vault, "foo.bar", project="project-korean")["matches"])
                self.assertFalse(search(vault, "foo.bar", project="other")["matches"])
                self.assertFalse(search(vault, "foo.bar", session_id="other")["matches"])

    def test_search_limit_and_archive_prefix_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(
                Path(temp),
                [{"type": "event_msg", "payload": {"type": "user_message", "message": "needle"}}],
            )
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                self.assertEqual(len(search(vault, "needle", limit=1)["matches"]), 1)
                self.assertTrue(search(vault, "needle", session_id=manifest["archive_id"][:8])["matches"])
                self.assertFalse(search(vault, "needle", limit=1)["truncated"])
                with self.assertRaisesRegex(VaultError, "query"):
                    search(vault, "")
                with self.assertRaisesRegex(VaultError, "limit"):
                    search(vault, "needle", limit=0)

    def test_limit_reports_truncation_and_snippets_strip_terminal_controls(self):
        records = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "\x1b[31mneedle\x1b[0m"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "another needle"}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), records)
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                result = search(vault, "needle", limit=1)
                self.assertEqual(len(result["matches"]), 1)
                self.assertTrue(result["truncated"])
                self.assertNotIn("\x1b", result["matches"][0]["snippet"])

    def test_show_returns_readable_records_without_sensitive_payloads(self):
        records = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
            {"type": "response_item", "payload": {"type": "message", "content": [{"type": "output_text", "text": "world"}]}},
            {"type": "session_meta", "payload": {"token_count": 123, "image": "data:image/png;base64,secret"}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), records)
            with patch("codex_vault.search.resolve_archive", return_value=manifest):
                result = show(vault, manifest["archive_id"])
                self.assertEqual([record["text"] for record in result["records"]], ["hello", "world"])
                self.assertTrue(all("secret" not in record["text"] for record in result["records"]))

    def test_unsupported_records_are_counted_even_after_result_limit(self):
        records = [
            {"type": "session_meta", "payload": {"id": "synthetic"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "needle one"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "needle two"}},
            {"type": "unknown_future_record", "payload": {"opaque": True}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), records)
            with patch("codex_vault.search.list_archives", return_value=[manifest]), \
                 patch("codex_vault.search.resolve_archive", return_value=manifest):
                for result in (search(vault, "needle", limit=1), show(vault, manifest["archive_id"], limit=1)):
                    self.assertTrue(result["truncated"])
                    self.assertEqual(result["skipped_records"], 2)

    def test_corrupt_gzip_and_invalid_json_are_explicit_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), [{"type": "event_msg", "payload": {"type": "user_message", "message": "secret"}}])
            (vault / manifest["blob"]).write_bytes(b"not gzip secret")
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                with self.assertRaisesRegex(VaultError, "corrupt gzip") as error:
                    search(vault, "secret")
                self.assertNotIn("secret", str(error.exception))

        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), [{"type": "event_msg", "payload": {"type": "user_message", "message": "secret"}}])
            with gzip.open(vault / manifest["blob"], "wb") as stream:
                stream.write(b'{"broken":\n')
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                with self.assertRaisesRegex(VaultError, "invalid JSON") as error:
                    search(vault, "secret")
                self.assertNotIn("secret", str(error.exception))

    def test_zstandard_errors_are_not_mislabeled_as_gzip_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), [])
            with patch("codex_vault.search.list_archives", return_value=[manifest]), \
                 patch.object(search_module, "_storage_iter_archive_lines", side_effect=VaultError("Zstandard content is corrupt or truncated")):
                with self.assertRaisesRegex(VaultError, "Zstandard") as error:
                    search(vault, "needle")
                self.assertNotIn("gzip", str(error.exception))

    def test_very_long_record_is_skipped_with_count(self):
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(
                Path(temp),
                [{"type": "event_msg", "payload": {"type": "user_message", "message": "x" * (2 * 1024 * 1024)}}],
            )
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                result = search(vault, "x")
                self.assertEqual(result["matches"], [])
                self.assertEqual(result["skipped_records"], 1)

    def test_deeply_nested_json_reports_safe_error(self):
        nested = "[" * 1500 + '"x"' + "]" * 1500
        raw_record = (
            b'{"type":"response_item","payload":{"type":"custom_tool_call_output",'
            b'"output":' + nested.encode("ascii") + b"}}\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), [raw_record])
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                with self.assertRaisesRegex(VaultError, "deeply nested JSON") as error:
                    search(vault, "x")
                self.assertNotIn("x", str(error.exception))

    def test_removed_source_does_not_affect_archive_search(self):
        with tempfile.TemporaryDirectory() as temp:
            vault, manifest = _archive_fixture(Path(temp), [{"type": "event_msg", "payload": {"type": "user_message", "message": "보관됨"}}])
            source = Path(manifest["source_root"]) / manifest["relative_path"]
            with patch("codex_vault.search.list_archives", return_value=[manifest]):
                result = search(vault, "보관됨")
                self.assertTrue(result["matches"][0]["relative_path"].endswith(".jsonl"))
                self.assertFalse(source.exists())

    def test_real_storage_prefix_show_and_corrupt_tail_is_detected(self):
        records = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "needle"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "tail"}},
        ]
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            vault, manifest = _archive_fixture(Path(temp), records)
            _write_manifest(vault, manifest)
            with patch.object(search_module, "_storage_iter_archive_lines", self._storage_lines):
                shown = show(vault, manifest["archive_id"][:12], limit=1)
                self.assertEqual(shown["archive_id"], manifest["archive_id"])
                self.assertEqual(shown["records"][0]["text"], "needle")

                blob = vault / manifest["blob"]
                encoded = blob.read_bytes()
                blob.write_bytes(encoded[:-1] + bytes([encoded[-1] ^ 0xFF]))
                with self.assertRaisesRegex(VaultError, "corrupt gzip"):
                    search(vault, "needle", limit=1)


if __name__ == "__main__":
    unittest.main()
