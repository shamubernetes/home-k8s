#!/usr/bin/env python3
"""Restore the authoritative stash-data native bundle, never pair raw PVC trees."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile


def restore(bundle, config, data, plugins, scrapers):
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError('invalid bundle')
    checksums = json.loads((bundle / 'checksums.json').read_text())
    if set(checksums) != {'stash-go.sqlite', 'config.yml', 'state.tar', 'metadata.json'}:
        raise ValueError('incomplete native bundle')
    for name, expected in checksums.items():
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('unsafe payload')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('native checksum mismatch')
    with contextlib.closing(sqlite3.connect((bundle / 'stash-go.sqlite').absolute().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('native database integrity failed')
    targets = {'blobs': data / 'blobs', 'metadata': data / 'metadata', 'plugins': plugins, 'scrapers': scrapers}
    if len({p.resolve() for p in [config, data, plugins, scrapers]}) != 4:
        raise ValueError('distinct recovery roots required')
    for target in [config, data, plugins, scrapers]:
        if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
            raise ValueError('empty isolated recovery directories required')
    with tarfile.open(bundle / 'state.tar') as archive:
        members = archive.getmembers()
        for member in members:
            path = Path(member.name)
            if not path.parts or path.is_absolute() or '..' in path.parts or path.parts[0] not in targets:
                raise ValueError('unsafe native state path')
            if not (member.isfile() or member.isdir()):
                raise ValueError('unsafe native state object')
        for label, target in targets.items():
            # A fresh per-label scratch prevents plugins or scrapers escaping
            # into an unrelated destination. The archive's leading label stays.
            with tempfile.TemporaryDirectory(prefix='.stash-restore-', dir=target.parent) as temporary:
                stage = Path(temporary)
                archive.extractall(stage, members=[m for m in members if Path(m.name).parts[0] == label], filter='data')
                source = stage / label
                if not source.is_dir():
                    raise ValueError('missing required native state root')
                if target.exists():
                    target.rmdir()
                shutil.move(source, target)
    shutil.copyfile(bundle / 'stash-go.sqlite', config / 'stash-go.sqlite')
    shutil.copyfile(bundle / 'config.yml', config / 'config.yml')
    (config / 'config.yml').chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated', action='store_true', required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    for name in ['config', 'data', 'plugins', 'scrapers']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    restore(args.bundle, args.config, args.data, args.plugins, args.scrapers)
    print('{"native_bundle_restored":true,"original_qualification":false}')


if __name__ == '__main__':
    main()
