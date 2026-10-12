#!/usr/bin/env python3
"""Prepare the exact controller-restored original in disposable memory only."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shlex
import tarfile

GENERATION = 'd4628863-facf-417b-83b6-6e91e405f572'
SQL = 'e2cd65afce4e8ecafe15c4462162f536b6dd2531ed709e63e1f84ba17493cb69'
ROSTER = '959144d641d4a5b5ebf684563719e98596fe7bd98db61d73ec9e6740985cf93b'
TREE = '4e1f1b2f56bfcb0ff57d15c2fdd01c856302e16591d7a76e29bbeb67c920bb87'
BOOKS_TREE = 'b58648198e93325536b4b3746a720fa8c2d024d8c0404c547b0309f62357a0e3'


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def tree(root):
    rows = []
    for path in [root, *sorted(root.rglob('*'))]:
        details = path.lstat()
        assert not path.is_symlink() and (path.is_file() or path.is_dir())
        rows.append([str(path.relative_to(root)), details.st_mode & 0o7777, details.st_uid,
                     details.st_gid, digest(path) if path.is_file() else None])
    return rows


def extract_archive(source, destination, *, app_owners=False):
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        seen = set()
        assert sum(member.size for member in members) < 512 * 1024 * 1024
        for member in members:
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and '..' not in path.parts
            assert str(path) not in seen
            seen.add(str(path))
            assert member.isdir() or member.isfile()
            assert not member.mode & 0o4000 and (member.isdir() or not member.mode & 0o2000)
            if app_owners:
                assert member.uid == 568 and member.gid == 568
        # All paths/types/modes pass before extraction. Never resolve owner names.
        archive.extractall(destination, filter='fully_trusted', numeric_owner=True)


def prepare():
    root = Path('/data/restored')
    bundle = root / 'mariadb-config/kopiur'
    runtime = bundle / 'runtime'
    for path in [bundle, runtime, *bundle.iterdir(), *runtime.iterdir()]:
        assert not path.is_symlink()
    assert digest(bundle / 'grimmory.sql') == SQL
    assert digest(runtime / 'runtime.sha256') == ROSTER
    assert (bundle / 'COMPLETE').read_text().strip() == GENERATION
    manifest = dict(line.split('=', 1) for line in (bundle / 'manifest.txt').read_text().splitlines())
    assert manifest['format'] == 'mariadb-logical-runtime-v2'
    assert manifest['runtime_generation'] == GENERATION
    assert manifest['sql_sha256'] == SQL and manifest['runtime_manifest_sha256'] == ROSTER
    assert manifest['base_tables'] == '73'
    checks = {}
    for line in (runtime / 'runtime.sha256').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        assert name not in checks
        checks[name] = checksum
    assert set(checks) == {'application.env0', 'application.argv0', 'app-data.tar',
                           'process-start.txt', 'runtime.txt', 'books.tar', 'books.txt'}
    assert all(digest(runtime / name) == checksum for name, checksum in checks.items())
    appdata = Path('/appdata')
    extract_archive(runtime / 'app-data.tar', appdata, app_owners=True)
    before = tree(root / 'grimmory-data')
    assert before == tree(appdata), 'original app tree metadata or bytes differ'
    assert hashlib.sha256(json.dumps(before).encode()).hexdigest() == TREE
    books = Path('/books')
    extract_archive(runtime / 'books.tar', books)
    rows = tree(books)
    assert len(rows) == 14 and sum(row[4] is not None for row in rows) == 11
    # The archive root canonicalizes to '.', matching the independent ARC receipt.
    assert hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest() == BOOKS_TREE
    assert (books / 'bookdrop').is_dir()

    tools = Path('/tools')
    os.chown(tools, 568, 568)
    tools.chmod(0o700)
    password = secrets.token_hex(24)
    environment = {'MYSQL_ROOT_PASSWORD': secrets.token_hex(24), 'MYSQL_PASSWORD': password,
                   'MYSQL_USER': 'grimmory', 'MYSQL_DATABASE': 'grimmory',
                   'PUID': '568', 'PGID': '568', 'TZ': 'UTC'}
    env = {}
    for item in (runtime / 'application.env0').read_bytes().split(b'\0'):
        if item:
            key, value = item.decode().split('=', 1)
            assert key not in env and key.isidentifier()
            env[key] = value
    args = [item.decode() for item in (runtime / 'application.argv0').read_bytes().split(b'\0') if item]
    assert args and args[0].endswith('java') and '-jar' in args
    # Only disposable connection/auth inputs change. Original escrow stays immutable.
    env.update(DATABASE_URL='jdbc:mariadb://127.0.0.1:3306/grimmory', DATABASE_USERNAME='grimmory',
               DATABASE_PASSWORD=password, FORCE_DISABLE_OIDC='true')
    code = '#!/bin/sh\nset -eu\ncd /app\n'
    code += ''.join('export ' + key + '=' + shlex.quote(value) + '\n' for key, value in env.items())
    code += 'exec ' + shlex.join(args) + '\n'
    boot = tools / 'boot.sh'
    boot.write_text(code)
    os.chown(boot, 568, 568)
    boot.chmod(0o500)
    database_env = tools / 'database.env'
    database_env.write_text(''.join(key + '=' + shlex.quote(value) + '\n' for key, value in environment.items()))
    database_env.chmod(0o600)


if __name__ == '__main__':
    try:
        prepare()
    except Exception:
        raise SystemExit('controller original preparation failed; private details withheld') from None
