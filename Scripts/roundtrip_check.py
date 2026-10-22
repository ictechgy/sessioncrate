#!/usr/bin/env python3
"""Exercise the real CLI using generated, disposable multi-tool sessions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

try:
    from compression import zstd
except ImportError:
    zstd = None


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('codex', 'claude', 'grok', 'gemini', 'zcode', 'autoclaw', 'agy', 'glm', 'opencode', 'export')
OLD_NS = 1_700_000_000_123456789


def file_state(path):
    data = path.read_bytes()
    info = path.stat()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
            'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}


def tree_state(root):
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): file_state(path)
            for path in root.rglob('*') if path.is_file()}


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.chmod(path, mode)


def jsonl(rows):
    return ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)


def fixture(root, tool, rng):
    markers = []
    for index in range(2):
        sid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        marker = f'왕복검증 {tool} {sid}'
        output = f'도구결과 {sid}\n' + 'synthetic build 성공, 다시 시도\n' * 900
        markers.append((marker, f'도구결과 {sid}'))
        if tool == 'codex':
            relative = f'{"sessions" if index == 0 else "archived_sessions"}/한글 경로/rollout-{sid}.jsonl'
            payload = jsonl([
                {'type': 'session_meta', 'payload': {'id': sid, 'cwd': '/synthetic/project',
                 'timestamp': '2023-11-14T00:00:00Z', 'cli_version': 'synthetic'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': marker + ' emoji 🧪 Straße'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output',
                 'call_id': sid, 'output': output}},
            ]).encode()
            if index == 1 and zstd is not None:
                relative += '.zst'
                payload = zstd.compress(payload)
            write(root / relative, payload, 0o640 if index else 0o400)
        elif tool == 'claude':
            project = root / 'projects' / '-synthetic-project'
            write(project / f'{sid}.jsonl', jsonl([{'type': 'user', 'sessionId': sid,
                  'cwd': '/synthetic/project', 'message': {'content': marker}}]))
            write(project / sid / 'subagents/agent-one.jsonl', jsonl([
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': output}]}}]))
            write(project / sid / 'tool-results/한글 결과.txt', output, 0o400)
            write(project / sid / 'tool-results/empty.txt', b'')
            (project / sid / 'empty directory').mkdir()
        elif tool == 'grok':
            session = root / 'sessions' / '%2Fsynthetic%2Fproject' / sid
            write(session / 'updates.jsonl', jsonl([
                {'update': {'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': marker}}},
                {'update': {'sessionUpdate': 'tool_call_update', 'content': {'type': 'text', 'text': output}}},
            ]))
            write(session / 'summary.json', json.dumps({'sessionId': sid, 'cwd': '/synthetic/project'}))
            write(session / 'opaque.bin', rng.randbytes(4096))
            (session / 'empty directory').mkdir()
        elif tool == 'gemini':
            write(root / f'tmp/project-hash/chats/session-{sid}.json', json.dumps({
                'sessionId': sid, 'projectHash': 'project-hash', 'messages': [
                    {'type': 'user', 'content': marker},
                    {'type': 'gemini', 'content': '', 'toolCalls': [{'name': 'run_shell_command', 'resultDisplay': output}]},
                ]}, ensure_ascii=False))
        elif tool in ('zcode', 'autoclaw'):
            write(root / f'model-io-sess-{sid}.jsonl', jsonl([
                {'type': 'model_io', 'sessionId': sid,
                 'request': {'messages': [{'role': 'user', 'content': marker}]},
                 'response': {'text': output}},
            ]))
        elif tool in ('glm', 'opencode'):
            write(root / f'한글 exports/{sid}.json', json.dumps({
                'info': {'id': sid, 'cwd': '/synthetic/project'}, 'messages': [
                    {'info': {'role': 'user'}, 'parts': [{'type': 'text', 'text': marker}]},
                    {'info': {'role': 'assistant'}, 'parts': [{'type': 'text', 'text': output}]},
                ]}, ensure_ascii=False))
        else:
            write(root / f'한글 exports/{sid}.{"md" if index == 0 else "txt"}', f'# {marker}\n{output}')
    # This unrelated file must never enter an archive or be reclaimed.
    write(root / 'unrelated.keep', b'synthetic unrelated file')
    for path in sorted(root.rglob('*'), key=lambda path: len(path.parts), reverse=True):
        if path.is_dir():
            os.chmod(path, 0o500 if path.name == 'empty directory' else 0o700)
        os.utime(path, ns=(OLD_NS, OLD_NS))
    return markers


def exercise(seed):
    if not shutil.which('lsof'):
        raise RuntimeError('lsof is required for this real original-reclamation check; no probe is mocked')
    rng = random.Random(seed)
    checks, outcomes, command_count = [], [], 0

    def check(label, condition):
        if not condition:
            raise RuntimeError(f'Roundtrip check failed: {label}; reproduce with --seed {seed}')
        checks.append(label)

    with tempfile.TemporaryDirectory(prefix='vault generated roundtrip ') as temp:
        base = Path(temp).resolve()
        vault = base / 'private vault'

        def cli(*args):
            nonlocal command_count
            command_count += 1
            process = subprocess.run([sys.executable, str(ROOT / 'bin/sessioncrate'), '--vault',
                                      str(vault), '--json', *map(str, args)], cwd=base,
                                     capture_output=True, text=True, timeout=45)
            if process.returncode:
                raise RuntimeError(f'CLI {args[0]} failed with code {process.returncode}: '
                                   f'{process.stdout or process.stderr}')
            return json.loads(process.stdout)

        raw_total, retained_total, file_count = 0, 0, 0
        for tool in TOOLS:
            home = base / f'generated {tool}'
            markers = fixture(home, tool, rng)
            originals = tree_state(home)
            expected = {name: value for name, value in originals.items() if name != 'unrelated.keep'}
            raw_bytes = sum(item['size'] for item in expected.values())
            before = tree_state(vault)
            preview = cli('archive', '--tool', tool, '--source-root', home, '--older-than', 30)
            check(f'{tool}: preview candidates', len(preview['candidates']) == 2)
            check(f'{tool}: preview does not create archives', tree_state(vault) == before)
            archived = cli('archive', '--tool', tool, '--source-root', home, '--execute')['archived']
            check(f'{tool}: archives both sessions', len(archived) == 2)
            check(f'{tool}: copy archive preserves originals', tree_state(home) == originals)
            ids = {item['archive_id'] for item in archived}
            repeated = cli('archive', '--tool', tool, '--source-root', home, '--execute')['archived']
            check(f'{tool}: identical rearchive reuses ids', {item['archive_id'] for item in repeated} == ids)
            check(f'{tool}: no duplicate manifests', len(cli('list', '--tool', tool)['archives']) == 2)
            covered = set()
            directories = {}
            for item in archived:
                if tool == 'codex':
                    covered.add(item['relative_path'])
                else:
                    covered.update(member['path'] for member in item['files'])
                    directories.update({entry['path']: {'mode': entry['mode'], 'mtime_ns': entry['mtime_ns']}
                                        for entry in item['directories']})
            check(f'{tool}: every intended file archived', covered == set(expected))
            raw_total += raw_bytes
            retained_total += raw_bytes
            file_count += len(expected)
            costs = cli('stats')
            check(f'{tool}: retained-source bytes accurate before prune', costs['source_bytes_retained'] == retained_total)
            prune_preview = cli('prune', '--tool', tool)
            check(f'{tool}: idle originals eligible', all(row['eligible'] for row in prune_preview['candidates']))
            check(f'{tool}: prune preview preserves originals', tree_state(home) == originals)
            reclaimed = cli('prune', '--tool', tool, '--execute')
            check(f'{tool}: real prune removes both sessions', len(reclaimed['removed']) == 2 and not reclaimed['skipped'])
            check(f'{tool}: only unrelated file remains', tree_state(home) == {'unrelated.keep': originals['unrelated.keep']})
            after_prune = cli('stats')
            check(f'{tool}: retained-source bytes accurate after prune',
                  after_prune['source_bytes_retained'] == retained_total - raw_bytes)
            for query, output_query in markers:
                check(f'{tool}: search without originals', bool(cli('search', query, '--tool', tool)['matches']))
                check(f'{tool}: tool output search without originals', bool(cli('search', output_query, '--tool', tool)['matches']))
            destination = base / f'restored {tool}'
            for item in archived:
                aid = item['archive_id']
                check(f'{tool}: verify {aid[:8]}', cli('verify', aid[:16])['ok'])
                check(f'{tool}: show {aid[:8]}', bool(cli('show', aid[:16], '--limit', 3)['records']))
                before_restore = tree_state(destination)
                cli('restore', aid[:16], '--destination-home', destination)
                check(f'{tool}: restore preview is read only {aid[:8]}', tree_state(destination) == before_restore)
                cli('restore', aid[:16], '--destination-home', destination, '--execute')
            check(f'{tool}: separate restore bytes/mode/nanosecond mtime match', tree_state(destination) == expected)
            for name, attributes in directories.items():
                info = (destination / name).stat()
                check(f'{tool}: directory metadata {name}',
                      {'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns} == attributes)
            for aid in ids:
                retry = cli('restore', aid, '--destination-home', destination, '--execute')
                check(f'{tool}: identical restore skipped {aid[:8]}', not retry['restored'] and bool(retry['skipped']))
            check(f'{tool}: retry leaves destination unchanged', tree_state(destination) == expected)
            for aid in ids:
                cli('restore', aid, '--execute')
            check(f'{tool}: original-location restore matches', tree_state(home) == originals)
            restored_archive = cli('archive', '--tool', tool, '--source-root', home, '--execute')['archived']
            check(f'{tool}: restored originals rearchive without new versions',
                  {item['archive_id'] for item in restored_archive} == ids)
            outcomes.append({'tool': tool, 'sessions': len(ids), 'files': len(expected),
                             'raw_bytes': raw_bytes, 'pruned_and_restored': True})
        verified = cli('verify')
        costs = cli('stats')
        session_count = 2 * len(TOOLS)
        check('all archives verify after every roundtrip', verified['ok'] and verified['count'] == session_count)
        check('all original source bytes counted after restoration', costs['source_bytes_retained'] == retained_total)
        check('current managed cost includes retained originals',
              costs['net_storage_bytes'] == costs['vault_bytes'] + retained_total)
        check('no duplicate archive versions after repeated roundtrips', costs['archive_count'] == session_count)
        check('normal restore leaves no plaintext stage', not list(base.rglob('.codex-vault-restore-stage-*')))
        return {'ok': True, 'fixture_only': True, 'python': sys.version.split()[0],
                'platform': sys.platform, 'seed': seed, 'tools': outcomes,
                'sessions': session_count, 'files': file_count, 'checks_passed': len(checks),
                'codex_zstd_covered': zstd is not None,
                'cli_commands': command_count, 'raw_bytes': raw_total,
                'vault_bytes_including_metadata': costs['vault_bytes'],
                'logical_reduction_percent_after_prune': round(100 * (1 - costs['vault_bytes'] / raw_total), 2),
                'actual_lsof_used': True, 'fixtures_cleaned_on_exit': True,
                'limits': ['Synthetic sessions; native app sidebar/resume was not exercised.',
                           'Repetitive fixture text is not a prediction of real-session compression.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=None, help='Replay the same generated session content')
    parser.add_argument('--report', type=Path, help='Also save the JSON result at this path')
    args = parser.parse_args()
    seed = args.seed if args.seed is not None else uuid.uuid4().int
    try:
        result = exercise(seed)
    except (OSError, ValueError, RuntimeError) as error:
        result = {'ok': False, 'fixture_only': True, 'seed': seed, 'error': str(error)}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.write_text(encoded)
    print(encoded, end='')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
