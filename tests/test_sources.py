import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_vault.sources import (
    SessionGroup,
    default_source_root,
    discover,
    iter_text_records,
    metadata_from_bytes,
    tool_catalog,
    valid_relative,
)
from codex_vault.storage import VaultError


def _supports_large_integer_limit_test():
    getter = getattr(sys, "get_int_max_str_digits", None)
    if getter is None:
        return False
    try:
        limit = getter()
    except (AttributeError, ValueError):
        return False
    return isinstance(limit, int) and 0 < limit <= 5000


def _large_integer_digits():
    return max(5000, sys.get_int_max_str_digits() + 1)


class SourceAdapterTests(unittest.TestCase):
    def test_catalog_and_default_roots_are_metadata_only(self):
        ids = {item["id"] for item in tool_catalog()}
        self.assertTrue({"claude", "grok", "gemini", "agy", "glm", "opencode"}.issubset(ids))
        self.assertIsInstance(default_source_root("claude"), Path)
        self.assertIsNone(default_source_root("agy"))
        with mock.patch.dict(os.environ, {"GEMINI_CLI_HOME": "/tmp/gemini-fixture"}, clear=False):
            self.assertEqual(default_source_root("gemini"), Path("/tmp/gemini-fixture/.gemini"))

    def test_claude_group_contains_main_and_all_companions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "projects" / "-repo"
            project.mkdir(parents=True)
            main = project / "sid.jsonl"
            main.write_text('{"type":"user"}\n', encoding="utf-8")
            sidecar = project / "sid"
            (sidecar / "subagents").mkdir(parents=True)
            (sidecar / "tool-results").mkdir()
            (sidecar / "subagents" / "agent-a.jsonl").write_text('{}\n', encoding="utf-8")
            (sidecar / "agent-a.meta.json").write_text('{}', encoding="utf-8")
            (sidecar / "tool-results" / "tool-a.txt").write_text("result", encoding="utf-8")

            groups = discover("claude", root)

            self.assertEqual(len(groups), 1)
            group = groups[0]
            self.assertIsInstance(group, SessionGroup)
            self.assertEqual(group.session_id, "sid")
            self.assertEqual(set(group.files), {
                "projects/-repo/sid.jsonl",
                "projects/-repo/sid/subagents/agent-a.jsonl",
                "projects/-repo/sid/agent-a.meta.json",
                "projects/-repo/sid/tool-results/tool-a.txt",
            })
            self.assertEqual(set(group.root_entries), {"projects/-repo/sid.jsonl", "projects/-repo/sid"})
            self.assertTrue(group.removable)

    def test_claude_symlink_companion_is_not_silently_removable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "projects" / "-repo"
            project.mkdir(parents=True)
            (project / "sid.jsonl").write_text('{}\n', encoding="utf-8")
            sidecar = project / "sid"
            sidecar.mkdir()
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                (sidecar / "leak.txt").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")

            group = discover("claude", root)[0]

            self.assertFalse(group.removable)
            self.assertTrue(any("symlink" in note for note in group.notes))
            self.assertNotIn("projects/-repo/sid/leak.txt", group.files)

    def test_grok_session_is_one_complete_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "sessions" / "repo-slug" / "session-1"
            session.mkdir(parents=True)
            (session / "updates.jsonl").write_text('{"type":"update"}\n', encoding="utf-8")
            (session / "state.bin").write_bytes(b"\x00\x01")

            group = discover("grok", root)[0]

            self.assertEqual(group.root_entries, ("sessions/repo-slug/session-1",))
            self.assertEqual(group.cwd, "")
            self.assertEqual(set(group.files), {
                "sessions/repo-slug/session-1/updates.jsonl",
                "sessions/repo-slug/session-1/state.bin",
            })
            self.assertEqual(group.formats["sessions/repo-slug/session-1/updates.jsonl"], "grok-acp-jsonl")
            self.assertEqual(group.formats["sessions/repo-slug/session-1/state.bin"], "binary")

            encoded = root / "sessions" / "%2Fwork%2Fapp" / "session-2"
            encoded.mkdir(parents=True)
            (encoded / "updates.jsonl").write_text('{}\n', encoding="utf-8")
            encoded_group = next(item for item in discover("grok", root) if item.session_id == "session-2")
            self.assertEqual(encoded_group.cwd, "/work/app")

    def test_gemini_and_exports_are_scoped_without_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tmp" / "project-hash" / "chats").mkdir(parents=True)
            (root / "tmp" / "project-hash" / "chats" / "session-abc.json").write_text("{}", encoding="utf-8")
            (root / "tmp" / "project-hash" / "settings.json").write_text("{}", encoding="utf-8")
            gemini = discover("gemini", root)
            self.assertEqual(len(gemini), 1)
            self.assertEqual(gemini[0].files, ("tmp/project-hash/chats/session-abc.json",))

            export = root / "export"
            export.mkdir()
            (export / "chat.md").write_text("hello", encoding="utf-8")
            (export / "store.sqlite").write_bytes(b"SQLite format 3\x00")
            (export / "debug.log").write_text("should not be selected", encoding="utf-8")
            (export / "private.pem").write_text("key", encoding="utf-8")
            groups = discover("agy", export)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].source_mode, "export")
            self.assertEqual(groups[0].files, ("chat.md",))

    def test_export_skips_credentials_and_hidden_directories_but_keeps_normal_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "exports"
            root.mkdir()
            (root / "chat.md").write_text("login auth discussion", encoding="utf-8")
            for name in (".credentials.json", "oauth_creds.json", "token.json", "google_accounts.json"):
                (root / name).write_text("secret", encoding="utf-8")
            hidden = root / ".hidden"
            hidden.mkdir()
            (hidden / "chat.md").write_text("hidden", encoding="utf-8")

            groups = discover("export", root)

            self.assertEqual([path for group in groups for path in group.files], ["chat.md"])

    def test_export_rejects_suffix_credential_names_and_native_signatures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "exports"
            root.mkdir()
            (root / "chat.md").write_text("safe", encoding="utf-8")
            for name in ("foo.token.json", "foo.credentials.json", "foo.oauth.json", "foo.secret.json", "foo.account.json"):
                (root / name).write_text("secret", encoding="utf-8")
            self.assertEqual([path for group in discover("export", root) for path in group.files], ["chat.md"])

            native = Path(temp) / "moved-native"
            (native / "projects").mkdir(parents=True)
            (native / ".credentials.json").write_text("secret", encoding="utf-8")
            with self.assertRaisesRegex(VaultError, "native"):
                discover("export", native)

            opencode = Path(temp) / "opencode"
            for name in ("session", "message", "part"):
                (opencode / "storage" / name).mkdir(parents=True)
            with self.assertRaisesRegex(VaultError, "native"):
                discover("export", opencode)

    def test_export_credential_tokens_are_separator_aware(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "exports"
            root.mkdir()
            (root / "chat.md").write_text("normal", encoding="utf-8")
            for name in ("mcp_credentials.json", "client_secret.json", "access_token.json", "refresh-token.json", "api_key.json"):
                (root / name).write_text("secret", encoding="utf-8")
            (root / "tokenization.json").write_text("normal export", encoding="utf-8")

            groups = discover("export", root)

            self.assertEqual([path for group in groups for path in group.files], ["chat.md", "tokenization.json"])

    def test_relocated_native_descendants_and_moved_group_are_rejected_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            native_paths = [
                base / ".claude" / "projects",
                base / ".grok" / "sessions",
                base / ".gemini" / "tmp" / "hash" / "chats",
                base / "opencode" / "storage" / "message",
            ]
            for path in native_paths:
                path.mkdir(parents=True)
            moved = base / "moved-native"
            (moved / "projects" / "repo" / "sid").mkdir(parents=True)
            (moved / ".credentials.json").write_text("secret", encoding="utf-8")
            (moved / "projects" / "repo" / "sid.jsonl").write_text('{}\n', encoding="utf-8")
            (moved / "projects" / "repo" / "sid" / "tool.txt").write_text("tool", encoding="utf-8")
            deep = moved / "projects" / "repo" / "sid" / "tool-results" / "a" / "b" / "c"
            deep.mkdir(parents=True)
            (deep / "export.md").write_text("tool", encoding="utf-8")

            with mock.patch.object(Path, "open", side_effect=AssertionError("discovery read a file")):
                for path in native_paths:
                    with self.subTest(path=path):
                        with self.assertRaisesRegex(VaultError, "native"):
                            discover("export", path)
                for candidate in (moved, moved / "projects", moved / "projects" / "repo", moved / "projects" / "repo" / "sid", deep):
                    with self.subTest(candidate=candidate):
                        with self.assertRaisesRegex(VaultError, "native"):
                            discover("export", candidate)

    def test_export_rejects_native_named_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".claude"
            root.mkdir()
            (root / "chat.md").write_text("export", encoding="utf-8")

            with self.assertRaisesRegex(VaultError, "native|source root"):
                discover("export", root)

    def test_valid_relative_rejects_traversal_and_credentials(self):
        self.assertTrue(valid_relative("claude", "projects/p/sid.jsonl", "native"))
        self.assertFalse(valid_relative("claude", "projects/p/../auth.json", "native"))
        self.assertFalse(valid_relative("agy", ".env", "export"))
        self.assertFalse(valid_relative("grok", "config.json", "native"))
        self.assertFalse(valid_relative("claude", "projects", "native"))
        self.assertFalse(valid_relative("agy", "chat//export.md", "export"))
        self.assertFalse(valid_relative("agy", "./chat.md", "export"))
        self.assertFalse(valid_relative("agy", "secret.pem", "export"))
        self.assertFalse(valid_relative("agy", ".credentials.json", "export"))
        self.assertFalse(valid_relative("export", ".hidden/chat.md", "export"))

    def test_iter_text_records_handles_claude_grok_and_images(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "한국어 hello"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "echo hello", "file_path": "src/한글.txt"}},
                {"type": "image", "source": {"data": "base64-secret"}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "도구 결과 한국어"},
                {"type": "image", "source": {"data": "base64-secret-2"}},
            ]}},
        ]
        data = b"\n".join(json.dumps(line, ensure_ascii=False).encode() for line in lines) + b"\n"
        records = list(iter_text_records(io.BytesIO(data), "claude-jsonl"))
        texts = [record["text"] for record in records if "text" in record]
        self.assertIn("한국어 hello", texts)
        self.assertTrue(any("echo hello" in text for text in texts))
        self.assertTrue(any("src/한글.txt" in text for text in texts))
        self.assertTrue(any("도구 결과 한국어" in text for text in texts))
        self.assertTrue(all("base64-secret" not in text for text in texts))

        grok_lines = [
            {"timestamp": "2026-01-01", "method": "session/update", "params": {"sessionId": "s", "update": {"sessionUpdate": {"kind": "user_message_chunk", "content": {"text": "ACP 한국어 user"}}}}},
            {"timestamp": "2026-01-01", "method": "session/update", "params": {"sessionId": "s", "update": {"sessionUpdate": {"kind": "agent_message_chunk", "content": {"text": "ACP assistant"}}}}},
            {"timestamp": "2026-01-01", "method": "session/update", "params": {"sessionId": "s", "update": {"sessionUpdate": {"kind": "tool_call", "title": "read", "rawInput": {"file_path": "src/한글.txt"}}}}},
            {"timestamp": "2026-01-01", "method": "session/update", "params": {"sessionId": "s", "update": {"sessionUpdate": {"kind": "tool_call_update", "content": [{"type": "text", "text": "도구 출력 한국어"}]}}}},
            {"timestamp": "2026-01-01", "method": "session/update", "params": {"sessionId": "s", "update": {"sessionUpdate": {"kind": "agent_thought_chunk", "content": {"text": "private thought"}}}}},
        ]
        grok_data = b"\n".join(json.dumps(line, ensure_ascii=False).encode() for line in grok_lines) + b"\n"
        grok = list(iter_text_records(io.BytesIO(grok_data), "grok-acp-jsonl"))
        grok_texts = [record["text"] for record in grok if "text" in record]
        self.assertIn("ACP 한국어 user", grok_texts)
        self.assertIn("ACP assistant", grok_texts)
        self.assertTrue(any("src/한글.txt" in text for text in grok_texts))
        self.assertIn("도구 출력 한국어", grok_texts)
        self.assertNotIn("private thought", grok_texts)
        plain = list(iter_text_records(io.BytesIO("평문 export\n".encode()), "text"))
        self.assertEqual(plain[0]["text"], "평문 export")
        opencode = list(iter_text_records(io.BytesIO(json.dumps({"info": {}, "messages": [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "OpenCode hello"}]}]}).encode()), "opencode-json"))
        self.assertIn("OpenCode hello", [record["text"] for record in opencode])
        state = list(iter_text_records(io.BytesIO(json.dumps({"messages": [{"state": {"output": "tool state output", "input": "tool state input"}}]}).encode()), "common-json"))
        self.assertTrue(any("tool state output" in record["text"] for record in state if "text" in record))
        chat_list = list(iter_text_records(io.BytesIO(json.dumps([{"role": "user", "content": "list chat"}]).encode()), "common-json"))
        self.assertEqual(chat_list[0]["text"], "list chat")

    def test_grok_skips_private_updates_in_string_and_object_shapes(self):
        lines = [
            {"update": {"sessionUpdate": "agent_thought_chunk", "content": {"text": "private string thought"}}},
            {"update": {"sessionUpdate": "hook_execution", "content": {"text": "private string hook"}}},
            {"update": {"sessionUpdate": {"kind": "agent_thought_chunk", "content": {"text": "private object thought"}}}},
            {"update": {"sessionUpdate": {"kind": "hook_execution", "content": {"text": "private object hook"}}}},
            {"update": {"sessionUpdate": "user_message_chunk", "content": {"text": "visible user"}}},
        ]
        data = b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"

        records = list(iter_text_records(io.BytesIO(data), "grok-acp-jsonl"))
        texts = [record["text"] for record in records if "text" in record]

        self.assertEqual(texts, ["visible user"])
        self.assertEqual(sum(record.get("reason") == "no readable text" for record in records), 4)

    @unittest.skipUnless(_supports_large_integer_limit_test(), "interpreter has no bounded integer conversion limit")
    def test_jsonl_large_integer_skips_record_and_continues(self):
        digits = _large_integer_digits()
        data = b"\n".join([
            b'{"content":"before"}',
            b'{"value":' + b"9" * digits + b'}',
            b'{"content":"after"}',
        ]) + b"\n"

        records = list(iter_text_records(io.BytesIO(data), "generic-jsonl"))
        self.assertEqual([record["text"] for record in records if "text" in record], ["before", "after"])
        self.assertEqual(records[1], {"line": 2, "skipped": True, "reason": "unsupported JSON record"})

    @unittest.skipUnless(_supports_large_integer_limit_test(), "interpreter has no bounded integer conversion limit")
    def test_full_json_large_integer_is_skipped(self):
        data = b'{"messages":[{"content":"before"},{"value":' + b"9" * _large_integer_digits() + b'}]}'

        records = list(iter_text_records(io.BytesIO(data), "common-json"))
        self.assertEqual(records, [{"line": 1, "skipped": True, "reason": "unsupported JSON document"}])

    @unittest.skipUnless(_supports_large_integer_limit_test(), "interpreter has no bounded integer conversion limit")
    def test_metadata_skips_large_integer_and_reads_later_jsonl_metadata(self):
        digits = _large_integer_digits()
        data = b"\n".join([
            b'{"value":' + b"9" * digits + b'}',
            b'{"sessionId":"after-large-int","cwd":"/repo"}',
        ]) + b"\n"

        self.assertEqual(
            metadata_from_bytes(data, "generic-jsonl"),
            {"session_id": "after-large-int", "cwd": "/repo"},
        )
        full_data = b'{"sessionId":"before-large-int","value":' + b"9" * digits + b'}'
        self.assertEqual(
            metadata_from_bytes(full_data, "common-json"),
            {"session_id": "before-large-int"},
        )

    def test_injected_json_decoder_value_error_is_skipped_portably(self):
        real_loads = json.loads

        def loads(value):
            if '"unsupported"' in value:
                raise ValueError("injected decoder failure")
            return real_loads(value)

        with mock.patch("codex_vault.sources.json.loads", side_effect=loads):
            jsonl = list(iter_text_records(io.BytesIO(
                b'{"content":"before"}\n{"unsupported":true}\n{"content":"after"}\n'
            ), "generic-jsonl"))
            self.assertEqual([record["text"] for record in jsonl if "text" in record], ["before", "after"])
            self.assertEqual(jsonl[1]["reason"], "unsupported JSON record")

            full = list(iter_text_records(io.BytesIO(b'{"unsupported":true}'), "common-json"))
            self.assertEqual(full, [{"line": 1, "skipped": True, "reason": "unsupported JSON document"}])

            metadata = metadata_from_bytes(
                b'{"unsupported":true}\n{"sessionId":"after-injected-error"}\n',
                "generic-jsonl",
            )
            self.assertEqual(metadata, {"session_id": "after-injected-error"})

    def test_unrelated_record_processing_value_error_is_not_suppressed(self):
        with mock.patch("codex_vault.sources._record_text", side_effect=ValueError("unrelated record failure")):
            with self.assertRaisesRegex(ValueError, "unrelated record failure"):
                list(iter_text_records(io.BytesIO(b'{"content":"text"}\n'), "generic-jsonl"))

    def test_gemini_tool_call_results_are_searchable_and_images_are_skipped(self):
        sample = {
            "messages": [{
                "type": "gemini",
                "content": "",
                "toolCalls": [
                    {"name": "run_shell_command", "resultDisplay": "도구 출력 한글"},
                    {"name": "read_file", "result": {"parts": [{"text": "파일 결과 한글"}]}},
                    {"name": "image", "result": {"parts": [{"inlineData": {"data": "base64-secret"}}]}},
                ],
            }],
        }
        records = list(iter_text_records(io.BytesIO(json.dumps(sample, ensure_ascii=False).encode()), "gemini-json"))
        texts = [record["text"] for record in records if "text" in record]
        self.assertTrue(any("도구 출력 한글" in text for text in texts))
        self.assertTrue(any("파일 결과 한글" in text for text in texts))
        self.assertTrue(all("base64-secret" not in text for text in texts))

    def test_native_credential_member_makes_group_nonremovable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "sessions" / "repo" / "sid"
            session.mkdir(parents=True)
            (session / "updates.jsonl").write_text('{}\n', encoding="utf-8")
            (session / "auth.pem").write_text("secret", encoding="utf-8")

            group = discover("grok", root)[0]

            self.assertFalse(group.removable)
            self.assertTrue(any("outside" in note for note in group.notes))
            self.assertNotIn("sessions/repo/sid/auth.pem", group.files)

    def test_export_root_path_safety_uses_mocked_native_defaults_without_reading_files(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            native = temp_root / "native"
            (native / "sessions").mkdir(parents=True)
            export_parent = temp_root
            export_child = native / "sessions"
            with mock.patch("codex_vault.sources._known_native_roots", return_value=[native]), \
                    mock.patch.object(Path, "open", side_effect=AssertionError("discovery read a file")):
                with self.assertRaisesRegex(VaultError, "native"):
                    discover("export", native)
                with self.assertRaisesRegex(VaultError, "native"):
                    discover("export", export_parent)
                with self.assertRaisesRegex(VaultError, "native"):
                    discover("export", export_child)

    def test_iter_text_records_bounds_json_and_binary(self):
        huge = b'{"messages":[' + b'"x"' * (11 * 1024 * 1024) + b']}'
        result = list(iter_text_records(io.BytesIO(huge), "common-json"))
        self.assertTrue(result[0]["skipped"])
        self.assertEqual(list(iter_text_records(io.BytesIO(b"\x00\x01"), "binary"))[0]["skipped"], True)

    def test_metadata_is_best_effort_from_bytes_only(self):
        data = b'{"type":"user","sessionId":"s1","cwd":"/repo","version":"1.2","timestamp":"2026-01-01"}\n'
        metadata = metadata_from_bytes(data, "claude-jsonl")
        self.assertEqual(metadata["session_id"], "s1")
        self.assertEqual(metadata["cwd"], "/repo")
        self.assertEqual(metadata["tool_version"], "1.2")

    def test_metadata_recovers_scalar_from_truncated_gemini_prefix(self):
        data = b'{"sessionId":"gemini-prefix","messages":[' + b'"noise",' * 30000
        metadata = metadata_from_bytes(data[:64 * 1024], "gemini-json")
        self.assertEqual(metadata["session_id"], "gemini-prefix")


if __name__ == "__main__":
    unittest.main()
