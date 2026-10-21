#!/usr/bin/env python3
"""Demonstrate archive/search/restore on disposable synthetic records only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='codex-vault-demo-') as directory:
        base = Path(directory).resolve()
        home = base / 'synthetic codex'
        vault = base / 'vault'
        destination = base / 'restored'
        source = home / 'sessions/2025/01/01/rollout-demo.jsonl'
        source.parent.mkdir(parents=True)
        records = [{'type': 'session_meta', 'payload': {
            'id': 'synthetic-vault-demo', 'cwd': '/demo/project',
            'timestamp': '2025-01-01T00:00:00Z', 'cli_version': 'fixture',
        }}]
        for number in range(80):
            records.extend([
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': f'한글 검색 예제 {number}: 결제 재시도 동작 확인'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output',
                 'call_id': str(number), 'output': ('synthetic test build output: passed\n' * 40)}},
            ])
        original = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records).encode()
        source.write_bytes(original)
        old = time.time() - 60 * 86400
        os.utime(source, (old, old))

        def run(*args):
            result = subprocess.run(
                [sys.executable, str(ROOT / 'bin/sessioncrate'), '--codex-home', str(home),
                 '--vault', str(vault), '--json', *args],
                cwd=base, capture_output=True, text=True, timeout=60,
            )
            if result.returncode:
                raise RuntimeError(f'Demo command failed: {args[0]}: {result.stdout or result.stderr}')
            return json.loads(result.stdout)

        run('archive', '--execute')
        archive_id = run('list')['archives'][0]['archive_id']
        assert source.read_bytes() == original
        # This is our own temporary fixture, never a personal Codex transcript.
        # Actual user-source pruning is separately guarded by the CLI.
        source.unlink()
        results = run('search', '결제 재시도', '--limit', '3')
        assert results['matches']
        verified = run('verify', archive_id)
        assert verified['ok']
        run('restore', archive_id, '--destination-home', str(destination), '--execute')
        restored = destination / source.relative_to(home)
        assert restored.read_bytes() == original
        stored_bytes = sum(path.stat().st_size for path in vault.rglob('*') if path.is_file())
        print(json.dumps({
            'fixture_only': True,
            'raw_input_bytes': len(original),
            'vault_bytes_including_metadata': stored_bytes,
            'logical_reduction_percent': round(100 * (1 - stored_bytes / len(original)), 2),
            'search_matches_returned': len(results['matches']),
            'sha256_roundtrip_verified': hashlib.sha256(restored.read_bytes()).digest() == hashlib.sha256(original).digest(),
            'note': 'Synthetic repetitive text; actual savings depend on your sessions. No real Codex data was accessed.',
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
