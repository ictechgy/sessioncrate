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
from unittest import mock


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
        human = subprocess.run(
            [sys.executable, str(ROOT / 'bin/sessioncrate'), '--vault', str(self.vault), 'list'],
            cwd=self.base, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(human.returncode, 2)
        self.assertIn(archive_id, human.stdout)
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
        self.assertIn('fully inspected', json.loads(prefix.stdout)['error'])
        self.run_cli('verify', archive_id[:16], '--tool', 'codex')

    def test_stats_combines_unfinished_temporary_storage_without_counting_archives(self):
        self.run_cli('archive', '--execute')
        paths = [
            self.vault / 'blobs/.vault-blob-unfinished',
            self.vault / 'manifests/.vault-tmp-unfinished',
            self.vault / 'bundles/blobs/.bundle-unfinished',
            self.vault / 'bundles/manifests/.bundle-manifest-unfinished',
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'incomplete')
        costs = json.loads(self.run_cli('stats').stdout)
        self.assertEqual(costs['archive_count'], 1)
        self.assertEqual(costs['temporary_files'], 4)
        self.assertEqual(costs['temporary_bytes'], 40)
        self.assertEqual(costs['temporary_allocated_bytes'], sum(path.stat().st_blocks * 512 for path in paths))
        self.assertEqual(costs['vault_bytes'], costs['blob_bytes'] + costs['manifest_bytes'] + 40)
        self.assertTrue(all(path.read_bytes() == b'incomplete' for path in paths))
        selected = json.loads(self.run_cli('stats', '--tool', 'grok').stdout)
        self.assertEqual(selected['temporary_files'], 0)

    def test_bundle_archive_partial_failure_exits_two_and_keeps_successful_archive(self):
        from codex_vault import bundles, cli, storage
        home = self.base / 'exports'
        home.mkdir()
        for name in ('healthy', 'failure'):
            (home / f'{name}.txt').write_text('synthetic session\n')
        snapshot = bundles._snapshot_bundle

        def fail_selected(root, group, blobs_dir):
            if group.session_id == 'failure':
                raise storage.VaultError('synthetic snapshot failure')
            return snapshot(root, group, blobs_dir)

        output = io.StringIO()
        with mock.patch.object(bundles, '_snapshot_bundle', side_effect=fail_selected), \
             contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), '--json', 'archive', '--tool', 'export',
                               '--source-root', str(home), '--older-than', '0', '--execute'])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual([item['session_id'] for item in result['archived']], ['healthy'])
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['skipped'], [])
        self.assertEqual(len(bundles.list_archives(self.vault)), 1)
        self.assertEqual(len(list(home.glob('*.txt'))), 2)

    def test_fatal_bundle_publication_reports_completed_work_and_preserves_originals(self):
        from codex_vault import bundles, cli, storage
        exports = self.base / 'partial publication'
        exports.mkdir()
        originals = {}
        old = time.time() - 60 * 86400
        for name in ('a-healthy', 'b-failure', 'c-later'):
            path = exports / f'{name}.txt'
            payload = f'synthetic {name}\n'.encode()
            path.write_bytes(payload)
            os.utime(path, (old, old))
            originals[path] = payload
        publish = bundles._write_manifest_atomic

        def fail_second(path, manifest):
            if manifest['session_id'] == 'b-failure':
                raise storage.VaultError('synthetic manifest publication failed')
            return publish(path, manifest)

        for json_output in (True, False):
            with self.subTest(json_output=json_output):
                vault = self.base / f'partial-vault-{json_output}'
                output, errors = io.StringIO(), io.StringIO()
                args = ['--vault', str(vault), '--tool', 'export', '--source-root', str(exports)]
                if json_output:
                    args.append('--json')
                with mock.patch.object(bundles, '_write_manifest_atomic', side_effect=fail_second), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    status = cli.main([*args, 'archive', '--older-than', '0', '--execute', '--remove-originals'])
                self.assertEqual(status, 2)
                if json_output:
                    result = json.loads(output.getvalue())
                    self.assertTrue(result['partial'])
                    self.assertFalse(result['ok'])
                    self.assertEqual([item['session_id'] for item in result['archived']], ['a-healthy'])
                    self.assertEqual(result['errors'][0]['session_id'], 'b-failure')
                    self.assertEqual(result['removed'], [])
                    self.assertIn('synthetic manifest publication failed', result['error'])
                else:
                    self.assertIn('Operation stopped', output.getvalue())
                    self.assertIn('Archived 1', output.getvalue())
                    self.assertIn('Errors: 1', output.getvalue())
                self.assertEqual([item['session_id'] for item in bundles.list_archives(vault)], ['a-healthy'])
                for path, payload in originals.items():
                    self.assertEqual(path.read_bytes(), payload)

    def test_codex_search_does_not_read_unselected_bundle_manifests(self):
        self.run_cli('archive', '--execute')
        manifests = self.vault / 'bundles/manifests'
        manifests.mkdir(parents=True)
        (self.vault / 'bundles/blobs').mkdir()
        (manifests / f'{"0" * 64}.json').write_text('{}')
        self.assertTrue(json.loads(self.run_cli('search', '한글', '--tool', 'codex').stdout)['matches'])

    def test_all_tool_search_keeps_healthy_hits_and_reports_namespace_failure(self):
        self.run_cli('archive', '--execute')
        (self.vault / 'bundles').write_text('broken namespace')
        result = self.run_cli('search', '한글', success=False)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload['matches']), 1)
        self.assertEqual(payload['matches'][0]['tool'], 'codex')
        self.assertTrue(payload['partial'])
        self.assertEqual(payload['errors'][0]['namespace'], 'bundles')
        human = subprocess.run(
            [sys.executable, str(ROOT / 'bin/sessioncrate'), '--vault', str(self.vault),
             'search', '한글'], cwd=self.base, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(human.returncode, 2)
        self.assertIn('한글', human.stdout)
        self.assertIn('Partial results', human.stdout)
        self.assertIn('bundles', human.stdout)

    def test_all_tool_search_interleaves_namespaces_before_limit(self):
        from codex_vault import bundles
        second = self.source.with_name('rollout-second.jsonl')
        second.write_bytes(self.raw)
        old = time.time() - 60 * 86400
        os.utime(second, (old, old))
        self.run_cli('archive', '--execute')
        exports = self.base / 'search exports'
        exports.mkdir()
        (exports / 'chat.txt').write_text('한글 bundle result', encoding='utf-8')
        bundles.archive('export', exports, self.vault, older_than_days=0, execute=True)
        result = json.loads(self.run_cli('search', '한글', '--limit', '2').stdout)
        self.assertEqual([hit['tool'] for hit in result['matches']], ['codex', 'export'])
        self.assertTrue(result['truncated'])
        self.assertFalse(result['partial'])

    def test_verify_missing_vault_is_an_error(self):
        result = self.run_cli('verify', success=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('does not exist', json.loads(result.stdout)['error'])

    def test_restore_expands_user_destination_for_both_backends(self):
        from codex_vault import bundles, cli, storage
        for backend in (storage, bundles):
            for execute in (False, True):
                with self.subTest(backend=backend.__name__, execute=execute):
                    args = cli.parser().parse_args([
                        'restore', 'a' * 64, '--destination-home', '~/sessioncrate-synthetic-restore',
                        *(['--execute'] if execute else []),
                    ])
                    # Stub only the write boundary. No real home directory is read or changed.
                    with mock.patch.object(cli, '_resolve_backend', return_value=(backend, {'archive_id': 'a' * 64})), \
                         mock.patch.object(backend, 'restore', return_value={'ok': True}) as restore:
                        cli._run(args, self.vault)
                    self.assertEqual(restore.call_args.kwargs['destination_home'],
                                     Path('~/sessioncrate-synthetic-restore').expanduser().absolute())
                    self.assertEqual(restore.call_args.kwargs['execute'], execute)

    def test_restore_unexpandable_home_is_a_safe_error(self):
        from codex_vault import cli, storage
        output = io.StringIO()
        real_expand = Path.expanduser

        def expand(path):
            if str(path).startswith('~missing-synthetic-user/'):
                raise RuntimeError('Could not determine home directory')
            return real_expand(path)

        with mock.patch.object(cli, '_resolve_backend', return_value=(storage, {'archive_id': 'a' * 64})), \
             mock.patch.object(Path, 'expanduser', expand), \
             mock.patch.object(storage, 'restore') as restore, contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), '--json', 'restore', 'a' * 64,
                               '--destination-home', '~missing-synthetic-user/restore'])
        self.assertEqual(status, 2)
        self.assertIn('expand', json.loads(output.getvalue())['error'])
        restore.assert_not_called()

    def test_unexpandable_source_and_vault_homes_are_safe_errors(self):
        from codex_vault import cli
        real_expand = Path.expanduser

        def expand(path):
            if str(path).startswith('~missing-synthetic-user/'):
                raise RuntimeError('Could not determine home directory')
            return real_expand(path)

        for option in ('--vault', '--source-root', '--codex-home'):
            with self.subTest(option=option):
                output = io.StringIO()
                with mock.patch.object(Path, 'expanduser', expand), contextlib.redirect_stdout(output):
                    status = cli.main(['--vault', str(self.vault), '--json', 'scan',
                                       option, '~missing-synthetic-user/directory'])
                self.assertEqual(status, 2)
                self.assertIn('expanded', json.loads(output.getvalue())['error'])

    def test_unexpandable_default_source_home_is_a_safe_error(self):
        from codex_vault import cli, sources
        output = io.StringIO()
        with mock.patch.object(sources, 'default_source_root', side_effect=RuntimeError('Could not determine home directory')), \
             contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), '--json', 'scan'])
        self.assertEqual(status, 2)
        self.assertIn('expanded', json.loads(output.getvalue())['error'])

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

    def test_human_preview_is_bounded_and_explains_eligibility(self):
        from codex_vault.cli import _print_human
        candidates = [{'relative_path': f'sessions/rollout-{index}.jsonl',
                       'size': 1024, 'eligible': True, 'reason': 'eligible'} for index in range(1000)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_human({'executed': False, 'candidates': candidates, 'archived': [],
                          'removed': [], 'skipped': [], 'errors': []}, command='archive')
        text = output.getvalue()
        self.assertIn('Preview', text)
        self.assertIn('1000 eligible', text)
        self.assertIn('980 more', text)
        self.assertIn('--json', text)
        self.assertLess(len(text.splitlines()), 40)

    def test_human_archive_reports_outcome_without_dumping_manifests(self):
        from codex_vault import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), '--source-root', str(self.home),
                               'archive', '--execute'])
        self.assertEqual(status, 0)
        text = output.getvalue()
        self.assertIn('Archived 1', text)
        self.assertIn('0 original(s) removed', text)
        self.assertNotIn('blob_sha256', text)
        self.assertNotIn('source_mtime_ns', text)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_human_bundle_restore_preview_shows_destination_and_file_count(self):
        from codex_vault import bundles, cli
        source = self.base / 'export'
        source.mkdir()
        (source / 'record.txt').write_text('synthetic session')
        item = bundles.archive('export', source, self.vault, older_than_days=0, execute=True)['archived'][0]
        destination = self.base / 'preview destination'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), 'restore', item['archive_id'],
                               '--destination-home', str(destination)])
        self.assertEqual(status, 0)
        self.assertIn('Preview', output.getvalue())
        self.assertIn('1 file(s)', output.getvalue())
        self.assertIn(str(destination), output.getvalue())
        self.assertFalse(destination.exists())

    def test_human_archive_preview_identifies_source_and_requested_removal(self):
        from codex_vault import cli
        source = self.base / 'explicit exports'
        source.mkdir()
        original = source / 'chat.txt'
        original.write_text('synthetic transcript')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), '--source-root', str(source),
                               '--tool', 'export', 'archive', '--older-than', '0', '--remove-originals'])
        self.assertEqual(status, 0)
        self.assertIn(str(source), output.getvalue())
        self.assertIn('Original removal requested', output.getvalue())
        self.assertEqual(original.read_text(), 'synthetic transcript')
        self.assertFalse(self.vault.exists())

    def test_human_stats_separates_storage_costs_and_partial_errors(self):
        from codex_vault.cli import _print_human
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_human({'archive_count': 1, 'vault_bytes': 2048, 'source_bytes_retained': 4096,
                          'temporary_files': 2, 'temporary_bytes': 1024, 'allocated_bytes': 8192,
                          'net_storage_bytes': 6144, 'partial': True,
                          'codex': {'archives': 1}, 'bundles': {},
                          'errors': [{'namespace': 'bundles', 'error': 'synthetic unreadable namespace'}]},
                         command='stats')
        text = output.getvalue()
        for marker in ('Vault storage', 'Retained originals', 'Temporary files', 'Partial results',
                       'synthetic unreadable namespace'):
            self.assertIn(marker, text)
        self.assertNotIn('{', text)

    def test_human_metadata_cannot_inject_status_lines(self):
        from codex_vault.cli import _print_human
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_human({'archives': [{'archive_id': 'a' * 64,
                                       'cwd': '/project\nFAKE SUCCESS\rhidden\tfield'}],
                          'total': 1, 'errors': []}, command='list')
        text = output.getvalue()
        self.assertNotIn('\nFAKE SUCCESS', text)
        self.assertNotIn('\r', text)
        self.assertIn(r'/project\nFAKE SUCCESS', text)

    def test_explicit_vault_does_not_require_resolvable_user_home(self):
        from codex_vault import cli
        output = io.StringIO()
        with mock.patch.object(Path, 'home', side_effect=RuntimeError('synthetic unknown home')), \
             contextlib.redirect_stdout(output):
            status = cli.main(['--vault', str(self.vault), '--json', 'list'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['archives'], [])

    def test_human_partial_archive_keeps_failure_and_preservation_reasons_visible(self):
        from codex_vault.cli import _print_human
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_human({'executed': True, 'archived': [], 'removed': [], 'candidates': [],
                          'errors': [{'session_id': 'failed-session', 'reason': 'synthetic disk full'}],
                          'skipped': [{'relative_path': 'sessions/rollout-busy.jsonl', 'reason': 'session file is open'}]},
                         command='archive')
        text = output.getvalue()
        self.assertIn('Errors: 1', text)
        self.assertIn('failed-session: synthetic disk full', text)
        self.assertIn('Originals preserved: 1', text)
        self.assertIn('session file is open', text)

    def test_human_prune_shows_retained_quarantine_recovery_path(self):
        from codex_vault.cli import _print_human
        output = io.StringIO()
        quarantine = '/synthetic/project/.pending-recovery\nforged status'
        with contextlib.redirect_stdout(output):
            _print_human({'executed': True, 'removed': [], 'removed_source_bytes': 0,
                          'candidates': [], 'skipped': [{'archive_id': 'a' * 64,
                          'reason': 'source path reappeared', 'quarantine': quarantine}]}, command='prune')
        text = output.getvalue()
        self.assertIn('Recovery quarantine:', text)
        self.assertIn(r'/synthetic/project/.pending-recovery\nforged status', text)
        self.assertNotIn('\nforged status', text)

    def test_direct_human_renderer_preserves_execution_state_and_backend_shapes(self):
        from codex_vault.cli import _print_human
        examples = [
            ({'executed': True, 'candidates': [], 'archived': [], 'removed': []}, 'Archived 0'),
            ({'executed': True, 'candidates': [], 'removed': []}, 'Removed 0'),
            ({'executed': True, 'restored': True, 'skipped': False, 'destination': '/synthetic'}, '1 restored'),
            ({'executed': True, 'restored': ['one'], 'skipped': ['two']}, '1 restored, 1 unchanged'),
            ({'archives': 0, 'archive_count': 0}, 'Vault storage'),
            ({'ok': True, 'archive_id': 'a' * 64}, 'Verified:'),
        ]
        for result, marker in examples:
            with self.subTest(marker=marker):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    _print_human(result)
                self.assertIn(marker, output.getvalue())
                self.assertNotIn('Preview', output.getvalue())

    def test_missing_default_home_returns_safe_error(self):
        from codex_vault import cli
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': ''}), \
             mock.patch.object(Path, 'home', side_effect=RuntimeError('synthetic unknown home')), \
             contextlib.redirect_stdout(output):
            status = cli.main(['--json', 'list'])
        self.assertEqual(status, 2)
        self.assertIn('vault home', json.loads(output.getvalue())['error'])

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
