#!/usr/bin/env python3
"""Existing ARC mixed-grimmory dispatch, scoped to the deployed native contract.

Synthetic prerequisite only. No Kubernetes, production credentials or NAS/R2.
The candidate's three capture hooks run in order on pinned production images.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

MARIA = 'docker.io/linuxserver/mariadb:11.8.8@sha256:fa63c6ff84bd6bab0a188d68e1b4cd1e4e1b34c22101fc52d544a53dfd502d45'
GRIMM = 'docker.io/grimmory/grimmory:v3.5.0@sha256:83597bf02da48a8ec4042fd4d4f58b9631f5b131d6dee7d2fcc5cf3f2eec2526'
ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / 'kubernetes/apps/media/grimmory/app'
PREFIX = 'k92-grimmory-' + uuid.uuid4().hex[:12]
CONTAINERS, VOLUMES, NETWORKS = [], [], []
SCRATCH: Path
os.umask(0o077)


def run(*args, env=None, data=None, ok=True, timeout=300):
    result = subprocess.run(args, env=env, input=data, capture_output=True, timeout=timeout)
    if ok and result.returncode:
        # Private fixture values stay on this isolated runner, never in CI logs.
        (SCRATCH / 'failure.log').write_bytes(result.stdout + result.stderr)
        raise RuntimeError(f'{args[0]} failed rc={result.returncode}; private fixture log retained')
    return result


def docker(*args, **kwargs):
    return run('docker', *args, **kwargs)


def volume(label):
    name = PREFIX + '-' + label
    docker('volume', 'create', name)
    VOLUMES.append(name)
    return name


def start(label, image, network, variables, mounts=(), setup=None):
    name = PREFIX + '-' + label
    CONTAINERS.append(name)
    environment = dict(os.environ, **variables)
    options = []
    for key in variables:
        options += ['-e', key]
    for source, target, readonly in mounts:
        options += ['-v', f'{source}:{target}' + (':ro' if readonly else '')]
    docker('create', '--name', name, '--network', network, '--memory=1500m',
           *options, image, env=environment)
    if setup:
        setup(name)
    docker('start', name)
    return name


def remove(name):
    docker('rm', '-fv', name)
    CONTAINERS.remove(name)


def wait(check):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    raise RuntimeError('native fixture readiness deadline expired')


def sql(container, statement, ok=True):
    return docker('exec', container, 'bash', '-c',
                  'MYSQL_PWD="$MYSQL_PASSWORD" mariadb --protocol=tcp -h127.0.0.1 '
                  '-u"$MYSQL_USER" --batch --skip-column-names -e "$1"',
                  'fixture-sql', statement, ok=ok)


def health(container):
    return docker('exec', container, 'sh', '-c',
                  'wget -qO- http://127.0.0.1:6060/api/v1/healthcheck >/dev/null',
                  ok=False).returncode == 0


def helpers(container):
    for name in ('backup.sh', 'restore.sh', 'runtime.sh', 'books.sh'):
        if (HELPERS / name).exists():
            docker('cp', str(HELPERS / name), container + ':/' + name)


def hook(database):
    return docker('exec', database, 's6-setuidgid', 'abc', 'bash', '/backup.sh')


def grimmory():
    runtime = (HELPERS / 'runtime.sh').exists()
    net = PREFIX + '-source'
    docker('network', 'create', '--internal', net)
    NETWORKS.append(net)
    database_env = {'MYSQL_ROOT_PASSWORD': secrets.token_hex(24), 'MYSQL_USER': 'grimmory',
                    'MYSQL_PASSWORD': secrets.token_hex(24), 'MYSQL_DATABASE': 'grimmory',
                    'PUID': '568', 'PGID': '568', 'TZ': 'UTC'}
    application_env = {'DATABASE_URL': 'jdbc:mariadb://127.0.0.1:3306/grimmory',
                       'DATABASE_USERNAME': 'grimmory', 'DATABASE_PASSWORD': database_env['MYSQL_PASSWORD'],
                       'USER_ID': '568', 'GROUP_ID': '568', 'FORCE_DISABLE_OIDC': 'true',
                       # Disposable fixture aliases the exact Books subtree.
                       # Production separately proves both NFS mounts and writers.
                       'GRIMMORY_BOOKDROP': '/books/bookdrop',
                       'JAVA_TOOL_OPTIONS': '-Xmx384m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 --enable-preview'}
    dbdata, appdata, runtime_data = volume('source-db'), volume('source-app'), volume('source-runtime')
    booksdata = volume('source-books')
    database = start('database-source', MARIA, net, database_env,
                     ((dbdata, '/config', False), (runtime_data, '/runtime-escrow', True)))
    wait(lambda: sql(database, 'SELECT 1', ok=False).returncode == 0)
    app = start('app-source', GRIMM, 'container:' + database, application_env,
                ((appdata, '/app/data', False), (dbdata, '/database-state', True),
                 (runtime_data, '/runtime-escrow', False), (booksdata, '/books', False)))
    wait(lambda: health(app))
    helpers(database)
    helpers(app)
    docker('exec', app, 'sh', '-c',
           'mkdir -p /books/bookdrop; printf "synthetic-book" >/books/bookdrop/fixture.txt')
    sql(database, "CREATE TABLE grimmory.lane_fixture(id INT PRIMARY KEY, body VARBINARY(64), text_value VARCHAR(64)) ENGINE=InnoDB;"
                  "INSERT INTO grimmory.lane_fixture VALUES(1,UNHEX('0001FEFF'),'fixture-雪');"
                  "CREATE VIEW grimmory.lane_view AS SELECT id FROM grimmory.lane_fixture;"
                  "CREATE PROCEDURE grimmory.lane_proc() SELECT COUNT(*) FROM grimmory.lane_fixture;")
    if runtime:
        docker('exec', app, 'sh', '-c', 'mkdir -p /app/data/native-config; printf "synthetic-config" >/app/data/native-config/value')
        docker('exec', app, 'sh', '/runtime.sh', 'capture')
        assert docker('exec', '--user=568:568', app, 'sh', '-c',
                      'printf unsafe > /runtime-escrow/.lock', ok=False).returncode != 0
        # Even a preexisting link must not redirect a root control write.
        docker('exec', app, 'sh', '-c',
               'printf preserve >/app/data/control-victim; rm /runtime-escrow/.lock; '
               'ln -s /app/data/control-victim /runtime-escrow/.lock')
        assert docker('exec', app, 'sh', '/runtime.sh', 'capture', ok=False).returncode != 0
        assert docker('exec', app, 'cat', '/app/data/control-victim').stdout == b'preserve'
        docker('exec', app, 'rm', '/runtime-escrow/.lock', '/app/data/control-victim')
        docker('exec', app, 'sh', '/runtime.sh', 'capture')
        # Every captured file, including control files, must be readable by UID568.
        docker('exec', database, 's6-setuidgid', 'abc', 'sh', '-c',
               'find /runtime-escrow -type f -exec cat {} + >/dev/null')
    hook(database)
    if runtime:
        docker('exec', '-e', 'GRIMMORY_DB_BUNDLE=/database-state/kopiur', app,
               'sh', '/runtime.sh', 'verify')
        # Configuration drift must fail before the CSI/mover phase.
        docker('exec', app, 'sh', '-c', 'printf changed >/app/data/drift-fixture')
        assert docker('exec', '-e', 'GRIMMORY_DB_BUNDLE=/database-state/kopiur', app,
                      'sh', '/runtime.sh', 'verify', ok=False).returncode != 0
        docker('exec', app, 'rm', '/app/data/drift-fixture')
        docker('exec', app, 'sh', '/runtime.sh', 'capture')
        hook(database)
        docker('exec', '-e', 'GRIMMORY_DB_BUNDLE=/database-state/kopiur', app,
               'sh', '/runtime.sh', 'verify')
    docker('cp', database + ':/config/kopiur', str(SCRATCH / 'bundle'))
    original = sql(database, 'SELECT HEX(body),text_value FROM grimmory.lane_fixture').stdout
    remove(app)
    remove(database)
    # A new engine with no network interface, not the source process or source volume.
    restored = start('database-restored', MARIA, 'none', database_env,
                     ((volume('restore-db'), '/config', False),))
    wait(lambda: sql(restored, 'SELECT 1', ok=False).returncode == 0)
    helpers(restored)
    docker('cp', str(SCRATCH / 'bundle'), restored + ':/restore')
    restore = lambda ok=True: docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', restored,
                                      'bash', '/restore.sh', '/restore', ok=ok)
    if runtime:
        # Replacement SQL plus a matching standalone checksum cannot change its generation.
        docker('exec', restored, 'bash', '-c',
               'cp /restore/grimmory.sql /restore/original.sql; printf "SELECT 1;" >/restore/grimmory.sql; '
               'cd /restore; sha256sum grimmory.sql >grimmory.sql.sha256')
        assert restore(False).returncode != 0
        docker('exec', restored, 'bash', '-c',
               'mv /restore/original.sql /restore/grimmory.sql; cd /restore; sha256sum grimmory.sql >grimmory.sql.sha256')
        docker('exec', restored, 'bash', '-c',
               'cp /restore/runtime/runtime.sha256 /restore/original-runtime.sha256; '
               'cd /restore/runtime; sha256sum runtime.txt >runtime.sha256; '
               'digest=$(sha256sum runtime.sha256 | cut -d" " -f1); '
               'sed -i "s/^runtime_manifest_sha256=.*/runtime_manifest_sha256=$digest/" /restore/manifest.txt')
        assert restore(False).returncode != 0
        # Restore the immutable original bundle, not a patched generation.
        docker('exec', restored, 'rm', '-rf', '/restore')
        docker('cp', str(SCRATCH / 'bundle'), restored + ':/restore')
    restore()
    assert sql(restored, 'SELECT HEX(body),text_value FROM grimmory.lane_fixture').stdout == original
    assert sql(restored, 'CALL grimmory.lane_proc()').stdout.strip() == b'1'
    assert restore(False).returncode != 0
    if runtime:
        env0 = (SCRATCH / 'bundle/runtime/application.env0').read_bytes()
        application_env = dict(item.decode().split('=', 1) for item in env0.split(b'\0') if item)
    restored_appdata = volume('restore-app')
    restored_booksdata = volume('restore-books')
    if runtime:
        import tarfile
        for payload in ('app-data.tar', 'books.tar'):
            with tarfile.open(SCRATCH / 'bundle/runtime' / payload) as archive:
                for member in archive.getmembers():
                    path = Path(member.name)
                    assert not path.is_absolute() and '..' not in path.parts
                    assert member.isdir() or member.isfile()
                    assert not member.mode & 0o4000
                    assert member.isdir() or not member.mode & 0o2000
        # Extract into the fresh disposable volume while the native app is absent.
        tool = PREFIX + '-unpack'
        CONTAINERS.append(tool)
        docker('create', '--name', tool, '--network', 'none', '--entrypoint', 'sleep',
               '-v', restored_appdata + ':/app/data', '-v', restored_booksdata + ':/books',
               GRIMM, 'infinity')
        docker('start', tool)
        docker('cp', str(SCRATCH / 'bundle/runtime/app-data.tar'), tool + ':/app-data.tar')
        docker('exec', tool, 'tar', '-xf', '/app-data.tar', '-C', '/app/data')
        docker('cp', str(SCRATCH / 'bundle/runtime/books.tar'), tool + ':/books.tar')
        docker('exec', tool, 'tar', '-xf', '/books.tar', '-C', '/books')
        remove(tool)
    restored_app = start('app-restored', GRIMM, 'container:' + restored, application_env,
                         ((restored_appdata, '/app/data', False), (restored_booksdata, '/books', True)))
    wait(lambda: health(restored_app))
    if runtime:
        assert docker('exec', restored_app, 'cat', '/app/data/native-config/value').stdout == b'synthetic-config'
        assert docker('exec', restored_app, 'cat', '/books/bookdrop/fixture.txt').stdout == b'synthetic-book'
    assert sql(restored, 'SELECT HEX(body),text_value FROM grimmory.lane_fixture').stdout == original
    print(json.dumps({'suite': 'mixed-grimmory', 'scope': 'synthetic prerequisite, not original NAS/R2',
                      'production_images': True, 'runtime_bundle': runtime,
                      'ordered_native_hooks': True, 'source_removed': True,
                      'restored_database_network': 'none', 'native_data_equality': True,
                      'effective_runtime_boot': runtime, 'native_health': True}), flush=True)


def main():
    global SCRATCH
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', choices=('grimmory',), required=True)
    parser.parse_args()
    SCRATCH = Path(tempfile.mkdtemp(prefix=PREFIX, dir=os.environ['TMPDIR']))
    try:
        # Grimmory's shared Books original needs this exact CWA writer guard.
        # Reuse the trusted mixed-grimmory dispatch without new runner privileges.
        import runpy
        runpy.run_path(str(ROOT / 'scripts/kopiur-cwa-startup-hold-native.py'))['main']()
        grimmory()
    finally:
        cleanup_failed = []
        for kind, names, command in (('container', CONTAINERS[:], ('rm', '-fv')),
                                     ('network', NETWORKS, ('network', 'rm')),
                                     ('volume', VOLUMES, ('volume', 'rm'))):
            for name in names:
                try:
                    if docker(*command, name, ok=False, timeout=60).returncode:
                        cleanup_failed.append(kind)
                except subprocess.TimeoutExpired:
                    cleanup_failed.append(kind)
        shutil.rmtree(SCRATCH)
        if cleanup_failed:
            raise RuntimeError('owned fixture cleanup failed: ' + ','.join(cleanup_failed))


if __name__ == '__main__':
    main()
