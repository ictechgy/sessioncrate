"""Command line interface for private, recoverable Codex transcript archives."""
import argparse
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import sys

from .presentation import _clean, _print_human, _size


def _options(parser, *, subcommand=False):
    fallback = argparse.SUPPRESS if subcommand else None
    parser.add_argument('--codex-home', type=Path, default=fallback,
                        help='Codex data directory (default: CODEX_HOME or ~/.codex)')
    parser.add_argument('--source-root', type=Path, default=fallback,
                        help='Selected tool home, or a directory containing explicit session exports')
    parser.add_argument('--tool', choices=('codex', 'claude', 'grok', 'gemini', 'zcode', 'autoclaw', 'agy', 'glm', 'opencode', 'export', 'all'),
                        default=fallback, help='Source tool; scan/archive default codex, list/search/stats/verify default all')
    parser.add_argument('--vault', type=Path, default=fallback,
                        help='Private archive directory (default: ~/.local/share/codex-vault)')
    parser.add_argument('--json', action='store_true', default=fallback,
                        help='Machine-readable JSON output')


def _command_name(program_name=None):
    if program_name:
        return str(program_name)
    invoked = Path(sys.argv[0]).name
    if invoked in {'sessioncrate', 'codex-vault'}:
        return invoked
    return 'codex-vault'


def parser(program_name=None):
    command_name = _command_name(program_name)
    root = argparse.ArgumentParser(
        prog=command_name,
        description='Compress, search, verify and restore local AI coding session records. No network access.',
    )
    _options(root)
    root.add_argument('--version', action='version', version=f'{command_name} 0.1.0')
    subs = root.add_subparsers(dest='command', required=True)
    descriptions = {
        'scan': 'Read file metadata and preview old session candidates',
        'archive': 'Preview or create compressed, hash-verified archives',
        'prune': 'Reclaim verified originals; skip selected files in use or with unknown activity',
        'list': 'List archived sessions without reading their content',
        'stats': 'Show archive storage and source retention costs',
        'search': 'Search readable content inside compressed archives (literal Unicode text)',
        'show': 'Read bounded text records from one compressed archive',
        'verify': 'Check compressed data against original SHA-256 hashes',
        'restore': 'Preview or restore an archive without overwriting different files',
        'tools': 'Show supported native tools and explicit export adapters',
    }
    commands = {}
    for name, description in descriptions.items():
        command = subs.add_parser(name, help=description, description=description)
        _options(command, subcommand=True)
        commands[name] = command
    for name in ('scan', 'archive'):
        commands[name].add_argument('--older-than', type=int, default=30, metavar='DAYS',
                                    help='Minimum source age in days (default: 30)')
    commands['archive'].add_argument('--execute', action='store_true', help='Create archives; otherwise preview only')
    commands['archive'].add_argument('--remove-originals', action='store_true',
                                     help='Also remove verified originals; requires --execute; in-use or unverifiable files are skipped')
    commands['prune'].add_argument('archive_id', nargs='?', help='Archive id/prefix; omit to preview all archived originals')
    commands['prune'].add_argument('--execute', action='store_true', help='Remove only unchanged, verified originals')
    commands['list'].add_argument('--project', help='Filter by project directory substring')
    commands['list'].add_argument('--limit', type=int, default=100)
    commands['search'].add_argument('query', help='Literal text to find, including Korean or punctuation')
    commands['search'].add_argument('--project', help='Filter by project directory substring')
    commands['search'].add_argument('--session', dest='session_id', help='Session id or archive id prefix')
    commands['search'].add_argument('--limit', type=int, default=20)
    commands['show'].add_argument('archive_id')
    commands['show'].add_argument('--limit', type=int, default=100)
    commands['show'].add_argument('--query', help='Only display records containing this literal text')
    commands['verify'].add_argument('archive_id', nargs='?')
    commands['restore'].add_argument('archive_id')
    commands['restore'].add_argument('--destination-home', type=Path,
                                     help='Restore below a separate source directory, instead of the original location')
    commands['restore'].add_argument('--execute', action='store_true', help='Write restored files; otherwise preview only')
    return root


def _archive_catalog(vault, selected_tool=None, *, prefix=''):
    """Read only matching manifest names, retaining independent integrity errors."""
    from . import bundles, storage
    selected_tool = None if selected_tool == 'all' else selected_tool
    rows, errors = [], []
    root = storage._safe_vault(vault, create=False)
    if root is None:
        return rows, errors
    with storage._vault_lock(root, exclusive=False, create=False):
        namespaces = []
        if selected_tool in (None, 'codex'):
            namespaces.append((storage, root, storage._iter_manifest_paths, storage._read_manifest_path, 'codex'))
        if selected_tool != 'codex':
            # Listing metadata must not depend on the unrelated blob directory.
            try:
                bundle_root = storage._safe_vault(root / 'bundles', create=False)
                if bundle_root is not None and storage._safe_vault_subdir(bundle_root, 'manifests', create=False) is not None:
                    namespaces.append((bundles, bundle_root, bundles._iter_manifest_paths, bundles._read_manifest, 'bundles'))
            except (storage.VaultError, OSError) as error:
                errors.append({'namespace': 'bundles', 'manifest': 'bundles', 'error': str(error)})
        for backend, namespace, paths, read, name in namespaces:
            try:
                if len(prefix) == 64:
                    candidate = namespace / 'manifests' / f'{prefix}.json'
                    storage._check_path_components(candidate.parent)
                    candidates = [candidate] if storage._lstat(candidate) is not None else []
                else:
                    candidates = list(paths(namespace))
            except (storage.VaultError, OSError) as error:
                errors.append({'namespace': name, 'manifest': str((namespace / 'manifests').relative_to(root)),
                               'error': str(error)})
                continue
            for path in candidates:
                archive_id = path.stem
                if not archive_id.startswith(prefix):
                    continue
                try:
                    item = read(path, expected_id=archive_id)
                except (storage.VaultError, OSError) as error:
                    errors.append({'archive_id': archive_id, 'manifest': str(path.relative_to(root)),
                                   'error': str(error)})
                    continue
                if backend is storage:
                    item = {**item, 'tool': 'codex', 'source_mode': 'native'}
                if selected_tool is None or item['tool'] == selected_tool:
                    rows.append((backend, item))
    rows.sort(key=lambda row: (row[1]['archived_at'], row[1]['archive_id']))
    return rows, errors


def _resolve_backend(vault, archive_id, selected_tool=None):
    from .storage import VaultError
    if not isinstance(archive_id, str) or not re.fullmatch(r'[0-9a-f]{1,64}', archive_id):
        raise VaultError('archive id must be a hexadecimal id or unique prefix')
    matches, errors = _archive_catalog(vault, selected_tool, prefix=archive_id)
    if errors and (len(archive_id) != 64 or any(item.get('archive_id') == archive_id for item in errors) or not matches):
        raise VaultError('archive scope could not be fully inspected; verify the vault, use the full id of an intact archive, or narrow --tool')
    if not matches:
        raise VaultError('archive was not found')
    if len(matches) != 1:
        raise VaultError('archive id prefix is ambiguous; use a longer id')
    return matches[0]


def _run(args, vault):
    from . import bundles, sources, storage
    from . import search as legacy_search
    command = args.command
    selected = None if args.tool == 'all' else args.tool
    if command == 'tools':
        return {'tools': sources.tool_catalog()}
    if command in ('scan', 'archive'):
        tool = args.tool or 'codex'
        if tool == 'all':
            raise storage.VaultError('scan/archive select one --tool and source root at a time')
        if args.source_root is not None and args.codex_home is not None:
            raise storage.VaultError('choose --source-root or --codex-home, not both')
        if tool != 'codex' and args.codex_home is not None:
            raise storage.VaultError('--codex-home is only for Codex; use --source-root for other tools')
        try:
            source = args.source_root or args.codex_home or sources.default_source_root(tool)
            if source is not None:
                source = source.expanduser().absolute()
        except storage.VaultError:
            raise
        except RuntimeError as error:
            raise storage.VaultError('source home could not be expanded') from error
        if source is None:
            if tool in ('zcode', 'autoclaw'):
                raise storage.VaultError(f'{tool} requires --source-root with a dedicated model-io diagnostic log directory')
            raise storage.VaultError(f'{tool} requires --source-root with explicit session exports; native databases are not selected')
        if tool == 'codex':
            if command == 'scan':
                return storage.scan(source, older_than_days=args.older_than)
            return storage.archive(source, vault, older_than_days=args.older_than,
                                   execute=args.execute, remove_originals=args.remove_originals)
        if command == 'scan':
            return bundles.scan(tool, source, older_than_days=args.older_than)
        return bundles.archive(tool, source, vault, older_than_days=args.older_than,
                               execute=args.execute, remove_originals=args.remove_originals)
    if command == 'list':
        catalog, errors = _archive_catalog(vault, selected)
        rows = [item for _, item in catalog]
        if args.project:
            rows = [item for item in rows if args.project.casefold() in item.get('cwd', '').casefold()]
        return {'archives': rows[:args.limit], 'total': len(rows), 'truncated': len(rows) > args.limit,
                'errors': errors}
    if command == 'stats':
        legacy, grouped, errors = {}, {}, []
        for name, backend in (('codex', storage), ('bundles', bundles)):
            if selected == 'codex' and name == 'bundles':
                continue
            if selected not in (None, 'codex') and name == 'codex':
                continue
            try:
                costs = backend.stats(vault, tool=selected) if name == 'bundles' else backend.stats(vault)
            except (storage.VaultError, OSError) as error:
                errors.append({'namespace': name, 'error': str(error)})
                continue
            if name == 'codex':
                legacy = costs
            else:
                grouped = costs
        result = dict(legacy)
        for key, bundle_key in (
                ('archive_count', 'bundle_archives'), ('archives', 'bundle_archives'),
                ('blob_count', 'bundle_blobs'), ('blobs', 'bundle_blobs'),
                ('raw_bytes', 'bundle_raw_bytes'), ('raw_archived_bytes', 'bundle_raw_bytes'),
                ('blob_bytes', 'bundle_blob_bytes'), ('compressed_bytes', 'bundle_blob_bytes'),
                ('manifest_bytes', 'bundle_manifest_bytes'), ('vault_bytes', 'bundle_vault_bytes'),
                ('allocated_bytes', 'bundle_allocated_bytes'),
                ('temporary_files', 'bundle_temporary_files'),
                ('temporary_bytes', 'bundle_temporary_bytes'),
                ('temporary_allocated_bytes', 'bundle_temporary_allocated_bytes'),
                ('source_bytes_retained', 'bundle_source_bytes_retained'),
                ('retained_source_bytes', 'bundle_source_bytes_retained')):
            result[key] = legacy.get(key, 0) + grouped.get(bundle_key, 0)
        result['stored_bytes'] = result['vault_bytes']
        result['managed_bytes'] = result['vault_bytes'] + result['source_bytes_retained']
        result['net_storage_bytes'] = result['managed_bytes']
        result['compression_savings_estimate_bytes'] = max(0, result['raw_bytes'] - result['vault_bytes'])
        return {**result, 'codex': legacy, 'bundles': grouped, 'partial': bool(errors), 'errors': errors}
    if command in ('restore', 'show') or (command in ('prune', 'verify') and args.archive_id):
        backend, item = _resolve_backend(vault, args.archive_id, selected)
        archive_id = item['archive_id']
        if command == 'restore':
            try:
                destination = args.destination_home.expanduser().absolute() if args.destination_home is not None else None
            except RuntimeError as error:
                raise storage.VaultError('destination home could not be expanded') from error
            return backend.restore(vault, archive_id, destination_home=destination, execute=args.execute)
        if command == 'show':
            reader = legacy_search if backend is storage else backend
            return reader.show(vault, archive_id, limit=args.limit, query=args.query)
        if command == 'prune':
            return backend.prune(vault, archive_id, execute=args.execute)
        return backend.verify(vault, archive_id)
    if command == 'verify':
        checks = []
        if storage._safe_vault(vault, create=False) is None:
            raise storage.VaultError('vault does not exist; create archives before verification')
        else:
            namespaces = []
            if selected in (None, 'codex'):
                namespaces.append(('codex', storage, {}))
            if selected != 'codex':
                namespaces.append(('bundles', bundles, {'tool': selected}))
            for name, backend, options in namespaces:
                try:
                    check = backend.verify(vault, **options)
                except (storage.VaultError, OSError) as error:
                    check = {'ok': False, 'count': 1, 'verified': [],
                             'failed': [{'ok': False, 'namespace': name, 'error': str(error)}]}
                checks.append({'namespace': name, **check})
        return {'ok': all(item.get('ok', False) for item in checks),
                'count': sum(item.get('count', 0) for item in checks),
                'verified': [record for item in checks for record in item.get('verified', [])],
                'failed': [record for item in checks for record in item.get('failed', [])],
                'checks': checks}
    if command == 'prune':
        if args.tool == 'all':
            raise storage.VaultError('prune requires one --tool or an archive id to keep removal scope explicit')
        tool = args.tool or 'codex'
        if tool == 'codex':
            return storage.prune(vault, execute=args.execute)
        return bundles.prune(vault, execute=args.execute, tool=tool)
    if command == 'search':
        if not args.query.strip():
            raise storage.VaultError('query must not be blank')
        results = []
        errors = []
        namespaces = []
        if selected in (None, 'codex'):
            namespaces.append(('codex', legacy_search, {}))
        if selected != 'codex':
            namespaces.append(('bundles', bundles, {'tool': selected}))
        for name, backend, options in namespaces:
            try:
                result = backend.search(vault, args.query, limit=args.limit, project=args.project,
                                        session_id=args.session_id, **options)
            except (storage.VaultError, OSError) as error:
                errors.append({'namespace': name, 'error': str(error)})
                continue
            if name == 'codex':
                result['matches'] = [{**item, 'tool': 'codex'} for item in result['matches']]
            results.append(result)
        # Keep one busy backend from hiding every result from the other one.
        # Each backend retains its own ordering and integrity checks.
        matches = [hit for row in zip_longest(*(result['matches'] for result in results))
                   for hit in row if hit is not None]
        return {'query': args.query, 'matches': matches[:args.limit],
                'scanned_archives': sum(result.get('scanned_archives', 0) for result in results),
                'skipped_records': sum(result.get('skipped_records', 0) for result in results),
                'partial': bool(errors), 'errors': errors,
                'truncated': len(matches) > args.limit or any(result.get('truncated', False) for result in results)}
    raise storage.VaultError('unsupported command')


def main(argv=None, *, program_name=None):
    args = parser(program_name).parse_args(argv)
    from . import storage
    try:
        if hasattr(args, 'older_than') and args.older_than < 0:
            raise storage.VaultError('--older-than must be nonnegative')
        if hasattr(args, 'limit') and not 1 <= args.limit <= 1000:
            raise storage.VaultError('--limit must be between 1 and 1000')
        try:
            if args.vault is not None:
                vault = args.vault.expanduser()
            else:
                configured_data_home = os.environ.get('XDG_DATA_HOME')
                data_home = Path(configured_data_home) if configured_data_home else Path.home() / '.local/share'
                vault = (data_home / 'codex-vault').expanduser()
        except RuntimeError as error:
            raise storage.VaultError('vault home could not be expanded') from error
        result = _run(args, vault)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_human(result, command=args.command)
        if result.get('ok') is False or result.get('errors') or result.get('failed'):
            return 2
        return 0
    except storage.VaultError as error:
        if isinstance(error, storage.BatchOperationError):
            partial = {**error.partial_result, 'ok': False, 'partial': True, 'error': str(error)}
            if args.json:
                print(json.dumps(partial, ensure_ascii=False, sort_keys=True))
            else:
                print('Operation stopped; completed work and recovery details follow:')
                _print_human(partial, command=args.command)
                print(f'Error: {_clean(error)}', file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({'error': str(error)}, ensure_ascii=False))
        else:
            print(f'Error: {_clean(error)}', file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError) as error:
        # Never emit traceback locals or transcript data on operational failures.
        message = f'Operation failed ({type(error).__name__}); check paths, permissions and archive integrity.'
        if args.json:
            print(json.dumps({'error': message}))
        else:
            print(f'Error: {message}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
