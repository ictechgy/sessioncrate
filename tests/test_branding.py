import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sessioncrate branding ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_new_launcher_works_outside_checkout_and_uses_sessioncrate_branding(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'bin/sessioncrate'), '--version'],
            cwd=self.base, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'sessioncrate 0.1.0')

        help_result = subprocess.run(
            [sys.executable, str(ROOT / 'bin/sessioncrate'), '--help'],
            cwd=self.base, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('usage: sessioncrate', help_result.stdout)

    def test_default_vault_directory_remains_codex_vault(self):
        source = self.base / 'codex-home/sessions/2025/01/01/rollout-branding.jsonl'
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': 'branding', 'cwd': '/synthetic/project'}}) + '\n')
        old = time.time() - 60 * 86400
        os.utime(source, (old, old))
        data_home = self.base / 'xdg-data'
        environment = {**os.environ, 'XDG_DATA_HOME': str(data_home)}
        result = subprocess.run(
            [sys.executable, str(ROOT / 'bin/sessioncrate'), '--codex-home',
             str(source.parents[4]), 'archive', '--execute'],
            cwd=self.base, env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((data_home / 'codex-vault').is_dir())
        self.assertFalse((data_home / 'sessioncrate').exists())


if __name__ == '__main__':
    unittest.main()
