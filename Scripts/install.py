#!/usr/bin/env python3
"""Install local launchers only; never open or modify Codex session data."""
import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def _target(path):
    path = Path(os.path.abspath(path.expanduser()))
    for parent in reversed(path.parents):
        if parent.is_symlink():
            raise ValueError(f'Refusing symlinked installation parent: {parent}')
        if parent.exists() and not parent.is_dir():
            raise ValueError(f'Installation parent is not a directory: {parent}')
    return path


def _created_link_state(path, target):
    """Capture enough identity to avoid deleting a replacement during rollback."""
    info = os.lstat(path)
    return {'path': path, 'target': os.fspath(target),
            'identity': (info.st_dev, info.st_ino)}


def install(bin_dir, skills_dir, *, execute=False, legacy_aliases=False):
    targets = [
        (_target(bin_dir / 'sessioncrate'), ROOT / 'bin/sessioncrate'),
        (_target(skills_dir / 'sessioncrate'), ROOT / 'skills/sessioncrate'),
    ]
    if legacy_aliases:
        targets.extend([
            (_target(bin_dir / 'codex-vault'), ROOT / 'bin/codex-vault'),
            (_target(skills_dir / 'codex-vault'), ROOT / 'skills/codex-vault'),
        ])
    actions = []
    for destination, source in targets:
        if not source.exists():
            raise ValueError(f'Checkout file is missing: {source}')
        if destination.is_symlink() and destination.resolve() == source.resolve():
            actions.append({'path': str(destination), 'target': str(source), 'action': 'unchanged'})
        elif destination.exists() or destination.is_symlink():
            raise ValueError(f'Existing path belongs to another installation; left untouched: {destination}')
        else:
            actions.append({'path': str(destination), 'target': str(source), 'action': 'create-symlink'})
    if execute:
        created = []
        try:
            for action in actions:
                if action['action'] == 'create-symlink':
                    destination = Path(action['path'])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _target(destination)  # Re-check after mkdir, before exclusive publication.
                    destination.symlink_to(action['target'], target_is_directory=Path(action['target']).is_dir())
                    created.append(_created_link_state(destination, action['target']))
        except (OSError, ValueError) as error:
            rollback_conflicts = []
            for record in reversed(created):
                destination = record['path']
                try:
                    info = os.lstat(destination)
                    current_target = os.readlink(destination)
                except FileNotFoundError:
                    continue
                except OSError as rollback_error:
                    rollback_conflicts.append(f'{destination}: {rollback_error}')
                    continue
                current_identity = (info.st_dev, info.st_ino)
                if current_identity != record['identity'] or current_target != record['target']:
                    rollback_conflicts.append(f'{destination}: path was replaced during rollback')
                    continue
                try:
                    destination.unlink()
                except OSError as rollback_error:
                    rollback_conflicts.append(f'{destination}: {rollback_error}')
            if rollback_conflicts:
                detail = '; '.join(rollback_conflicts)
                raise ValueError(f'{error}; rollback conflict: {detail}') from error
            raise
    return {'executed': execute, 'actions': actions,
            'note': 'Local launchers only. No sessions, credentials, config or shell profiles were read or changed.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path)
    parser.add_argument('--skills-dir', type=Path)
    parser.add_argument('--execute', action='store_true', help='Create launchers; otherwise preview only')
    parser.add_argument('--legacy-aliases', action='store_true',
                        help='Also install the old codex-vault launcher and skill aliases')
    args = parser.parse_args(argv)
    try:
        default_home = Path.home() if args.bin_dir is None or args.skills_dir is None else None
        bin_dir = args.bin_dir if args.bin_dir is not None else default_home / '.local/bin'
        skills_dir = args.skills_dir if args.skills_dir is not None else default_home / '.agents/skills'
        print(json.dumps(install(bin_dir, skills_dir, execute=args.execute,
                                 legacy_aliases=args.legacy_aliases), ensure_ascii=False, indent=2))
        return 0
    except RuntimeError:
        print('Error: Installation home could not be expanded; set --bin-dir and --skills-dir explicitly.', file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
