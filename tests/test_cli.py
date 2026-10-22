import json
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def fixture(home):
    path = home / 'sessions/2025/01/02/rollout-example.jsonl'
    path.parent.mkdir(parents=True)
    records = [
        {'timestamp': '2025-01-02T00:00:00Z', 'type': 'session_meta',
         'payload': {'id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                     'timestamp': '2025-01-02T00:00:00Z',
                     'cwd': '/projects/example', 'cli_version': '0.1.0'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': '한글 아카이빙 검색 테스트 payment.retry()'}]}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output',
         'call_id': 'test', 'output': 'repeatable build output\n' * 8000}},
    ]
    raw = b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode() for r in records)
    path.write_bytes(raw)
    old = time.time() - 60 * 86400
    os.utime(path, (old, old))
    return path, raw


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vault CLI space ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'codex home'
        self.vault = self.base / 'private vault'
        self.source, self.raw = fixture(self.home)

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'bin/codex-vault'),
             '--codex-home', str(self.home), '--vault', str(self.vault), '--json', *args],
            cwd=self.base, capture_output=True, text=True, timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def test_help_runs_outside_checkout(self):
        result = self.run_cli('--help')
        self.assertIn('archive', result.stdout)
        self.assertIn('search', result.stdout)

    def test_default_archive_is_read_only_preview(self):
        result = self.run_cli('archive')
        payload = json.loads(result.stdout)
        self.assertFalse(payload.get('executed', False))
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertFalse(self.vault.exists())

    def test_archive_search_verify_and_restore_roundtrip(self):
        self.run_cli('archive', '--execute')
        self.assertEqual(self.source.read_bytes(), self.raw)
        archives = json.loads(self.run_cli('list').stdout)['archives']
        self.assertEqual(len(archives), 1)
        archive_id = archives[0]['archive_id']
        self.source.unlink()  # Synthetic fixture only: prove archive independence.
        hits = json.loads(self.run_cli('search', '한글 아카이빙').stdout)['matches']
        self.assertTrue(hits)
        self.assertEqual(hits[0]['archive_id'], archive_id)
        self.run_cli('verify', archive_id)
        destination = self.base / 'restored home'
        self.run_cli('restore', archive_id, '--destination-home', str(destination), '--execute')
        restored = destination / self.source.relative_to(self.home)
        self.assertEqual(restored.read_bytes(), self.raw)
        self.assertFalse(self.source.exists())

    def test_unknown_archive_is_actionable_error_without_traceback(self):
        result = self.run_cli('restore', 'does-not-exist', success=False)
        self.assertEqual(result.returncode, 2)
        output = result.stdout + result.stderr
        self.assertNotIn('Traceback', output)
        self.assertIn('error', output.lower())

    def test_options_work_after_subcommand(self):
        result = self.run_cli('scan', '--codex-home', str(self.home), '--json')
        self.assertTrue(json.loads(result.stdout)['candidates'])

    def test_verify_corruption_exits_nonzero(self):
        self.run_cli('archive', '--execute')
        blob = next((self.vault / 'blobs').glob('*.jsonl.gz'))
        blob.write_bytes(blob.read_bytes()[:-8])
        result = self.run_cli('verify', success=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)['ok'])

    def test_unrelated_corrupt_manifests_do_not_block_exact_or_prefix_recovery(self):
        self.run_cli('archive', '--execute')
        archive_id = json.loads(self.run_cli('list').stdout)['archives'][0]['archive_id']
        bad_id = ('f' if archive_id[0] != 'f' else 'e') * 64
        for directory in (self.vault / 'manifests', self.vault / 'bundles/manifests'):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f'{bad_id}.json').write_text('{}')
        (self.vault / 'bundles/blobs').mkdir()
        for identifier in (archive_id, archive_id[:16]):
            with self.subTest(identifier=identifier):
                destination = self.base / f'restored-{len(identifier)}'
                self.run_cli('restore', identifier, '--destination-home', str(destination), '--execute')
                self.assertEqual((destination / self.source.relative_to(self.home)).read_bytes(), self.raw)

    def test_list_keeps_valid_archives_and_identifies_damaged_manifests(self):
        self.run_cli('archive', '--execute')
        bad_id = '0' * 64
        (self.vault / 'manifests' / f'{bad_id}.json').write_text('{}')
        result = self.run_cli('list', success=False)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload['archives']), 1)
        self.assertEqual(payload['errors'][0]['archive_id'], bad_id)
        self.assertIn(f'{bad_id}.json', payload['errors'][0]['manifest'])

    def test_corrupt_matching_prefix_is_not_silently_resolved_to_good_archive(self):
        self.run_cli('archive', '--execute')
        archive_id = json.loads(self.run_cli('list').stdout)['archives'][0]['archive_id']
        bad_id = archive_id[:8] + ('0' if archive_id[8] != '0' else '1') + archive_id[9:]
        (self.vault / 'manifests' / f'{bad_id}.json').write_text('{}')
        result = self.run_cli('restore', archive_id[:8], success=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('full', json.loads(result.stdout)['error'])
        self.run_cli('verify', archive_id)

    def test_verify_reports_each_corrupt_manifest_and_still_verifies_intact_data(self):
        self.run_cli('archive', '--execute')
        bad_id = '0' * 64
        for directory in (self.vault / 'manifests', self.vault / 'bundles/manifests'):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f'{bad_id}.json').write_text('{}')
        (self.vault / 'bundles/blobs').mkdir()
        result = self.run_cli('verify', success=False)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['count'], 3)
        self.assertEqual(len(payload['verified']), 1)
        self.assertEqual(len(payload['failed']), 2)
        self.assertTrue(all(item['archive_id'] == bad_id for item in payload['failed']))

    def test_stats_preserves_top_level_storage_cost_fields(self):
        self.run_cli('archive', '--execute')
        result = json.loads(self.run_cli('stats').stdout)
        self.assertEqual(result['source_bytes_retained'], len(self.raw))
        self.assertEqual(result['net_storage_bytes'], result['vault_bytes'] + len(self.raw))
        self.assertEqual(result['raw_archived_bytes'], len(self.raw))

    def test_verify_keeps_completed_checks_when_other_namespace_is_unreadable(self):
        self.run_cli('archive', '--execute')
        (self.vault / 'bundles').write_text('not a namespace directory')
        result = self.run_cli('verify', success=False)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload['ok'])
        self.assertEqual(len(payload['verified']), 1)
        self.assertEqual(len(payload['failed']), 1)
        self.assertEqual(payload['failed'][0]['namespace'], 'bundles')

    def test_broken_bundle_namespace_does_not_disable_intact_codex_recovery(self):
        self.run_cli('archive', '--execute')
        archive_id = json.loads(self.run_cli('list').stdout)['archives'][0]['archive_id']
        (self.vault / 'bundles').write_text('broken namespace')
        listing = self.run_cli('list', success=False)
        self.assertEqual(listing.returncode, 2)
        self.assertEqual(len(json.loads(listing.stdout)['archives']), 1)
        costs = self.run_cli('stats', success=False)
        self.assertEqual(costs.returncode, 2)
        payload = json.loads(costs.stdout)
        self.assertEqual(payload['archive_count'], 1)
        self.assertTrue(payload['partial'])
        self.assertTrue(payload['errors'])
        self.assertTrue(json.loads(self.run_cli('search', '한글', '--tool', 'codex').stdout)['matches'])
        destination = self.base / 'namespace-damaged-recovery'
        self.run_cli('restore', archive_id, '--destination-home', str(destination), '--execute')
        self.assertEqual((destination / self.source.relative_to(self.home)).read_bytes(), self.raw)
        prefix = self.run_cli('restore', archive_id[:16], success=False)
        self.assertEqual(prefix.returncode, 2)
        self.run_cli('verify', archive_id[:16], '--tool', 'codex')

    def test_codex_search_does_not_read_unselected_bundle_manifests(self):
        self.run_cli('archive', '--execute')
        manifests = self.vault / 'bundles/manifests'
        manifests.mkdir(parents=True)
        (self.vault / 'bundles/blobs').mkdir()
        (manifests / f'{"0" * 64}.json').write_text('{}')
        self.assertTrue(json.loads(self.run_cli('search', '한글', '--tool', 'codex').stdout)['matches'])

    def test_verify_missing_vault_is_an_error(self):
        result = self.run_cli('verify', success=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('does not exist', json.loads(result.stdout)['error'])

    def test_human_bundle_scan_displays_path_and_source_bytes(self):
        from codex_vault.cli import _print_human
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_human({'candidates': [{'root_entries': ['projects/work/session.jsonl'],
                                         'source_bytes': 4096, 'reason': 'eligible'}]})
        self.assertIn('projects/work/session.jsonl', output.getvalue())
        self.assertIn('4.0 KiB', output.getvalue())

    def test_human_search_identifies_codex_source_file(self):
        self.run_cli('archive', '--execute')
        result = subprocess.run(
            [sys.executable, str(ROOT / 'bin/sessioncrate'), '--vault', str(self.vault),
             'search', 'payment.retry()'],
            cwd=self.base, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.source.relative_to(self.home).as_posix(), result.stdout)
        self.assertIn('line 2', result.stdout)

    def test_human_search_and_show_identify_bundle_members(self):
        from codex_vault import bundles
        home = self.base / 'grok fixture'
        group = home / 'sessions/project/session'
        group.mkdir(parents=True)
        paths = [group / 'first.txt', group / 'second.txt']
        for path in paths:
            path.write_text('shared search marker\n')
        bundles.archive('grok', home, self.vault, older_than_days=0, execute=True)
        archive_id = bundles.list_archives(self.vault)[0]['archive_id']
        for args in [('search', 'shared search marker'), ('show', archive_id)]:
            with self.subTest(command=args[0]):
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'bin/sessioncrate'), '--vault', str(self.vault), *args],
                    cwd=self.base, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for path in paths:
                    self.assertIn(path.relative_to(home).as_posix(), result.stdout)

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is unavailable')
    def test_prune_cli_skips_open_source_then_reclaims_after_it_closes(self):
        self.run_cli('archive', '--execute')
        with self.source.open('rb'):
            busy = json.loads(self.run_cli('prune', '--execute').stdout)
        self.assertEqual(busy['removed'], [])
        self.assertEqual(busy['skipped'][0]['activity'], 'active')
        self.assertEqual(self.source.read_bytes(), self.raw)
        idle = json.loads(self.run_cli('prune', '--execute').stdout)
        self.assertEqual(len(idle['removed']), 1)
        self.assertFalse(self.source.exists())
        self.run_cli('verify')


if __name__ == '__main__':
    unittest.main()
