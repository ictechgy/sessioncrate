import json
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vault install space ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bin = self.base / 'bin'
        self.skills = self.base / 'skills'

    def run_install(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / 'Scripts/install.py'),
             '--bin-dir', str(self.bin), '--skills-dir', str(self.skills), *args],
            cwd=self.base, capture_output=True, text=True, timeout=30,
        )

    def test_preview_creates_nothing(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.skills.exists())

    def test_home_lookup_is_lazy_and_missing_defaults_return_safe_errors(self):
        spec = importlib.util.spec_from_file_location('vault_installer_home', ROOT / 'Scripts/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                output, errors = io.StringIO(), io.StringIO()
                args = ['--bin-dir', str(self.bin), '--skills-dir', str(self.skills)] if explicit else []
                with mock.patch.object(Path, 'home', side_effect=RuntimeError('synthetic private diagnostic')), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    status = installer.main(args)
                self.assertEqual(status, 0 if explicit else 2)
                if explicit:
                    self.assertFalse(json.loads(output.getvalue())['executed'])
                else:
                    self.assertIn('home', errors.getvalue().lower())
                    self.assertNotIn('synthetic private diagnostic', errors.getvalue())
                self.assertFalse(self.bin.exists())
                self.assertFalse(self.skills.exists())

    def test_install_launchers_work_and_reinstall_is_idempotent(self):
        for _ in range(2):
            result = self.run_install('--execute')
            self.assertEqual(result.returncode, 0, result.stderr)
        launcher = self.bin / 'sessioncrate'
        skill = self.skills / 'sessioncrate'
        self.assertTrue(launcher.is_symlink())
        self.assertTrue((skill / 'scripts/sessioncrate').is_file())
        self.assertFalse((self.bin / 'codex-vault').exists())
        self.assertFalse((self.skills / 'codex-vault').exists())
        for command in [launcher, skill / 'scripts/sessioncrate']:
            result = subprocess.run([sys.executable, str(command), '--version'],
                                    cwd=self.base, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('sessioncrate 0.1.0', result.stdout)

    def test_existing_unrelated_file_is_preserved_and_other_target_not_installed(self):
        self.bin.mkdir()
        unrelated = self.bin / 'sessioncrate'
        unrelated.write_text('user-owned launcher')
        result = self.run_install('--execute')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(unrelated.read_text(), 'user-owned launcher')
        self.assertFalse(self.skills.exists())

    def test_existing_legacy_alias_is_preserved_by_default(self):
        self.bin.mkdir()
        legacy = self.bin / 'codex-vault'
        legacy.write_text('user-owned legacy launcher')
        result = self.run_install('--execute')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(legacy.read_text(), 'user-owned legacy launcher')
        self.assertTrue((self.bin / 'sessioncrate').is_symlink())

    def test_legacy_aliases_are_opt_in_and_conflicts_are_atomic(self):
        self.bin.mkdir()
        legacy = self.bin / 'codex-vault'
        legacy.write_text('user-owned legacy launcher')
        result = self.run_install('--execute', '--legacy-aliases')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(legacy.read_text(), 'user-owned legacy launcher')
        self.assertFalse((self.bin / 'sessioncrate').exists())
        self.assertFalse(self.skills.exists())

    def test_symlink_parent_is_rejected(self):
        other = self.base / 'other'
        other.mkdir()
        self.skills.symlink_to(other, target_is_directory=True)
        result = self.run_install('--execute')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(other.iterdir()), [])

    def test_validation_failure_rolls_back_new_launchers(self):
        spec = importlib.util.spec_from_file_location('vault_installer', ROOT / 'Scripts/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original = installer._target

        def changed_parent(path):
            if path.parent == self.skills and (self.bin / 'sessioncrate').is_symlink():
                raise ValueError('Installation parent changed during publication')
            return original(path)

        with mock.patch.object(installer, '_target', side_effect=changed_parent):
            with self.assertRaises(ValueError):
                installer.install(self.bin, self.skills, execute=True)
        self.assertFalse((self.bin / 'sessioncrate').is_symlink())
        self.assertFalse((self.skills / 'sessioncrate').is_symlink())

    def test_rollback_preserves_replaced_launcher_and_reports_conflict(self):
        spec = importlib.util.spec_from_file_location('vault_installer', ROOT / 'Scripts/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original = installer._target
        foreign = '/tmp/foreign-sessioncrate-launcher'

        def replaced_before_second_validation(path):
            if path.parent == self.skills and (self.bin / 'sessioncrate').is_symlink():
                (self.bin / 'sessioncrate').unlink()
                (self.bin / 'sessioncrate').symlink_to(foreign)
                raise ValueError('Installation parent changed during publication')
            return original(path)

        with mock.patch.object(installer, '_target', side_effect=replaced_before_second_validation):
            with self.assertRaisesRegex(ValueError, 'rollback conflict'):
                installer.install(self.bin, self.skills, execute=True)
        self.assertTrue((self.bin / 'sessioncrate').is_symlink())
        self.assertEqual((self.bin / 'sessioncrate').readlink(), Path(foreign))
        self.assertFalse((self.skills / 'sessioncrate').exists())


if __name__ == '__main__':
    unittest.main()
