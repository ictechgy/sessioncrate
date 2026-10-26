"""Human CLI output, separate from archive operations and JSON contracts."""

import re


_DETAIL_LIMIT = 20
_ANSI_CSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def _clean(value, *, multiline=False):
    text = _ANSI_CSI.sub('', str(value))
    if not multiline:
        text = text.replace('\n', r'\n').replace('\r', r'\r').replace('\t', r'\t')
    return ''.join(ch if ch.isprintable() or (multiline and ch == '\n') else ' ' for ch in text)


def _size(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _clean(value)
    value = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return f'{value:,.1f} {unit}'
        value /= 1024


def _label(item):
    if not isinstance(item, dict):
        return _clean(item)
    return _clean(item.get('relative_path') or ', '.join(item.get('root_entries', []))
                  or item.get('archive_id') or item.get('session_id')
                  or item.get('namespace') or item.get('manifest') or 'source scope')


def _details(items, render):
    for item in items[:_DETAIL_LIMIT]:
        print('  ' + render(item))
    if len(items) > _DETAIL_LIMIT:
        print(f'  ... {len(items) - _DETAIL_LIMIT} more; use --json for all details.')


def _diagnostics(items, title='Errors'):
    if not items:
        return
    print(f'{title}: {len(items)}')
    _details(items, lambda item: f"{_label(item)}: {_clean(item.get('reason') or item.get('error') or 'unavailable')}")


def _archive_row(item, *, full_id=False):
    archive_id = str(item.get('archive_id', ''))
    identifier = archive_id if full_id else archive_id[:12]
    location = item.get('cwd') or item.get('relative_path') or item.get('session_id') or ''
    return (f"[{_clean(item.get('tool', 'codex'))}] {_clean(identifier)}  "
            f"{_size(item.get('raw_bytes', 0))} -> {_size(item.get('compressed_bytes', 0))}  {_clean(location)}")


def _candidates(result):
    rows = result.get('candidates', [])
    eligible = sum(bool(item.get('eligible')) for item in rows)
    print(f'{len(rows)} candidate(s): {eligible} eligible, {len(rows) - eligible} ineligible.')
    def row(item):
        size = next((item[key] for key in ('size', 'source_bytes', 'bytes') if key in item), None)
        measured = f'  {_size(size)}' if size is not None else ''
        return f"{_label(item)}{measured}  {_clean(item.get('reason', ''))}"
    _details(rows, row)


def _print_operation(result, command):
    executed = bool(result.get('executed', result.get('execute', False)))
    if command == 'scan' or not executed:
        print('Preview: no session files changed.')
        source = result.get('source_root') or result.get('codex_home')
        if source:
            print(f'Source: {_clean(source)}')
        _candidates(result)
        if command == 'archive' and result.get('remove_originals_requested'):
            print('Original removal requested: eligible originals will be removed after verification and activity checks.')
        if command in ('archive', 'prune'):
            print(f'To apply this {command}, repeat the same command with --execute.')
        return
    removed = len(result.get('removed', []))
    if command == 'archive':
        archived = result.get('archived', [])
        print(f'Archived {len(archived)} session(s); {removed} original(s) removed.')
        _details(archived, _archive_row)
        ineligible = sum(not item.get('eligible', False) for item in result.get('candidates', []))
        if ineligible:
            print(f'Ineligible candidates: {ineligible}; use scan or --json for reasons.')
    else:
        print(f'Removed {removed} original(s); reclaimed {_size(result.get("removed_source_bytes", 0))}.')
        _details(result.get('removed', []), _label)
    _diagnostics(result.get('skipped', []), 'Originals preserved')
    _diagnostics(result.get('errors', []))


def _print_restore(result):
    restored = result.get('restored', False)
    skipped = result.get('skipped', False)
    restored_count = len(restored) if isinstance(restored, list) else int(bool(restored))
    skipped_count = len(skipped) if isinstance(skipped, list) else int(bool(skipped))
    executed = bool(result.get('executed', result.get('execute', False)))
    if executed:
        print(f'Restore complete: {restored_count} restored, {skipped_count} unchanged file(s).')
    else:
        count = result.get('file_count', 1)
        print(f'Preview: {count} file(s), {skipped_count} already present with identical bytes.')
    print(f"Archive: {_clean(result.get('archive_id', ''))}")
    print(f"Destination: {_clean(result.get('destination_root') or result.get('destination', ''))}")
    if isinstance(restored, list):
        _details(restored, _label)
    if not executed:
        print('To restore, repeat the same command with --execute.')


def _print_stats(result):
    if result.get('partial'):
        print('Partial results: storage totals exclude unreadable namespaces.')
    print(f"Archives: {result.get('archive_count', 0)}")
    for label, key in (('Vault storage', 'vault_bytes'), ('Retained originals', 'source_bytes_retained'),
                       ('Combined storage', 'net_storage_bytes'), ('Allocated vault storage', 'allocated_bytes')):
        print(f'{label}: {_size(result.get(key, 0))}')
    print(f"Temporary files: {result.get('temporary_files', 0)} ({_size(result.get('temporary_bytes', 0))}); included in vault storage.")
    if result.get('temporary_files'):
        print('Temporary files are incomplete work, not verified archives.')
    _diagnostics(result.get('errors', []))


def _print_read(result, command):
    if command == 'search':
        for hit in result['matches']:
            print(f"[{_clean(hit.get('tool', 'codex'))}] {_clean(hit['archive_id'][:12])}  "
                  f"{_clean(hit.get('cwd', ''))}  line {hit['line']}  {_clean(hit['kind'])}")
            source = hit.get('member_path') or hit.get('relative_path')
            if source:
                print(f'  source: {_clean(source)}')
            print(f"  {_clean(hit['snippet'])}")
        print(f"{len(result['matches'])} match(es), {result.get('scanned_archives', 0)} archive(s) scanned")
    else:
        for record in result['records']:
            print(f"[{record.get('line', '?')}] {_clean(record.get('kind', ''))}")
            if record.get('member_path'):
                print(f"  source: {_clean(record['member_path'])}")
            print(_clean(record.get('text', ''), multiline=True))
        print(f"{len(result['records'])} record(s) shown.")
    if result.get('truncated'):
        print('Result limit reached; narrow the query or increase --limit.')
    if result.get('skipped_records'):
        print(f"Skipped {result['skipped_records']} oversized or unsupported record(s).")
    if result.get('errors'):
        print('Partial results: some archive namespaces could not be searched.')
        _diagnostics(result['errors'])


def _print_human(result, *, command=None):
    # Keep direct callers of the former CLI helper compatible. CLI execution
    # always supplies command, since archive and prune have overlapping schemas.
    if command is None:
        if isinstance(result.get('archives'), list):
            command = 'list'
        else:
            command = next((name for key, name in (
                ('tools', 'tools'), ('matches', 'search'), ('records', 'show'),
                ('archived', 'archive'), ('restored', 'restore'), ('archive_count', 'stats'),
                ('verified', 'verify'), ('failed', 'verify'), ('removed', 'prune'),
                ('candidates', 'scan'), ('ok', 'verify'),
            ) if key in result), None)
    if command == 'tools':
        for tool in result['tools']:
            print(f"{tool['id']:10}  {_clean(tool['label'])}  [{tool['mode']}]")
            print(f"  {_clean(tool.get('default_root') or '--source-root REQUIRED')}")
            print(f"  {_clean(tool.get('caveat', ''))}")
    elif command in ('scan', 'archive', 'prune'):
        _print_operation(result, command)
    elif command == 'restore':
        _print_restore(result)
    elif command == 'stats':
        _print_stats(result)
    elif command in ('search', 'show'):
        _print_read(result, command)
    elif command == 'list':
        rows = result['archives']
        for item in rows:
            print(_archive_row(item, full_id=bool(result.get('errors'))))
        if not rows:
            print('No readable archives in the selected scope.' if result.get('errors') else 'No archives in the selected scope.')
        print(f"Showing {len(rows)} of {result.get('total', len(rows))} archive(s).")
        if result.get('truncated'):
            print('List limit reached; narrow --project or increase --limit (maximum 1000).')
        _diagnostics(result.get('errors', []))
    elif command == 'verify':
        if 'archive_id' in result:
            print(f"{'Verified' if result.get('ok') else 'Verification failed'}: {_clean(result['archive_id'])}")
            if result.get('error'):
                print(_clean(result['error']))
        else:
            print(f"Verified {len(result.get('verified', []))} archive(s); {len(result.get('failed', []))} failed.")
            _diagnostics(result.get('failed', []), 'Failures')
    else:
        raise ValueError('human output requires a supported command')
