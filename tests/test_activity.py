from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from codex_vault import activity
from codex_vault.activity import probe_session_file


class ActivityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="codex-vault activity ")
        self.addCleanup(self._temporary.cleanup)
        self.target = Path(self._temporary.name) / "session with spaces.jsonl"
        self.target.write_text("synthetic session data\n", encoding="utf-8")

    def _completed(self, returncode: int, *, stdout: str = "", stderr: str = "") -> mock.Mock:
        result = mock.Mock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    def test_clean_exit_one_with_no_output_is_inactive(self) -> None:
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            with mock.patch(
                "codex_vault.activity.subprocess.run",
                return_value=self._completed(1),
            ) as run:
                result = probe_session_file(self.target)

        self.assertEqual(result["state"], "inactive")
        self.assertIsInstance(result["reason"], str)
        run.assert_called_once_with(
            [
                "/usr/sbin/lsof",
                "-nP",
                "-Fpf",
                "--",
                str(self.target.absolute()),
            ],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=5,
        )

    def test_process_record_means_active(self) -> None:
        completed = self._completed(0, stdout="p1234\nf4\n")
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            with mock.patch("codex_vault.activity.subprocess.run", return_value=completed):
                result = probe_session_file(self.target)
        self.assertEqual(result["state"], "active")

    def test_timeout_is_unknown_and_does_not_fall_back(self) -> None:
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            with mock.patch(
                "codex_vault.activity.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=5),
            ):
                result = probe_session_file(self.target)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("timed out", result["reason"].lower())

    def test_parser_or_subprocess_syntax_error_is_unknown(self) -> None:
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            with mock.patch("codex_vault.activity.subprocess.run", side_effect=SyntaxError("bad lsof")):
                result = probe_session_file(self.target)
        self.assertEqual(result["state"], "unknown")

    def test_missing_tool_is_unknown(self) -> None:
        with mock.patch("codex_vault.activity.shutil.which", return_value=None):
            result = probe_session_file(self.target)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("lsof", result["reason"].lower())

    def test_warning_or_ambiguous_output_is_unknown(self) -> None:
        cases = [
            self._completed(0, stdout="p1234\nf4\n", stderr="lsof: warning: partial result\n"),
            self._completed(0, stdout="warning: parser output\n"),
            self._completed(1, stderr="permission denied\n"),
            self._completed(2, stdout="p1234\nf4\n"),
        ]
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            for completed in cases:
                with self.subTest(completed=completed):
                    with mock.patch("codex_vault.activity.subprocess.run", return_value=completed):
                        result = probe_session_file(self.target)
                    self.assertEqual(result["state"], "unknown")

    def test_malformed_success_output_is_unknown(self) -> None:
        completed = self._completed(0, stdout="f4\n")
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            with mock.patch("codex_vault.activity.subprocess.run", return_value=completed):
                result = probe_session_file(self.target)
        self.assertEqual(result["state"], "unknown")

    def test_paths_with_spaces_are_passed_as_one_absolute_argument_without_shell(self) -> None:
        completed = self._completed(1)
        with mock.patch("codex_vault.activity.shutil.which", return_value="/usr/sbin/lsof"):
            with mock.patch(
                "codex_vault.activity.subprocess.run",
                return_value=completed,
            ) as run:
                probe_session_file(Path(self._temporary.name) / "session with spaces.jsonl")
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], str(self.target.absolute()))
        self.assertIsInstance(args, tuple)
        self.assertFalse(kwargs.get("shell", True))

    def test_symlink_and_missing_file_are_unknown(self) -> None:
        symlink = Path(self._temporary.name) / "session-link.jsonl"
        symlink.symlink_to(self.target)
        with mock.patch("codex_vault.activity.shutil.which") as which:
            self.assertEqual(probe_session_file(symlink)["state"], "unknown")
            self.assertEqual(probe_session_file(self.target.with_name("missing.jsonl"))["state"], "unknown")
            which.assert_not_called()

    @unittest.skipUnless(shutil.which("lsof"), "lsof is unavailable")
    def test_real_lsof_smoke_free_then_open_file(self) -> None:
        free = probe_session_file(self.target)
        self.assertEqual(free["state"], "inactive")
        with self.target.open("rb") as stream:
            active = probe_session_file(self.target)
            pending = self.target.with_name(f".codex-vault-pending-{uuid.uuid4().hex}")
            self.target.rename(pending)
            self.addCleanup(lambda: pending.unlink(missing_ok=True))
            renamed_active = probe_session_file(pending)
        self.assertEqual(active["state"], "active")
        self.assertEqual(renamed_active["state"], "active")

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is unavailable')
    def test_selected_tree_detects_open_companion_and_renamed_tree(self) -> None:
        tree = self.target.parent / 'session companions'
        tree.mkdir()
        child = tree / 'tool-output.txt'
        child.write_text('synthetic tool output')
        self.assertEqual(activity.probe_session_tree(tree)['state'], 'inactive')
        with child.open('rb'):
            self.assertEqual(activity.probe_session_tree(tree)['state'], 'active')
            pending = tree.with_name('.vault-pending-tree')
            tree.rename(pending)
            self.assertEqual(activity.probe_session_tree(pending)['state'], 'active')
        directory_fd = os.open(pending, os.O_RDONLY)
        try:
            self.assertEqual(activity.probe_session_tree(pending)['state'], 'active')
        finally:
            os.close(directory_fd)

    def test_tree_probe_fails_closed_on_warning_or_symlink(self) -> None:
        tree = self.target.parent / 'session'
        tree.mkdir()
        linked = tree.with_name('linked-session')
        linked.symlink_to(tree, target_is_directory=True)
        self.assertEqual(activity.probe_session_tree(linked)['state'], 'unknown')
        with mock.patch('codex_vault.activity.shutil.which', return_value='/usr/sbin/lsof'):
            with mock.patch('codex_vault.activity.subprocess.run',
                            return_value=self._completed(1, stderr='partial tree inspection')):
                self.assertEqual(activity.probe_session_tree(tree)['state'], 'unknown')


if __name__ == "__main__":
    unittest.main()
