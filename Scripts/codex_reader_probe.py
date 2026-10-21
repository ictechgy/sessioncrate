#!/usr/bin/env python3
"""Check legacy Codex rollout reading before/after restoration, offline on macOS.

This does not start a model turn or certify native session continuation.
"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def read_thread(command, project, environment, thread_id, markers, error_path):
    with error_path.open('wb') as error:
        process = subprocess.Popen([*command, 'app-server', '--listen', 'stdio://'],
                                   cwd=project, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=error)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b''
        try:
            def rpc(identifier, method, params):
                nonlocal buffer
                process.stdin.write((json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n').encode())
                process.stdin.flush()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        message = json.loads(line)
                        if message.get('id') == identifier:
                            if 'error' in message:
                                raise RuntimeError(f'native {method} returned an error')
                            return message['result']
                    if process.poll() is not None:
                        raise RuntimeError('native app-server exited before the response')
                    if selector.select(0.25):
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if chunk:
                            buffer += chunk
                raise RuntimeError(f'native {method} timed out')

            rpc(1, 'initialize', {'clientInfo': {'name': 'sessioncrate_offline_probe', 'version': '0.1.0'},
                                  'capabilities': {'experimentalApi': True}})
            process.stdin.write(b'{"method":"initialized"}\n')
            process.stdin.flush()
            result = rpc(2, 'thread/read', {'threadId': thread_id, 'includeTurns': True})
            thread = result.get('thread', {})
            turns = thread.get('turns', [])
            encoded = json.dumps(turns)
            evidence = {'thread_id_matches': thread.get('id') == thread_id,
                        'turns_returned': bool(turns), 'user_marker_read': markers[0] in encoded,
                        'assistant_marker_read': markers[1] in encoded}
            if not all(evidence.values()):
                raise RuntimeError('native reader did not hydrate the expected synthetic history')
            return evidence
        finally:
            selector.close()
            process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)


def main():
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec') or not shutil.which('codex'):
        raise RuntimeError('This opt-in probe requires macOS sandbox-exec and an installed Codex CLI')
    with tempfile.TemporaryDirectory(prefix='sessioncrate-native-reader-') as temporary:
        base = Path(temporary).resolve()
        project = base / 'project'
        project.mkdir()
        profile_home = base / 'codex-home'
        profile_home.mkdir()
        policy = base / 'offline.sb'
        restrictions = ['(version 1)', '(allow default)', '(deny network*)']
        for name in ('.codex', '.claude', '.grok', '.gemini', '.agents', '.config'):
            restrictions.append('(deny file-read* file-write* (subpath ' + json.dumps(str(Path.home() / name)) + '))')
        policy.write_text('\n'.join(restrictions))
        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith(('OPENAI_', 'CODEX_', 'ANTHROPIC_', 'XAI_', 'GROK_', 'GEMINI_')):
                environment.pop(key, None)
        environment.update(CODEX_HOME=str(profile_home), XDG_CONFIG_HOME=str(base / 'config'),
                           XDG_CACHE_HOME=str(base / 'cache'), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
        command = ['sandbox-exec', '-f', str(policy), shutil.which('codex'), '-c', 'cli_auth_credentials_store="file"']
        version = subprocess.run([*command, '--version'], cwd=project, env=environment,
                                 capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        sid = str(uuid.uuid4())
        markers = ['sessioncrate synthetic user ' + sid, 'sessioncrate synthetic assistant ' + sid]
        timestamp = '2026-01-01T00:00:00Z'
        rows = [
            {'type': 'session_meta', 'payload': {'id': sid, 'timestamp': timestamp, 'cwd': str(project),
             'originator': 'codex_cli_rs', 'cli_version': version.removeprefix('codex-cli '),
             'source': 'cli', 'model_provider': 'openai'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': sid,
             'model_context_window': 4096, 'collaboration_mode_kind': 'default'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': markers[0],
             'images': [], 'local_images': [], 'text_elements': []}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': markers[1], 'phase': 'final_answer'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': sid, 'last_agent_message': markers[1]}},
        ]
        raw = b''.join((json.dumps({'timestamp': timestamp, **row}) + '\n').encode() for row in rows)
        source = profile_home / 'sessions/2026/01/01' / f'rollout-2026-01-01T00-00-00-{sid}.jsonl'
        source.parent.mkdir(parents=True)
        source.write_bytes(raw)
        old = time.time() - 60 * 86400
        os.utime(source, (old, old))
        before = read_thread(command, project, environment, sid, markers, base / 'before.stderr')
        vault = base / 'vault'

        def cli(*arguments):
            result = subprocess.run([sys.executable, str(ROOT / 'bin/sessioncrate'), '--vault', str(vault),
                                     '--json', *map(str, arguments)], cwd=base, capture_output=True,
                                    text=True, timeout=30, check=True)
            return json.loads(result.stdout)

        saved = cli('archive', '--tool', 'codex', '--source-root', profile_home, '--execute')['archived']
        if len(saved) != 1:
            raise RuntimeError('expected one synthetic rollout archive')
        removed = cli('prune', saved[0]['archive_id'], '--execute')
        if len(removed['removed']) != 1 or source.exists():
            raise RuntimeError('synthetic original was not reclaimed')
        cli('restore', saved[0]['archive_id'], '--execute')
        if source.read_bytes() != raw:
            raise RuntimeError('restored synthetic rollout differs')
        after = read_thread(command, project, environment, sid, markers, base / 'after.stderr')
        print(json.dumps({'ok': True, 'application': version, 'network_denied_by_os': True,
                          'real_profile_directories_denied_by_os': True, 'credentials_copied': False,
                          'before': before, 'after': after, 'sha256': hashlib.sha256(raw).hexdigest(),
                          'byte_exact': True, 'native_resume_tested': False,
                          'scope': 'Legacy EventMsg history reading only; no thread/resume or model turn'}, indent=2))


if __name__ == '__main__':
    main()
