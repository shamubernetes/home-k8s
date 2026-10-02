#!/usr/bin/env python3
"""Boot an offline working copy of a restored NAS/R2 PVC export, then clean up.

Does not download a backup, connect to Kubernetes, mount NFS or contact live DBs.
"""
import argparse
import importlib.util
from pathlib import Path
import secrets
import shutil
import sqlite3

spec = importlib.util.spec_from_file_location('lane', Path(__file__).with_name('k8s92-mixed-db-test.py'))
lane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane)
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('app', choices=('grimmory', 'tubearchivist'))
p.add_argument('restored_root', type=Path)
p.add_argument('--database-user', default='grimmory', help='Original SQL definer account name; password is always new')
a = p.parse_args()
# Refuse restored links before copying, SQLite writes or container creation.
# A symlinked db.sqlite3 could otherwise overwrite a host file through copy2.
if a.restored_root.is_symlink() or not a.restored_root.is_dir():
    raise ValueError('restored root must be a directory, not a symlink')
root = a.restored_root.resolve()
for path in root.rglob('*'):
    if path.is_symlink() or not (path.is_dir() or path.is_file()):
        raise ValueError('restored PVC contains links or special files')


def created_app(label, image, net, env, local, destination):
    name = lane.PREFIX + '-' + label
    lane.CONTAINERS.append(name)
    process_env = lane.os.environ.copy()
    flags = []
    for key, value in env.items():
        process_env[key] = value
        flags.extend(('-e', key))
    lane.docker('create', '--name', name, '--network', net, *flags, image, env=process_env)
    lane.docker('cp', str(local) + '/.', name + ':' + destination)
    lane.docker('start', name)
    return name


try:
    net = lane.network('restore-only')
    if a.app == 'grimmory':
        bundle = root / 'mariadb-config/kopiur'
        if not (bundle / 'grimmory.sql').is_file() or not (root / 'grimmory-data').is_dir():
            raise RuntimeError('expected full Grimmory PVC root, not physical MariaDB files alone')
        password = secrets.token_hex(24)
        env = {'MYSQL_ROOT_PASSWORD': secrets.token_hex(24), 'MYSQL_USER': a.database_user,
               'MYSQL_PASSWORD': password, 'MYSQL_DATABASE': 'grimmory'}
        db = lane.start('maria', lane.MARIA, net, env)
        lane.wait(lambda: lane.sql(db, 'SELECT 1;', ok=False).returncode == 0)
        lane.docker('cp', str(bundle), db + ':/restore')
        lane.docker('cp', str(lane.ROOT / 'kubernetes/apps/media/grimmory/app/restore.sh'), db + ':/restore.sh')
        lane.docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', db, 'bash', '/restore.sh', '/restore')
        appenv = {'DATABASE_URL': 'jdbc:mariadb://127.0.0.1:3306/grimmory', 'DATABASE_USERNAME': a.database_user,
                  'DATABASE_PASSWORD': password, 'USER_ID': '568', 'GROUP_ID': '568', 'FORCE_DISABLE_OIDC': 'true',
                  'JAVA_TOOL_OPTIONS': '-Xmx384m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 --enable-preview'}
        app = created_app('grimmory', lane.GRIMM, 'container:' + db, appenv, root / 'grimmory-data', '/app/data')
        lane.wait(lambda: lane.docker('exec', app, 'wget', '-qO-', 'http://localhost:6060/api/v1/healthcheck', ok=False).returncode == 0, 300)
    else:
        bundle = root / 'kopiur/current'
        es = lane.start('es', lane.ES, net, {'discovery.type': 'single-node', 'xpack.security.enabled': 'false',
                        'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2', 'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false', 'cluster.name': 'k8s92-ta-restore',
                        'path.repo': '/usr/share/elasticsearch/data/snapshot'}, extra=('--network-alias', 'es'))
        lane.wait(lambda: lane.esready(es), 300)
        redis = lane.start('redis', lane.REDIS, net, args=('redis-server', '--save', '', '--appendonly', 'no'), extra=('--network-alias', 'redis'))
        assert lane.docker('exec', redis, 'redis-cli', '-n', '15', 'DBSIZE').stdout.strip() == '0'
        env = {'ES_URL': 'http://es:9200', 'REDIS_CON': 'redis://redis:6379/15', 'TA_HOST': 'http://localhost:8000',
               'TA_USERNAME': 'restore-audit', 'TA_PASSWORD': secrets.token_hex(24), 'ELASTIC_PASSWORD': secrets.token_hex(24),
               'TA_PORT': '8000', 'TA_BACKEND_PORT': '8080', 'HOST_UID': '1000', 'HOST_GID': '1000', 'TZ': 'UTC'}
        tool = lane.start('tool', lane.TA, net, env, args=('infinity',), extra=('--entrypoint', 'sleep'))
        lane.docker('cp', str(bundle), tool + ':/bundle')
        lane.docker('cp', str(lane.ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py'), tool + ':/contract.py')
        lane.docker('exec', tool, 'python', '/contract.py', 'restore-es', '/bundle', timeout=660)
        cache = lane.SCRATCH / 'working-cache'
        shutil.copytree(root, cache, symlinks=True)
        shutil.copy2(bundle / 'db.sqlite3', cache / 'db.sqlite3')
        for suffix in ('-wal', '-shm', '-journal'):
            (cache / ('db.sqlite3' + suffix)).unlink(missing_ok=True)
        with sqlite3.connect(cache / 'db.sqlite3') as db:
            db.execute('UPDATE django_celery_beat_periodictask SET enabled=0')
        app = created_app('ta', lane.TA, net, env, cache, '/cache')
        lane.wait(lambda: lane.docker('exec', app, 'curl', '-fsS', '-H', 'Host: localhost:8000', 'http://localhost:8000/api/health/', ok=False).returncode == 0, 300)
    print(a.app + ': restored native database and application health passed on an isolated working copy')
finally:
    for container in reversed(lane.CONTAINERS):
        logs = lane.docker('logs', container, ok=False)
        (lane.SCRATCH / (container + '.log')).write_text(logs.stdout + logs.stderr)
        lane.docker('rm', '-fv', container, ok=False)
    for net in lane.NETWORKS:
        lane.docker('network', 'rm', net, ok=False)
    print('Private validation logs: ' + str(lane.SCRATCH))
