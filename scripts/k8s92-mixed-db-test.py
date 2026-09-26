#!/usr/bin/env python3
"""Disposable, network-isolated real-engine tests. No kubectl or production creds."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

# Logs and exported account/token fixtures must remain private on the host.
os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
# Same SQL engine version, official fixture image. This does not qualify the
# production LSIO entrypoint. Execute only on the approved native remote runner.
MARIA = 'mariadb:11.8.8@sha256:24e76fcec8c003a0362d0dd53f4806e7e79458d7fdeaf47437760e19496f5a9c'
GRIMM = 'docker.io/grimmory/grimmory:v3.5.0@sha256:83597bf02da48a8ec4042fd4d4f58b9631f5b131d6dee7d2fcc5cf3f2eec2526'
TA = 'bbilly1/tubearchivist:v0.5.12@sha256:ba1c846ddd0c6fdd0f040727129d2466e03b7a0c26499223839d450c7586ac09'
ES = 'docker.elastic.co/elasticsearch/elasticsearch:8.19.22@sha256:e98f9c3b09beb2fbb9eaf667d602df3f0e00bd3644138b8458dc17ba1a675595'
REDIS = 'redis:7.4-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499'
PREFIX = 'k8s92-mixed-' + uuid.uuid4().hex[:10]
CONTAINERS = []
NETWORKS = []
EVIDENCE = {'scope': 'synthetic fixture; no live backup or NAS/R2 acceptance'}
SCRATCH_PARENT = Path.home() / '.hermes/cache/scratch'
SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
SCRATCH = Path(tempfile.mkdtemp(prefix=PREFIX + '-', dir=SCRATCH_PARENT))


def run(*args, data=None, env=None, timeout=300, ok=True):
    result = subprocess.run(args, input=data, text=True, capture_output=True, env=env, timeout=timeout)
    if ok and result.returncode:
        # Raw logs may contain credentials or synthetic private content. Keep them local.
        (SCRATCH / 'failure.log').write_text(result.stdout + result.stderr)
        raise RuntimeError(f'command {args[0]} failed rc={result.returncode}; private log {SCRATCH}/failure.log')
    return result


def docker(*args, **kwargs):
    return run('docker', *args, **kwargs)


def network(label):
    name = PREFIX + '-' + label
    docker('network', 'create', '--internal', name)
    NETWORKS.append(name)
    return name


COLIMA_DIRS = []


def fixture_mounts(label, image):
    """Opt-in Colima root-disk storage, never shared Docker pruning/config edits."""
    if os.environ.get('K8S92_COLIMA_ROOT_DISK') != '1':
        return []
    check = run('docker', 'context', 'show').stdout.strip()
    if check != 'colima' or os.environ.get('DOCKER_HOST'):
        raise RuntimeError('root-disk fixture mode requires the default Colima context')
    paths = ['/tmp']
    if image == ES:
        paths += ['/usr/share/elasticsearch/data', '/usr/share/elasticsearch/logs']
    elif image == TA:
        paths += ['/cache']
    elif image == MARIA:
        paths += ['/var/lib/mysql']
    options = []
    for position, destination in enumerate(paths):
        directory = str(SCRATCH.resolve() / (label + '-vm-' + str(position)))
        # The VM cannot see the host's secondary-volume scratch. Create a new,
        # disjoint directory at that path on its root disk for this run only.
        run('colima', 'ssh', '--', 'sudo', '-n', 'mkdir', '-p', directory)
        COLIMA_DIRS.append(directory)
        run('colima', 'ssh', '--', 'sudo', '-n', 'chmod', '700', str(SCRATCH.resolve()))
        run('colima', 'ssh', '--', 'sudo', '-n', 'chmod', '777', directory)
        options += ['--mount', f'type=bind,src={directory},dst={destination}']
    return options


def cleanup_fixture_dirs():
    for directory in COLIMA_DIRS:
        run('colima', 'ssh', '--', 'sudo', '-n', 'rm', '-rf', '--', directory)
        run('colima', 'ssh', '--', 'sudo', '-n', 'test', '!', '-e', directory)
    if COLIMA_DIRS:
        run('colima', 'ssh', '--', 'sudo', '-n', 'rmdir', '--', str(SCRATCH.resolve()))


def start(label, image, net, variables=None, args=(), extra=()):
    name = PREFIX + '-' + label
    CONTAINERS.append(name)
    env = os.environ.copy()
    options = []
    for key, value in (variables or {}).items():
        env[key] = str(value)
        options += ['-e', key]
    options += fixture_mounts(label, image)
    docker('run', '-d', '--name', name, '--network', net, *extra, *options, image, *args, env=env)
    return name


def remove(name):
    docker('rm', '-fv', name)
    CONTAINERS.remove(name)


def wait(check, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for container in CONTAINERS:
            state = json.loads(docker('inspect', '--format', '{{json .State}}', container).stdout)
            if state['Status'] in ('exited', 'dead'):
                raise RuntimeError(f'{container} exited rc={state["ExitCode"]} oom={state["OOMKilled"]}; inspect private run logs')
        if check():
            return
        time.sleep(2)
    raise RuntimeError('readiness deadline expired')


def sql(container, statement, ok=True):
    return docker('exec', '-i', container, 'bash', '-c', 'MYSQL_PWD="$MYSQL_PASSWORD" mariadb --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" --batch --skip-column-names grimmory', data=statement, ok=ok)


def grimmory():
    net = network('maria')
    password = secrets.token_hex(24)
    env = {'MYSQL_ROOT_PASSWORD': secrets.token_hex(24), 'MYSQL_USER': 'grimmory', 'MYSQL_PASSWORD': password, 'MYSQL_DATABASE': 'grimmory', 'PUID': '568', 'PGID': '568'}
    source = start('maria-source', MARIA, net, env)
    wait(lambda: sql(source, 'SELECT 1;', ok=False).returncode == 0)
    appenv = {'DATABASE_URL': 'jdbc:mariadb://127.0.0.1:3306/grimmory', 'DATABASE_USERNAME': 'grimmory', 'DATABASE_PASSWORD': password, 'USER_ID': '568', 'GROUP_ID': '568', 'FORCE_DISABLE_OIDC': 'true', 'JAVA_TOOL_OPTIONS': '-Xmx384m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 --enable-preview'}
    app = start('grimm-source', GRIMM, 'container:' + source, appenv, extra=('--memory=1500m',))
    health = lambda name: docker('exec', name, 'sh', '-c', 'wget -qO- http://127.0.0.1:6060/api/v1/healthcheck >/dev/null', ok=False).returncode == 0
    wait(lambda: health(app), 300)
    baseline = int(sql(source, "SELECT count(*) FROM information_schema.tables WHERE table_schema='grimmory' AND table_type='BASE TABLE';").stdout.strip())
    # Exercise binary, Unicode, triggers, procedures, events and views in addition to the real app schema.
    sql(source, """CREATE TABLE lane_fixture (id INT PRIMARY KEY, body VARBINARY(64), text_value VARCHAR(64)) ENGINE=InnoDB;
INSERT INTO lane_fixture VALUES (1,UNHEX('0001FEFF'),'fixture-雪');
CREATE VIEW lane_view AS SELECT id FROM lane_fixture;
CREATE TRIGGER lane_trigger BEFORE INSERT ON lane_fixture FOR EACH ROW SET NEW.text_value=UPPER(NEW.text_value);
CREATE PROCEDURE lane_proc() SELECT COUNT(*) FROM lane_fixture;
CREATE EVENT lane_event ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1;
""")
    docker('cp', str(ROOT / 'kubernetes/apps/media/grimmory/app/backup.sh'), source + ':/backup.sh')
    capture = docker('exec', source, 'bash', '/backup.sh').stdout.strip()
    docker('cp', source + ':/config/kopiur', str(SCRATCH / 'grimmory'))
    # The source DB and app are removed before restoring the artifact.
    remove(app)
    remove(source)
    restored = start('maria-restored', MARIA, net, env)
    wait(lambda: sql(restored, 'SELECT 1;', ok=False).returncode == 0)
    docker('cp', str(SCRATCH / 'grimmory'), restored + ':/restore')
    docker('cp', str(ROOT / 'kubernetes/apps/media/grimmory/app/restore.sh'), restored + ':/restore.sh')
    docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', restored, 'bash', '/restore.sh', '/restore')
    assert docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', restored, 'bash', '/restore.sh', '/restore', ok=False).returncode != 0
    assert sql(restored, 'SELECT HEX(body),text_value FROM lane_fixture;').stdout.strip() == '0001FEFF\tfixture-雪'
    assert sql(restored, 'CALL lane_proc();').stdout.strip() == '1'
    assert sql(restored, 'SELECT COUNT(*) FROM lane_view;').stdout.strip() == '1'
    sql(restored, "INSERT INTO lane_fixture VALUES(2,UNHEX('01'),'lower');")
    assert sql(restored, 'SELECT text_value FROM lane_fixture WHERE id=2;').stdout.strip() == 'LOWER'
    assert sql(restored, "SELECT COUNT(*) FROM information_schema.events WHERE event_schema='grimmory' AND event_name='lane_event';").stdout.strip() == '1'
    restoredapp = start('grimm-restored', GRIMM, 'container:' + restored, appenv, extra=('--memory=1500m',))
    wait(lambda: health(restoredapp), 300)
    # Negative path must delete stale current artifacts and reject unsafe engines.
    docker('cp', str(ROOT / 'kubernetes/apps/media/grimmory/app/backup.sh'), restored + ':/backup.sh')
    docker('exec', restored, 'bash', '/backup.sh')
    sql(restored, 'CREATE TABLE lane_unsafe(id INT) ENGINE=MyISAM;')
    assert docker('exec', restored, 'bash', '/backup.sh', ok=False).returncode != 0
    assert docker('exec', restored, 'test', '!', '-e', '/config/kopiur/grimmory.sql').returncode == 0
    EVIDENCE['grimmory'] = {'real_app_base_tables': baseline, 'capture': capture, 'binary_unicode_view_trigger_procedure_event_restore': 'pass', 'source_removed_before_restore': True, 'restored_app_health': 'pass', 'unsafe_engine_stale_artifact_rejection': 'pass'}
    print(json.dumps({'grimmory': EVIDENCE['grimmory']}), flush=True)
    remove(restoredapp)
    remove(restored)


def esready(container):
    return docker('exec', container, 'curl', '-fsS', 'http://localhost:9200/_cluster/health?wait_for_status=yellow&timeout=1s', ok=False).returncode == 0


def ta():
    net = network('ta')
    source = start('es-source', ES, net, {'discovery.type': 'single-node', 'xpack.security.enabled': 'false', 'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2', 'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false', 'cluster.name': 'k8s92-ta-source', 'path.repo': '/usr/share/elasticsearch/data/snapshot'}, extra=('--network-alias', 'es', '--memory=1600m'))
    wait(lambda: esready(source), 300)
    redis = start('redis-source', REDIS, net, args=('redis-server', '--save', '', '--appendonly', 'no'), extra=('--network-alias', 'redis'))
    env = {'ES_URL': 'http://es:9200', 'REDIS_CON': 'redis://redis:6379/15', 'TA_HOST': 'http://localhost:8000', 'TA_USERNAME': 'fixture', 'TA_PASSWORD': secrets.token_hex(24), 'ELASTIC_PASSWORD': secrets.token_hex(24), 'TA_PORT': '8000', 'TA_BACKEND_PORT': '8080', 'HOST_UID': '1000', 'HOST_GID': '1000', 'TZ': 'UTC'}
    app = start('ta-source', TA, net, env)
    wait(lambda: docker('exec', app, 'curl', '-fsS', '-H', 'Host: localhost:8000', 'http://localhost:8000/api/health/', ok=False).returncode == 0, 300)
    docker('cp', str(ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py'), app + ':/contract.py')
    fixture = """import sys,json
sys.path.insert(0,'/'); import contract
for index in contract.INDICES:
    if index == 'ta_config': continue
    contract.request(index+'/_doc/lane_fixture?refresh=true','PUT',{'lane_fixture':'unicode-雪','lane_id':42})
# A populated Django account and task table are created by real app startup.
"""
    docker('exec', '-i', app, 'python', '-', data=fixture)
    capture = docker('exec', app, 'python', '/contract.py', 'capture', '/cache/kopiur/current', timeout=660).stdout
    docker('cp', app + ':/cache/kopiur/current', str(SCRATCH / 'tubearchivist'))
    # Preserve the app cache too; replace its raw SQLite with the online artifact below.
    docker('cp', app + ':/cache', str(SCRATCH / 'ta-cache'))
    remove(app)
    remove(source)
    remove(redis)
    # Brand new server and fresh Redis. No source container or data path is reused.
    restored = start('es-restored', ES, net, {'discovery.type': 'single-node', 'xpack.security.enabled': 'false', 'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2', 'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false', 'cluster.name': 'k8s92-ta-restore', 'path.repo': '/usr/share/elasticsearch/data/snapshot'}, extra=('--network-alias', 'es', '--memory=1600m'))
    wait(lambda: esready(restored), 300)
    freshredis = start('redis-restored', REDIS, net, args=('redis-server', '--save', '', '--appendonly', 'no'), extra=('--network-alias', 'redis'))
    assert docker('exec', freshredis, 'redis-cli', '-n', '15', 'DBSIZE').stdout.strip() == '0'
    tool = start('ta-restore-tool', TA, net, env, args=('infinity',), extra=('--entrypoint', 'sleep'))
    docker('cp', str(ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py'), tool + ':/contract.py')
    docker('cp', str(SCRATCH / 'tubearchivist'), tool + ':/bundle')
    docker('exec', tool, 'python', '/contract.py', 'verify', '/bundle')
    result = docker('exec', tool, 'python', '/contract.py', 'restore-es', '/bundle', timeout=660).stdout.strip()
    assert docker('exec', tool, 'python', '/contract.py', 'restore-es', '/bundle', ok=False).returncode != 0
    # Boot a working copy. The immutable recovery artifact stays separate.
    cache = SCRATCH / 'ta-cache'
    shutil.copy2(SCRATCH / 'tubearchivist/db.sqlite3', cache / 'db.sqlite3')
    for suffix in ('-wal', '-shm', '-journal'):
        (cache / ('db.sqlite3' + suffix)).unlink(missing_ok=True)
    # Disable restored schedules on the working copy, never mutate the artifact.
    import sqlite3
    with sqlite3.connect(cache / 'db.sqlite3') as db:
        db.execute('UPDATE django_celery_beat_periodictask SET enabled=0')
    docker('cp', str(cache) + '/.', tool + ':/cache')
    # Use the actual normal entrypoint after the restore, not a fake health server.
    docker('exec', '-d', tool, 'bash', '-c', 'exec /app/run.sh >/cache/lane-boot.log 2>&1')
    wait(lambda: docker('exec', tool, 'curl', '-fsS', '-H', 'Host: localhost:8000', 'http://localhost:8000/api/health/', ok=False).returncode == 0, 300)
    manifest = json.loads((SCRATCH / 'tubearchivist/manifest.json').read_text())
    EVIDENCE['tubearchivist'] = {'native_export': 'pass', 'documents': {k: v['count'] for k, v in manifest['documents'].items()}, 'sqlite_tables': manifest['sqlite_tables'], 'source_removed_before_restore': True, 'es_restore': result, 'redis_initial_dbsize': 0, 'restore_rejects_nonempty_target': 'pass', 'restored_app_health': 'pass'}
    print(json.dumps({'tubearchivist': EVIDENCE['tubearchivist']}), flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', choices=('grimmory', 'tubearchivist', 'all'), default='all')
    app = parser.parse_args().app
    try:
        if app in ('grimmory', 'all'):
            grimmory()
        if app in ('tubearchivist', 'all'):
            ta()
    finally:
        (SCRATCH / 'evidence.json').write_text(json.dumps(EVIDENCE, indent=2) + '\n')
        for container in reversed(CONTAINERS):
            logs = docker('logs', container, ok=False)
            (SCRATCH / (container + '.log')).write_text(logs.stdout + logs.stderr)
            docker('rm', '-fv', container, ok=False)
        for net in reversed(NETWORKS):
            docker('network', 'rm', net, ok=False)
        cleanup_fixture_dirs()
        print('Evidence and fixture artifacts: ' + str(SCRATCH), flush=True)


if __name__ == '__main__':
    main()
