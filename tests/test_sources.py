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
        self.assertTrue({"claude", "grok", "gemini", "agy", "glm", "opencode", "zcode", "autoclaw"}.issubset(ids))
        self.assertIsInstance(default_source_root("claude"), Path)
        self.assertIsNone(default_source_root("agy"))
        self.assertIsNone(default_source_root("zcode"))
        self.assertIsNone(default_source_root("autoclaw"))
        diagnostic = {item["id"]: item for item in tool_catalog() if item["id"] in {"zcode", "autoclaw"}}
        self.assertEqual({item["mode"] for item in diagnostic.values()}, {"diagnostic"})
        self.assertEqual({item["default_root"] for item in diagnostic.values()}, {None})
        with mock.patch.dict(os.environ, {"GEMINI_CLI_HOME": "/tmp/gemini-fixture"}, clear=False):
            self.assertEqual(default_source_root("gemini"), Path("/tmp/gemini-fixture/.gemini"))

    def test_diagnostic_discovery_is_flat_scoped_and_marks_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "diagnostic-logs"
            root.mkdir()
            (root / "model-io-request_1.jsonl").write_text('{"type":"model_io"}\n', encoding="utf-8")
            (root / ("model-io-" + "x" * 81 + ".jsonl")).write_text("{}\n", encoding="utf-8")
            (root / "model-io-headers.jsonl").write_text('{"headers":{"Authorization":"secret"}}\n', encoding="utf-8")
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "store.sqlite").write_bytes(b"SQLite format 3\x00")
            nested = root / "nested"
            nested.mkdir()
            (nested / "model-io-nested.jsonl").write_text("{}\n", encoding="utf-8")
            outside = Path(temp) / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            try:
                (root / "model-io-link.jsonl").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")

            groups = discover("zcode", root)

            self.assertEqual([group.session_id for group in groups], ["model-io-headers", "model-io-link", "model-io-request_1"])
            self.assertEqual(groups[0].files, ("model-io-headers.jsonl",))
            symlink = next(group for group in groups if group.session_id == "model-io-link")
            self.assertEqual(symlink.files, ())
            self.assertFalse(symlink.removable)
            self.assertTrue(any("symlink" in note for note in symlink.notes))
            self.assertEqual(next(group for group in groups if group.session_id == "model-io-request_1").source_mode, "diagnostic")
            self.assertFalse(any("nested" in path for group in groups for path in group.files))
            self.assertFalse(any(path.endswith("config.json") or path.endswith("store.sqlite") for group in groups for path in group.files))

    def test_diagnostic_root_rejects_home_native_roots_and_symlink_components(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            native = home / ".zcode"
            log_root = native / "cli" / "rollout"
            log_root.mkdir(parents=True)
            (log_root / "model-io-safe.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch("codex_vault.sources.Path.home", return_value=home):
                self.assertEqual(len(discover("zcode", log_root)), 1)
                self.assertEqual(len(discover("autoclaw", log_root)), 1)
                for unsafe in (home, native, native / "cli"):
                    unsafe.mkdir(parents=True, exist_ok=True)
                    with self.subTest(unsafe=unsafe), self.assertRaisesRegex(VaultError, "diagnostic|home|native"):
                        discover("zcode", unsafe)

                codex_root = home / ".codex" / "diagnostics"
                codex_root.mkdir(parents=True)
                (codex_root / "model-io-codex.jsonl").write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(VaultError, "native"):
                    discover("autoclaw", codex_root)

            real_root = Path(temp) / "real-root"
            real_root.mkdir()
            linked_parent = Path(temp) / "linked-parent"
            try:
                linked_parent.symlink_to(real_root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(VaultError, "symlink"):
                discover("zcode", linked_parent)

    def test_diagnostic_rejects_relocated_native_root_from_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            relocated = Path(temp) / "claude-profile"
            relocated.mkdir()
            (relocated / "model-io-live.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(relocated)}, clear=False):
                with self.assertRaisesRegex(VaultError, "native"):
                    discover("zcode", relocated)

    def test_diagnostic_relative_validation_is_tool_and_mode_specific(self):
        self.assertTrue(valid_relative("zcode", "model-io-safe_1.jsonl", "diagnostic"))
        self.assertTrue(valid_relative("autoclaw", "model-io-safe_1.jsonl", "diagnostic"))
        self.assertFalse(valid_relative("zcode", "nested/model-io-safe.jsonl", "diagnostic"))
        self.assertFalse(valid_relative("zcode", "model-io-.jsonl", "diagnostic"))
        self.assertFalse(valid_relative("zcode", "model-io-safe.json", "diagnostic"))
        self.assertFalse(valid_relative("agy", "model-io-safe.jsonl", "diagnostic"))
        self.assertFalse(valid_relative("zcode", "model-io-safe.jsonl", "native"))

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

    def test_native_discovery_keeps_healthy_siblings_when_project_is_unreadable(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)

            claude_root = base / "claude"
            claude_bad = claude_root / "projects" / "bad"
            claude_good = claude_root / "projects" / "good"
            claude_bad.mkdir(parents=True)
            claude_good.mkdir(parents=True)
            (claude_good / "healthy.jsonl").write_text('{}\n', encoding="utf-8")
            real_scandir = os.scandir

            def claude_scandir(path):
                if Path(path) == claude_bad:
                    raise PermissionError("synthetic unreadable Claude project")
                return real_scandir(path)

            with mock.patch("codex_vault.sources.os.scandir", side_effect=claude_scandir):
                claude_groups = discover("claude", claude_root)
            claude_bad_group = next(group for group in claude_groups if "bad" in group.root_entries[0])
            self.assertFalse(claude_bad_group.files)
            self.assertFalse(claude_bad_group.removable)
            self.assertTrue(any("unreadable directory" in note for note in claude_bad_group.notes))
            self.assertTrue(any(group.files == ("projects/good/healthy.jsonl",) for group in claude_groups))

            grok_root = base / "grok"
            grok_bad = grok_root / "sessions" / "bad"
            grok_good = grok_root / "sessions" / "good"
            grok_bad.mkdir(parents=True)
            (grok_good / "healthy").mkdir(parents=True)
            (grok_good / "healthy" / "updates.jsonl").write_text('{}\n', encoding="utf-8")
            real_iterdir = Path.iterdir

            def grok_iterdir(path):
                if path == grok_bad:
                    raise PermissionError("synthetic unreadable Grok project")
                return real_iterdir(path)

            with mock.patch.object(Path, "iterdir", grok_iterdir):
                grok_groups = discover("grok", grok_root)
            grok_bad_group = next(group for group in grok_groups if "sessions/bad" in group.root_entries[0])
            self.assertFalse(grok_bad_group.files)
            self.assertFalse(grok_bad_group.removable)
            self.assertTrue(any("unreadable directory" in note for note in grok_bad_group.notes))
            self.assertTrue(any(group.files == ("sessions/good/healthy/updates.jsonl",) for group in grok_groups))

            gemini_root = base / "gemini"
            gemini_bad = gemini_root / "tmp" / "bad" / "chats"
            gemini_good = gemini_root / "tmp" / "good" / "chats"
            gemini_bad.mkdir(parents=True)
            gemini_good.mkdir(parents=True)
            (gemini_good / "session-healthy.json").write_text('{}', encoding="utf-8")

            def gemini_iterdir(path):
                if path == gemini_bad:
                    raise PermissionError("synthetic unreadable Gemini project")
                return real_iterdir(path)

            with mock.patch.object(Path, "iterdir", gemini_iterdir):
                gemini_groups = discover("gemini", gemini_root)
            gemini_bad_group = next(group for group in gemini_groups if "tmp/bad/chats" in group.root_entries[0])
            self.assertFalse(gemini_bad_group.files)
            self.assertFalse(gemini_bad_group.removable)
            self.assertTrue(any("unreadable directory" in note for note in gemini_bad_group.notes))
            self.assertTrue(any(group.files == ("tmp/good/chats/session-healthy.json",) for group in gemini_groups))

    def test_discovery_warning_does_not_block_filtered_codex_or_export_file(self):
        """Walk diagnostics stay separate from healthy files filtered by the adapter."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            codex_root = base / "codex"
            codex_scope = codex_root / "sessions" / "project"
            blocked_codex = codex_scope / "blocked"
            blocked_codex.mkdir(parents=True)
            (codex_scope / "a.db").write_bytes(b"SQLite format 3\x00")
            (codex_scope / "rollout-healthy.jsonl").write_text('{}\n', encoding="utf-8")

            export_root = base / "exports"
            blocked_export = export_root / "blocked"
            blocked_export.mkdir(parents=True)
            (export_root / "a.db").write_bytes(b"SQLite format 3\x00")
            (export_root / "healthy.md").write_text("healthy", encoding="utf-8")

            real_scandir = os.scandir

            def blocked_scandir(path):
                if Path(path) in {blocked_codex, blocked_export}:
                    raise PermissionError("synthetic unreadable directory")
                return real_scandir(path)

            # Discovery is metadata-only; no archive call or transcript bytes are read.
            with mock.patch("codex_vault.sources.os.scandir", side_effect=blocked_scandir):
                codex_groups = discover("codex", codex_root)
                export_groups = discover("export", export_root)

            codex_healthy = next(group for group in codex_groups if group.files == ("sessions/project/rollout-healthy.jsonl",))
            self.assertTrue(codex_healthy.removable)
            self.assertFalse(codex_healthy.notes)
            codex_warning = next(group for group in codex_groups if not group.root_entries)
            self.assertFalse(codex_warning.removable)
            self.assertTrue(any("unreadable directory" in note for note in codex_warning.notes))

            export_healthy = next(group for group in export_groups if group.files == ("healthy.md",))
            self.assertTrue(export_healthy.removable)
            self.assertFalse(export_healthy.notes)
            export_warning = next(group for group in export_groups if not group.root_entries)
            self.assertFalse(export_warning.removable)
            self.assertTrue(any("unreadable directory" in note for note in export_warning.notes))

    def test_codex_discovery_matches_legacy_rollout_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex"
            sessions = root / "sessions" / "project"
            archived = root / "archived_sessions" / "old"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            (sessions / "rollout-plain.jsonl").write_bytes(b"synthetic\n")
            (sessions / "rollout-compressed.jsonl.zst").write_bytes(b"synthetic zstd bytes")
            (sessions / "notes.jsonl").write_bytes(b"outside rollout scope\n")
            (sessions / "rollout-not-jsonl.txt").write_bytes(b"outside rollout scope\n")
            (archived / "rollout-old.jsonl").write_bytes(b"synthetic archived\n")

            groups = discover("codex", root)

            self.assertEqual(
                sorted(path for group in groups for path in group.files),
                [
                    "archived_sessions/old/rollout-old.jsonl",
                    "sessions/project/rollout-compressed.jsonl.zst",
                    "sessions/project/rollout-plain.jsonl",
                ],
            )

    def test_diagnostic_scope_warning_has_no_fake_root_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "diagnostic-logs"
            root.mkdir()
            real_scandir = os.scandir

            def blocked_scandir(path):
                if Path(path) == root:
                    raise PermissionError("synthetic unreadable diagnostic root")
                return real_scandir(path)

            with mock.patch("codex_vault.sources.os.scandir", side_effect=blocked_scandir):
                groups = discover("zcode", root)

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].root_entries, ())
            self.assertFalse(groups[0].removable)
            self.assertTrue(any("unreadable directory" in note for note in groups[0].notes))

    def test_native_discovery_reports_inaccessible_top_level_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)

            claude_root = base / "claude"
            (claude_root / "projects").mkdir(parents=True)
            real_scandir = os.scandir

            def blocked_scandir(path):
                if Path(path) == claude_root / "projects":
                    raise PermissionError("synthetic unreadable Claude projects")
                return real_scandir(path)

            with mock.patch("codex_vault.sources.os.scandir", side_effect=blocked_scandir):
                claude_groups = discover("claude", claude_root)
            self.assertEqual(len(claude_groups), 1)
            self.assertFalse(claude_groups[0].removable)
            self.assertTrue(any("projects" in note for note in claude_groups[0].notes))

            grok_root = base / "grok"
            (grok_root / "sessions").mkdir(parents=True)
            real_iterdir = Path.iterdir

            def blocked_grok_iterdir(path):
                if path == grok_root / "sessions":
                    raise PermissionError("synthetic unreadable Grok sessions")
                return real_iterdir(path)

            with mock.patch.object(Path, "iterdir", blocked_grok_iterdir):
                grok_groups = discover("grok", grok_root)
            self.assertEqual(len(grok_groups), 1)
            self.assertFalse(grok_groups[0].removable)
            self.assertTrue(any("sessions" in note for note in grok_groups[0].notes))

            gemini_root = base / "gemini"
            (gemini_root / "tmp").mkdir(parents=True)

            def blocked_gemini_iterdir(path):
                if path == gemini_root / "tmp":
                    raise PermissionError("synthetic unreadable Gemini tmp")
                return real_iterdir(path)

            with mock.patch.object(Path, "iterdir", blocked_gemini_iterdir):
                gemini_groups = discover("gemini", gemini_root)
            self.assertEqual(len(gemini_groups), 1)
            self.assertFalse(gemini_groups[0].removable)
            self.assertTrue(any("tmp" in note for note in gemini_groups[0].notes))

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

    def test_native_project_identifiers_allow_credential_like_path_terms_only(self):
        self.assertTrue(valid_relative("claude", "projects/-Users-fixture-my-account-app/sid.jsonl", "native"))
        self.assertTrue(valid_relative("claude", "projects/-Users-fixture-my-secret-app/sid.jsonl", "native"))
        self.assertTrue(valid_relative("grok", "sessions/%2Fwork%2Fmy-token-app/session-1/updates.jsonl", "native"))
        self.assertTrue(valid_relative("gemini", "tmp/my-secret-app/chats/session-1.json", "native"))

        self.assertFalse(valid_relative("claude", "projects/-Users-fixture-my-account-app/sid/auth.json", "native"))
        self.assertFalse(valid_relative("claude", "projects/-Users-fixture-my-token-app/sid/token-results/output.txt", "native"))
        self.assertFalse(valid_relative("grok", "sessions/%2Fwork%2Fmy-token-app/session-1/token.txt", "native"))
        self.assertFalse(valid_relative("gemini", "tmp/my-secret-app/chats/session-secret.json", "native"))

        self.assertFalse(valid_relative("claude", "exports/my-account-app/session.jsonl", "native"))
        self.assertFalse(valid_relative("claude", "projects/my-account-app", "native"))
        self.assertFalse(valid_relative("unknown", "projects/my-account-app/session.jsonl", "native"))
        self.assertFalse(valid_relative("claude", "projects/my-account-app/session.jsonl", "other"))

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

    def test_model_io_records_extract_known_text_without_dumping_sensitive_fields(self):
        lines = [
            {
                "type": "model_io",
                "sessionId": "diagnostic-session",
                "request": {
                    "messages": [
                        {"role": "user", "content": "사용자 요청"},
                        {"role": "assistant", "content": [{"type": "text", "text": "응답 준비"}]},
                        {"role": "assistant", "reasoning": "private reasoning", "headers": {"Authorization": "secret"}, "content": [
                            {"type": "reasoning", "text": "hidden reasoning block"},
                            {"type": "thinking", "text": "hidden thinking block"},
                            {"type": "analysis", "text": "hidden analysis block"},
                            {"type": "redacted-thinking", "text": "hidden redacted block"},
                            {"type": "text", "text": "visible next to private blocks"},
                            {"type": "input_image", "data": "binary-media"},
                        ]},
                    ],
                    "providerOptions": {"apiKey": "secret"},
                },
                "response": {
                    "text": "모델 응답",
                    "toolCalls": [
                        {"type": "thinking", "text": "private tool thinking"},
                        {"name": "read_file", "arguments": {"path": "src/한글.txt"}},
                    ],
                    "toolResults": [
                        {"type": "analysis", "text": "private tool analysis"},
                        {"name": "read_file", "result": {"output": "파일 결과"}},
                    ],
                    "reasoning": "private response reasoning",
                    "headers": {"Authorization": "secret"},
                },
                "error": {"message": "top-level error"},
            },
            {
                "type": "model_io",
                "request": {"sdkMessages": {"role": "user", "content": "SDK 메시지"}},
                "response": {"toolResults": [{"resultDisplay": "SDK 도구 결과"}]},
            },
            {
                "type": "model_io",
                "request": {"body": json.dumps({"messages": [{"role": "user", "content": "문자열 body"}]})},
                "response": {"text": "body 응답"},
            },
            {"type": "model_io", "request": {"body": {"messages": {"role": "user", "content": "dict body"}}}},
            {"type": "model_io", "error": {"message": "오류 메시지"}},
            {"type": "model_io", "body": {"messages": [{"role": "user", "content": "최상위 body는 무시"}]}},
            {"type": "modelIOReset", "sessionId": "reset-only"},
        ]
        data = b"\n".join(json.dumps(line, ensure_ascii=False).encode() for line in lines) + b"\n"

        records = list(iter_text_records(io.BytesIO(data), "model-io-jsonl"))
        texts = [record["text"] for record in records if "text" in record]

        for expected in ("사용자 요청", "응답 준비", "visible next to private blocks", "모델 응답", "read_file", "src/한글.txt", "파일 결과", "SDK 메시지", "SDK 도구 결과", "문자열 body", "body 응답", "dict body", "오류 메시지"):
            self.assertTrue(any(expected in text for text in texts), expected)
        self.assertNotIn("최상위 body는 무시", " ".join(texts))
        self.assertNotIn("private reasoning", " ".join(texts))
        self.assertNotIn("hidden reasoning block", " ".join(texts))
        self.assertNotIn("hidden thinking block", " ".join(texts))
        self.assertNotIn("hidden analysis block", " ".join(texts))
        self.assertNotIn("hidden redacted block", " ".join(texts))
        self.assertNotIn("private tool thinking", " ".join(texts))
        self.assertNotIn("private tool analysis", " ".join(texts))
        self.assertNotIn("secret", " ".join(texts))
        self.assertNotIn("binary-media", " ".join(texts))
        self.assertEqual(records[-1], {"line": 7, "skipped": True, "reason": "no readable text"})

    def test_model_io_delta_records_are_independent_and_unknown_shapes_are_skipped(self):
        lines = [
            {"type": "model_io", "request": {"messages": [{"content": "first delta"}]}},
            {"type": "model_io", "request": {"messages": [{"content": "second delta"}]}},
            {"type": "model_io_delta", "text": "must not be reconstructed"},
            {"type": "other", "request": {"messages": [{"content": "unknown"}]}},
        ]
        data = b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"

        records = list(iter_text_records(io.BytesIO(data), "model-io-jsonl"))
        texts = [record["text"] for record in records if "text" in record]

        self.assertEqual(texts, ["first delta", "second delta"])
        self.assertEqual(records[2]["reason"], "no readable text")
        self.assertEqual(records[3]["reason"], "no readable text")

    def test_model_io_combines_concurrent_request_views_and_tail_tool_results(self):
        line = {
            "type": "model_io",
            "request": {
                "messages": [{"content": "common"}, {"content": "message-only"}],
                "sdkMessages": [{"content": "common"}, {"content": "sdk-only"}],
                "body": {"messages": [{"content": "common"}, {"content": "body-only"}]},
            },
            "response": {
                "text": "response text",
                "toolCalls": [{"name": "read_file", "arguments": {"path": "common"}}],
                "toolResults": [],
            },
            "toolResults": [{"result": "tail-only tool result"}],
        }

        records = list(iter_text_records(io.BytesIO((json.dumps(line) + "\n").encode()), "model-io-jsonl"))
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["text"],
            "common message-only sdk-only body-only response text read_file tail-only tool result",
        )

    def test_model_io_metadata_reads_only_display_scalars(self):
        data = b"\n".join([
            json.dumps({"type": "model_io", "sessionId": "model-session", "startedAt": "2026-10-23T00:00:00Z", "request": {"messages": [{"content": "text"}]}}).encode(),
            json.dumps({"type": "model_io", "sessionId": "later", "startedAt": "later"}).encode(),
        ]) + b"\n"

        self.assertEqual(
            metadata_from_bytes(data, "model-io-jsonl"),
            {"session_id": "model-session", "started_at": "2026-10-23T00:00:00Z"},
        )

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

    def test_grok_wrapper_prefers_explicit_updates_over_metadata_fallbacks(self):
        lines = [
            {"update": {"sessionUpdate": {"kind": "user_message_chunk", "content": {"text": "direct update"}}}, "params": {"label": "ignored"}},
            {"params": {"update": {"sessionUpdate": {"kind": "user_message_chunk", "content": {"text": "params update"}}}}},
            {"params": {"label": "metadata"}, "data": {"update": {"sessionUpdate": {"kind": "user_message_chunk", "content": {"text": "data update"}}}}},
            {"params": {"sessionUpdate": {"kind": "user_message_chunk", "content": {"text": "params fallback"}}}},
            {"data": {"sessionUpdate": {"kind": "user_message_chunk", "content": {"text": "data fallback"}}}},
            {"params": {"label": "metadata"}, "data": {"update": {"sessionUpdate": {"kind": "agent_thought_chunk", "content": {"text": "private data update"}}}}},
        ]
        data = b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"

        records = list(iter_text_records(io.BytesIO(data), "grok-acp-jsonl"))
        texts = [record["text"] for record in records if "text" in record]

        self.assertEqual(texts, [
            "direct update",
            "params update",
            "data update",
            "params fallback",
            "data fallback",
        ])
        self.assertNotIn("metadata", texts)
        self.assertEqual(records[-1], {"line": 6, "skipped": True, "reason": "no readable text"})

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
