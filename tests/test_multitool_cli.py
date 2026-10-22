import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def age_tree(root):
    old = time.time() - 60 * 86400
    for path in sorted(root.rglob('*'), key=lambda path: len(path.parts), reverse=True):
        os.utime(path, (old, old))
    os.utime(root, (old, old))


class MultiToolCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='multi AI vault space ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vault = self.base / 'vault'

    def run_cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(ROOT / 'bin/codex-vault'),
                                 '--vault', str(self.vault), '--json', *args],
                                cwd=self.base, capture_output=True, text=True, timeout=45)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def claude_fixture(self):
        home = self.base / 'claude source'
        main = home / 'projects/-work/session-one.jsonl'
        main.parent.mkdir(parents=True)
        main.write_text(json.dumps({'type': 'user', 'sessionId': 'session-one', 'cwd': '/work',
                                  'message': {'role': 'user', 'content': 'Claude 공통 검색'}}) + '\n')
        sub = main.parent / 'session-one/subagents/agent-a.jsonl'
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': '하위 에이전트 검색'}]}}) + '\n')
        result = main.parent / 'session-one/tool-results/result.txt'
        result.parent.mkdir()
        result.write_text('도구 결과 검색\n')
        age_tree(home)
        return home, {path.relative_to(home): path.read_bytes() for path in home.rglob('*') if path.is_file()}

    def test_tools_reports_native_and_export_capabilities(self):
        result = json.loads(self.run_cli('tools').stdout)
        by_id = {tool['id']: tool for tool in result['tools']}
        for tool in ('codex', 'claude', 'grok', 'gemini', 'agy', 'glm', 'opencode'):
            self.assertIn(tool, by_id)
        self.assertEqual(by_id['agy']['mode'], 'export')
        self.assertIsNone(by_id['agy']['default_root'])

    def test_diagnostic_tools_require_explicit_root(self):
        catalog = {item['id']: item for item in json.loads(self.run_cli('tools').stdout)['tools']}
        for tool in ('zcode', 'autoclaw'):
            with self.subTest(tool=tool):
                self.assertEqual(catalog[tool]['mode'], 'diagnostic')
                self.assertIsNone(catalog[tool]['default_root'])
                result = self.run_cli('scan', '--tool', tool, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--source-root', json.loads(result.stdout)['error'])

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is unavailable')
    def test_diagnostic_log_roundtrip_preserves_unselected_database_and_active_log(self):
        for tool in ('zcode', 'autoclaw'):
            with self.subTest(tool=tool):
                root = self.base / (tool + ' diagnostic logs')
                root.mkdir()
                source = root / 'model-io-sess-fixture.jsonl'
                raw = (json.dumps({'type': 'model_io', 'sessionId': 'synthetic/main',
                                  'request': {'messages': [{'role': 'user', 'content': '진단 질문'}],
                                              'headers': {'authorization': 'synthetic-hidden-header'}},
                                  'response': {'text': '진단 응답'}}, ensure_ascii=False) + '\n').encode()
                source.write_bytes(raw)
                unrelated = root / 'session.db'
                unrelated.write_bytes(b'synthetic shared database sentinel')
                age_tree(root)
                original_stat = source.stat()
                archived = json.loads(self.run_cli('archive', '--tool', tool, '--source-root', str(root), '--execute').stdout)['archived']
                self.assertEqual(len(archived), 1)
                manifest = archived[0]
                self.assertEqual(manifest['source_mode'], 'diagnostic')
                self.assertEqual([item['path'] for item in manifest['files']], [source.name])
                self.assertEqual(manifest['directories'], [])
                with source.open('rb'):
                    busy = json.loads(self.run_cli('prune', manifest['archive_id'], '--execute').stdout)
                self.assertFalse(busy['removed'])
                self.assertTrue(source.exists())
                idle = json.loads(self.run_cli('prune', manifest['archive_id'], '--execute').stdout)
                self.assertEqual(idle['removed'], [manifest['archive_id']])
                self.assertFalse(source.exists())
                for query in ('진단 질문', '진단 응답'):
                    self.assertTrue(json.loads(self.run_cli('search', query, '--tool', tool).stdout)['matches'])
                self.assertFalse(json.loads(self.run_cli('search', 'synthetic-hidden-header', '--tool', tool).stdout)['matches'])
                self.run_cli('verify', manifest['archive_id'])
                self.run_cli('restore', manifest['archive_id'], '--execute')
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual(source.stat().st_mtime_ns, original_stat.st_mtime_ns)
                self.assertEqual(unrelated.read_bytes(), b'synthetic shared database sentinel')

    def test_claude_bundle_search_and_restore_includes_companions(self):
        home, originals = self.claude_fixture()
        self.run_cli('--tool', 'claude', '--source-root', str(home), 'archive', '--execute')
        listing = json.loads(self.run_cli('list').stdout)['archives']
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['tool'], 'claude')
        archive_id = listing[0]['archive_id']
        hit = json.loads(self.run_cli('search', '하위 에이전트').stdout)['matches'][0]
        self.assertEqual(hit['tool'], 'claude')
        self.run_cli('verify', archive_id[:16])
        destination = self.base / 'restored claude'
        self.run_cli('restore', archive_id[:16], '--destination-home', str(destination), '--execute')
        for relative, original in originals.items():
            self.assertEqual((destination / relative).read_bytes(), original)

    def test_all_tool_search_keeps_bundle_hits_when_codex_is_damaged(self):
        home, _ = self.claude_fixture()
        self.run_cli('archive', '--tool', 'claude', '--source-root', str(home), '--execute')
        manifests = self.vault / 'manifests'
        manifests.mkdir()
        (manifests / ('0' * 64 + '.json')).write_text('{}')
        result = self.run_cli('search', '하위 에이전트', success=False)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload['matches']), 1)
        self.assertEqual(payload['matches'][0]['tool'], 'claude')
        self.assertTrue(payload['partial'])
        self.assertEqual(payload['errors'][0]['namespace'], 'codex')

    def test_bundle_recovery_ignores_unrelated_damaged_bundle_manifest(self):
        home, originals = self.claude_fixture()
        self.run_cli('--tool', 'claude', '--source-root', str(home), 'archive', '--execute')
        archive_id = json.loads(self.run_cli('list').stdout)['archives'][0]['archive_id']
        bad_id = ('0' if archive_id[0] != '0' else '1') * 64
        (self.vault / 'bundles/manifests' / f'{bad_id}.json').write_text('{}')
        for identifier in (archive_id, archive_id[:16]):
            destination = self.base / f'recovered-bundle-{len(identifier)}'
            self.run_cli('restore', identifier, '--destination-home', str(destination), '--execute')
            for relative, original in originals.items():
                self.assertEqual((destination / relative).read_bytes(), original)

    def test_stats_includes_retained_bundle_sources_once(self):
        home, originals = self.claude_fixture()
        self.run_cli('--tool', 'claude', '--source-root', str(home), 'archive', '--execute')
        self.run_cli('--tool', 'claude', '--source-root', str(home), 'archive', '--execute')
        costs = json.loads(self.run_cli('stats').stdout)
        retained = sum(len(raw) for raw in originals.values())
        self.assertEqual(costs['source_bytes_retained'], retained)
        self.assertEqual(costs['net_storage_bytes'], costs['vault_bytes'] + retained)

    def test_stats_tool_scope_counts_only_selected_archives_and_unique_blobs(self):
        from tests.helpers import make_codex_home, make_session
        home = make_codex_home(self.base)
        codex = make_session(home, age_seconds=60 * 86400)
        self.run_cli('archive', '--source-root', str(home), '--execute')
        sources = []
        for label, tool, text in [('one', 'export', 'shared export bytes\n'),
                                  ('two', 'export', 'shared export bytes\n'),
                                  ('three', 'glm', 'different GLM export\n')]:
            root = self.base / label
            root.mkdir()
            path = root / 'session.txt'
            path.write_text(text)
            stamp = 1_700_000_000_000_000_000
            os.utime(path, ns=(stamp, stamp))
            self.run_cli('archive', '--tool', tool, '--source-root', str(root), '--execute')
            sources.append(path)
        archives = json.loads(self.run_cli('list').stdout)['archives']
        orphan = self.vault / 'bundles/blobs' / ('f' * 64 + '.tar.gz')
        orphan.write_bytes(b'unreferenced synthetic blob')
        self.assertEqual(json.loads(self.run_cli('stats').stdout)['archive_count'], 4)
        for tool, count, retained in [('codex', 1, codex.stat().st_size),
                                     ('export', 2, sum(path.stat().st_size for path in sources[:2])),
                                     ('glm', 1, sources[2].stat().st_size)]:
            with self.subTest(tool=tool):
                selected = [item for item in archives if item['tool'] == tool]
                costs = json.loads(self.run_cli('stats', '--tool', tool).stdout)
                manifest_dir = self.vault / ('manifests' if tool == 'codex' else 'bundles/manifests')
                manifest_bytes = sum((manifest_dir / (item['archive_id'] + '.json')).stat().st_size
                                     for item in selected)
                blobs = {item['blob'] for item in selected}
                blob_bytes = sum((self.vault / path).stat().st_size for path in blobs)
                self.assertEqual(costs['archive_count'], count)
                self.assertEqual(costs['blob_count'], 1)
                self.assertEqual(costs['raw_bytes'], sum(item['raw_bytes'] for item in selected))
                self.assertEqual(costs['manifest_bytes'], manifest_bytes)
                self.assertEqual(costs['vault_bytes'], manifest_bytes + blob_bytes)
                self.assertEqual(costs['source_bytes_retained'], retained)
                self.assertEqual(costs['net_storage_bytes'], manifest_bytes + blob_bytes + retained)
        # A different namespace must not make a scoped result partial.
        (self.vault / 'manifests' / ('0' * 64 + '.json')).write_text('{}')
        costs = json.loads(self.run_cli('stats', '--tool', 'export').stdout)
        self.assertFalse(costs['partial'])
        self.assertEqual(costs['codex'], {})
        empty = json.loads(self.run_cli('stats', '--tool', 'claude').stdout)
        self.assertEqual(empty['archive_count'], 0)
        self.assertEqual(empty['vault_bytes'], 0)

    def test_unreadable_numeric_export_roundtrips_and_search_continues(self):
        root = self.base / 'numeric export'
        root.mkdir()
        source = root / 'session.jsonl'
        raw = (b'{"value":' + b'9' * 5000 + b'}\n'
               b'{"content":"searchable after numeric record"}\n')
        source.write_bytes(raw)
        age_tree(root)
        self.run_cli('archive', '--tool', 'export', '--source-root', str(root), '--execute')
        source.unlink()  # Only this disposable fixture; prove archive independence.
        result = json.loads(self.run_cli('search', 'searchable after', '--tool', 'export').stdout)
        self.assertEqual(len(result['matches']), 1)
        self.assertEqual(result['skipped_records'], 1)
        archive_id = result['matches'][0]['archive_id']
        self.run_cli('verify', archive_id)
        self.run_cli('restore', archive_id, '--execute')
        self.assertEqual(source.read_bytes(), raw)

    def test_grok_native_and_glm_agy_exports_are_searchable(self):
        grok = self.base / 'grok source'
        updates = grok / 'sessions/%2Fwork/grok-one/updates.jsonl'
        updates.parent.mkdir(parents=True)
        updates.write_text(json.dumps({'update': {'sessionUpdate': 'user_message_chunk',
            'content': {'type': 'text', 'text': 'Grok 공통 검색'}}}) + '\n')
        (updates.parent / 'summary.json').write_text(json.dumps({'sessionId': 'grok-one'}))
        age_tree(grok)
        self.run_cli('archive', '--tool', 'grok', '--source-root', str(grok), '--execute')
        exports = self.base / 'GLM exports'
        exports.mkdir()
        (exports / 'session.json').write_text(json.dumps({'info': {'id': 'glm-one'}, 'messages': [
            {'info': {'role': 'user'}, 'parts': [{'type': 'text', 'text': 'GLM 공통 검색'}]}]}))
        age_tree(exports)
        self.run_cli('archive', '--tool', 'glm', '--source-root', str(exports), '--execute')
        agy = self.base / 'AGY exports'
        agy.mkdir()
        (agy / 'conversation.md').write_text('# Session\nAGY 공통 검색\n')
        age_tree(agy)
        self.run_cli('archive', '--tool', 'agy', '--source-root', str(agy), '--execute')
        hits = json.loads(self.run_cli('search', '공통 검색').stdout)['matches']
        self.assertEqual({hit['tool'] for hit in hits}, {'grok', 'glm', 'agy'})
        only_glm = json.loads(self.run_cli('search', '공통 검색', '--tool', 'glm').stdout)['matches']
        self.assertEqual({hit['tool'] for hit in only_glm}, {'glm'})
        grok_originals = {path.relative_to(grok): path.read_bytes()
                          for path in grok.rglob('*') if path.is_file()}
        grok_id = next(hit['archive_id'] for hit in hits if hit['tool'] == 'grok')
        reclaimed = json.loads(self.run_cli('prune', '--tool', 'grok', '--execute').stdout)
        self.assertEqual(len(reclaimed['removed']), 1)
        self.assertFalse(updates.parent.exists())
        self.assertTrue(json.loads(self.run_cli('search', 'Grok 공통', '--tool', 'grok').stdout)['matches'])
        self.run_cli('restore', grok_id, '--execute')
        for relative, original in grok_originals.items():
            self.assertEqual((grok / relative).read_bytes(), original)

    def test_export_tools_require_explicit_root_without_guessing_native_database(self):
        for tool in ('agy', 'glm', 'opencode', 'export'):
            with self.subTest(tool=tool):
                result = self.run_cli('scan', '--tool', tool, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--source-root', json.loads(result.stdout)['error'])

    def test_legacy_codex_and_new_bundle_coexist(self):
        home = self.base / 'codex source'
        session = home / 'sessions/2025/01/01/rollout-old.jsonl'
        session.parent.mkdir(parents=True)
        session.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session_meta', 'payload': {'id': 'old-codex', 'cwd': '/work'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Codex 공통 검색'}},
        ]) + '\n')
        age_tree(home)
        self.run_cli('archive', '--codex-home', str(home), '--execute')
        claude, _ = self.claude_fixture()
        self.run_cli('archive', '--tool', 'claude', '--source-root', str(claude), '--execute')
        listing = json.loads(self.run_cli('list').stdout)['archives']
        self.assertEqual({item['tool'] for item in listing}, {'codex', 'claude'})
        hits = json.loads(self.run_cli('search', '공통 검색').stdout)['matches']
        self.assertEqual({item['tool'] for item in hits}, {'codex', 'claude'})
        self.assertTrue(json.loads(self.run_cli('verify').stdout)['ok'])
        costs = json.loads(self.run_cli('stats').stdout)
        self.assertEqual(costs['archive_count'], 2)
        self.assertEqual(costs['vault_bytes'], costs['codex']['vault_bytes'] + costs['bundles']['bundle_vault_bytes'])

    def test_gemini_native_json_roundtrips(self):
        home = self.base / 'gemini source'
        transcript = home / 'tmp/project-hash/chats/session-2025-gemini-one.json'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'sessionId': 'gemini-one', 'projectHash': 'project-hash',
            'messages': [{'type': 'user', 'content': 'Gemini 한글 검색'}]}))
        original = transcript.read_bytes()
        age_tree(home)
        self.run_cli('archive', '--tool', 'gemini', '--source-root', str(home), '--execute')
        hits = json.loads(self.run_cli('search', 'Gemini 한글', '--tool', 'gemini').stdout)['matches']
        self.assertEqual(len(hits), 1)
        archive_id = hits[0]['archive_id']
        destination = self.base / 'restored gemini'
        self.run_cli('restore', archive_id, '--destination-home', str(destination), '--execute')
        self.assertEqual((destination / transcript.relative_to(home)).read_bytes(), original)

    @unittest.skipUnless(shutil.which('lsof'), 'lsof is unavailable')
    def test_open_claude_subagent_protects_the_entire_session_group(self):
        home, originals = self.claude_fixture()
        self.run_cli('archive', '--tool', 'claude', '--source-root', str(home), '--execute')
        subagent = home / 'projects/-work/session-one/subagents/agent-a.jsonl'
        with subagent.open('rb'):
            result = json.loads(self.run_cli('prune', '--tool', 'claude', '--execute').stdout)
        self.assertEqual(result['removed'], [])
        self.assertTrue(result['skipped'])
        for relative, original in originals.items():
            self.assertEqual((home / relative).read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
