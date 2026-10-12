#!/usr/bin/env python3
"""Grimmory's exact-image, private, disconnected process-fence prerequisite."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'docker.io/grimmory/grimmory:v3.5.0@sha256:83597bf02da48a8ec4042fd4d4f58b9631f5b131d6dee7d2fcc5cf3f2eec2526'
MARIA = 'docker.io/linuxserver/mariadb:11.8.8@sha256:fa63c6ff84bd6bab0a188d68e1b4cd1e4e1b34c22101fc52d544a53dfd502d45'
HELPERS = ROOT / 'kubernetes/apps/media/grimmory/app'
HOLDER = ROOT / 'scripts/kopiur-grimmory-fence.sh'
PRODUCTION_HOLDER = HELPERS / 'fence.sh'
if PRODUCTION_HOLDER.exists():
    if PRODUCTION_HOLDER.read_bytes() != HOLDER.read_bytes():
        raise RuntimeError('production fence differs from qualified contract')
    HOLDER = PRODUCTION_HOLDER
NATIVE = ['/usr/local/bin/entrypoint.sh', 'java', '--enable-native-access=ALL-UNNAMED', '--enable-preview', '-jar', '/app/app.jar']
OWNER = uuid.uuid4().hex
PREFIX = 'k92-grimm-fence-' + OWNER[:12]
LABEL = 'k92.grimmory-fence-owner'
CREATED = []
VOLUMES = []


def volume(suffix):
    name = PREFIX + '-' + suffix
    VOLUMES.append(name)
    docker('volume', 'create', '--label', LABEL + '=' + OWNER, name)
    return name


def docker(*args, check=True, timeout=90, env=None):
    result = subprocess.run(['docker', *args], env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('private native fence operation failed')
    return result


def wait(predicate, seconds=180):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.25)
    raise RuntimeError('native fence proof deadline expired')


def create(name, image, network, variables, extra=(), command=()):
    # Disposable fixture credentials travel in inherited environment, not argv.
    env = dict(os.environ, **variables)
    options = [value for key in variables for value in ('-e', key)]
    CREATED.append(name)
    docker('create', '--name', name, '--label', LABEL + '=' + OWNER,
           '--network', network, '--log-driver=none', '--memory=2g',
           '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
           '--cap-add=FOWNER', '--cap-add=DAC_OVERRIDE', '--security-opt=no-new-privileges',
           *options, *extra, image, *command, env=env)


def inspect(name):
    return json.loads(docker('inspect', name).stdout)[0]


def owned_cleanup():
    for name in reversed(CREATED):
        result = docker('inspect', name, check=False)
        if result.returncode:
            if 'no such' not in result.stderr.lower():
                raise RuntimeError('owned cleanup lookup failed')
            continue
        info = json.loads(result.stdout)[0]
        if info['Config']['Labels'].get(LABEL) != OWNER:
            raise RuntimeError('owned cleanup identity failed')
        docker('rm', '-f', name)
        if docker('container', 'ls', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').stdout.strip():
            raise RuntimeError('owned container remains')
    for name in reversed(VOLUMES):
        result = docker('volume', 'inspect', name, check=False)
        if result.returncode:
            if 'no such volume' not in result.stderr.lower():
                raise RuntimeError('owned volume lookup failed')
            continue
        if json.loads(result.stdout)[0]['Labels'].get(LABEL) != OWNER:
            raise RuntimeError('owned volume identity failed')
        docker('volume', 'rm', name)
        if docker('volume', 'inspect', name, check=False).returncode == 0:
            raise RuntimeError('owned volume remains')


def exercise():
    db, app = PREFIX + '-db', PREFIX + '-app'
    password = secrets.token_hex(24)
    pod_uid = str(uuid.uuid4())
    db_env = {'MYSQL_ROOT_PASSWORD': secrets.token_hex(24), 'MYSQL_DATABASE': 'grimmory',
              'MYSQL_USER': 'grimmory', 'MYSQL_PASSWORD': password, 'PUID': '568', 'PGID': '568', 'TZ': 'UTC'}
    app_env = {'DATABASE_URL': 'jdbc:mariadb://127.0.0.1:3306/grimmory', 'DATABASE_USERNAME': 'grimmory',
               'DATABASE_PASSWORD': password, 'USER_ID': '568', 'GROUP_ID': '568', 'FORCE_DISABLE_OIDC': 'true',
               'KOPIUR_POD_UID': pod_uid, 'GRIMMORY_BOOKDROP': '/books/bookdrop',
               'JAVA_TOOL_OPTIONS': '-Xmx384m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 --enable-preview'}
    runtime_volume, app_volume, books_volume = volume('runtime'), volume('app'), volume('books')
    db_volume = volume('db')
    create(db, MARIA, 'none', db_env, ('-v', db_volume + ':/config', '-v', runtime_volume + ':/fixture-runtime',
                                     '-v', runtime_volume + ':/runtime-escrow:ro'))
    docker('start', db)

    def sql(statement, check=True):
        return docker('exec', db, 'bash', '-c',
                      'MYSQL_PWD="$MYSQL_PASSWORD" mariadb --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" --batch --skip-column-names -e "$1"',
                      'fence-sql', statement, check=check)

    wait(lambda: sql('SELECT 1', False).returncode == 0)
    create(app, IMAGE, 'container:' + db, app_env,
           ('-v', app_volume + ':/app/data', '-v', books_volume + ':/books',
            '--tmpfs=/bookdrop:rw,uid=568,gid=568,size=16m', '-v', runtime_volume + ':/runtime-escrow',
            '-v', db_volume + ':/database-state:ro',
            '--entrypoint=/bin/sh'), ('/fence.sh', 'supervise', *NATIVE))
    docker('cp', str(HOLDER), app + ':/fence.sh')
    if PRODUCTION_HOLDER.exists():
        for name in ('runtime.sh', 'books.sh'):
            docker('cp', str(HELPERS / name), app + ':/' + name)
        for name in ('backup.sh', 'runtime.sh'):
            docker('cp', str(HELPERS / name), db + ':/' + name)
    docker('start', app)

    def execute(*args, check=True):
        return docker('exec', app, *args, check=check)

    def fence(*args, check=True):
        return execute('sh', '/fence.sh', *args, check=check)

    def health():
        return execute('wget', '-q', '-T', '2', '-O', '/dev/null', 'http://127.0.0.1:6060/api/v1/healthcheck', check=False).returncode == 0

    def setup():
        response = execute('wget', '-q', '-T', '2', '-O', '-', 'http://127.0.0.1:6060/api/v1/setup/status')
        return json.loads(response.stdout)['data'] is False

    wait(health)
    if not setup():
        raise RuntimeError('native database-backed setup gate failed')
    process = fence('process').stdout.strip()
    if process == '1':
        raise RuntimeError('supervised native process was not separate')
    command_digest = execute('sh', '-c', 'sha256sum /proc/"$1"/cmdline', 'native-command', process).stdout.split()[0]
    environment_digest = execute('sh', '-c', 'su-exec 568:568 cat /proc/"$1"/environ | sha256sum', 'native-env', process).stdout.split()[0]
    execute('sh', '-c', 'mkdir -p /books/bookdrop; printf fixture >/books/bookdrop/fence-fixture; printf config >/app/data/fence-fixture')
    file_digest = execute('sh', '-c', 'sha256sum /books/bookdrop/fence-fixture /app/data/fence-fixture').stdout
    sql("CREATE TABLE grimmory.fence_fixture(id INT PRIMARY KEY, body VARBINARY(64)) ENGINE=InnoDB; INSERT INTO grimmory.fence_fixture VALUES(1,UNHEX('0001feff'));")
    native_sql = sql('SELECT id,HEX(body) FROM grimmory.fence_fixture').stdout
    info = inspect(app)
    db_info = inspect(db)
    if db_info['HostConfig']['NetworkMode'] != 'none' or info['HostConfig']['NetworkMode'] != 'container:' + db_info['Id']:
        raise RuntimeError('disconnected native network gate failed')
    for item in (info, db_info):
        if item['HostConfig'].get('PortBindings') or item['HostConfig']['LogConfig']['Type'] != 'none':
            raise RuntimeError('private native boundary failed')
        added = sorted(value.removeprefix('CAP_') for value in item['HostConfig']['CapAdd'])
        dropped = [value.removeprefix('CAP_') for value in item['HostConfig']['CapDrop']]
        if added != sorted(['CHOWN', 'SETUID', 'SETGID', 'FOWNER', 'DAC_OVERRIDE']) or dropped != ['ALL']:
            raise RuntimeError('production capability parity failed')
    if fence('acquire', str(int(time.time()) + 3600), '1', check=False).returncode == 0:
        raise RuntimeError('oversized hold accepted')
    if execute('su-exec', '568:568', 'sh', '-c', 'printf unsafe >/runtime-escrow/fence/request', check=False).returncode == 0:
        raise RuntimeError('native UID changed root fence state')

    results = []
    for release in (True, False):
        deadline = int(time.time()) + (120 if PRODUCTION_HOLDER.exists() else 18)
        evidence = fence('acquire', str(deadline), '8').stdout.strip()
        if evidence.split()[0] != pod_uid or len(evidence.split()) != 5:
            raise RuntimeError('native evidence identity mismatch')
        fence('check', evidence, '1')
        if health():
            raise RuntimeError('native Java still served requests while fenced')
        fence('liveness')
        if PRODUCTION_HOLDER.exists():
            fence('check', evidence, '60')
            execute('sh', '/runtime.sh', 'capture')
            docker('exec', db, 's6-setuidgid', 'abc', 'bash', '/backup.sh')
            docker('exec', '-e', 'GRIMMORY_DB_BUNDLE=/database-state/kopiur', app, 'sh', '/runtime.sh', 'verify')
            fence('check', evidence, '1')
        if execute('sh', '-c', 'su-exec 568:568 cat /proc/"$1"/environ | sha256sum', 'native-env', process).stdout.split()[0] != environment_digest:
            raise RuntimeError('native runtime changed under fence')
        if execute('sh', '-c', 'sha256sum /proc/"$1"/cmdline', 'native-command', process).stdout.split()[0] != command_digest:
            raise RuntimeError('native argv changed under fence')
        if execute('sh', '-c', 'sha256sum /books/bookdrop/fence-fixture /app/data/fence-fixture').stdout != file_digest:
            raise RuntimeError('native files changed under fence')
        if sql('SELECT id,HEX(body) FROM grimmory.fence_fixture').stdout != native_sql:
            raise RuntimeError('native SQL changed under fence')
        if fence('check', evidence.replace(pod_uid, str(uuid.uuid4())), '1', check=False).returncode == 0:
            raise RuntimeError('stale Pod identity accepted')
        if fence('acquire', str(deadline), '1', check=False).returncode == 0:
            raise RuntimeError('concurrent owner accepted')
        if release:
            fence('release', evidence)
        else:
            while time.time() < deadline - 1:
                fence('check', evidence, '1')
                time.sleep(0.25)
        wait(health, 20)
        if fence('status', check=False).returncode == 0 or fence('check', evidence, '1', check=False).returncode == 0:
            raise RuntimeError('expired evidence accepted')
        if fence('process').stdout.strip() != process or not setup():
            raise RuntimeError('same native Java/database did not resume')
        if execute('sh', '-c', 'sha256sum /books/bookdrop/fence-fixture /app/data/fence-fixture').stdout != file_digest or sql('SELECT id,HEX(body) FROM grimmory.fence_fixture').stdout != native_sql:
            raise RuntimeError('native post-resume equality failed')
        results.append({'explicit_release': release, 'whole_native_group_stopped': True, 'same_java_resumed': True,
                        'runtime_config_books_sql_equal': True, 'database_backed_setup': False, 'http_status': 200})
    replacement_deadline = int(time.time()) + 25
    old_evidence = fence('acquire', str(replacement_deadline), '12').stdout.strip()
    old_birth = execute('sh', '-c', 'cat /runtime-escrow/fence/child').stdout
    docker('kill', '--signal=TERM', app)
    wait(lambda: not inspect(app)['State']['Running'], 15)
    retained = docker('exec', db, 'cat', '/fixture-runtime/fence/request').stdout.strip()
    if retained != old_evidence:
        raise RuntimeError('termination removed admitted hold')
    # Simulate a replaced kernel monotonic epoch on the isolated retained volume.
    fields = retained.split()
    fields[3] = '1'
    fields[4] = str(uuid.uuid4())
    replacement_evidence = ' '.join(fields)
    docker('exec', db, 'sh', '-c', 'umask 077; printf "%s\\n" "$1" >/fixture-runtime/fence/request',
           'changed-boot-fixture', replacement_evidence)
    docker('start', app)
    wait(lambda: execute('test', '-d', '/runtime-escrow/fence', check=False).returncode == 0, 5)
    while time.time() < replacement_deadline - 1:
        if fence('check', old_evidence, '1', check=False).returncode == 0:
            raise RuntimeError('replacement accepted prior native identity')
        no_java = execute('sh', '-c', 'for f in /proc/[0-9]*/comm; do [ "$(cat "$f" 2>/dev/null)" != java ] || exit 1; done', check=False)
        if no_java.returncode != 0:
            raise RuntimeError('replacement started Java before original expiry')
        if execute('cat', '/runtime-escrow/fence/request').stdout.strip() != replacement_evidence:
            raise RuntimeError('replacement changed absolute hold')
        time.sleep(0.25)
    wait(health, 180)
    if execute('cat', '/runtime-escrow/fence/child').stdout == old_birth or not setup():
        raise RuntimeError('native replacement recovery not proved')
    if execute('sh', '-c', 'sha256sum /books/bookdrop/fence-fixture /app/data/fence-fixture').stdout != file_digest or sql('SELECT id,HEX(body) FROM grimmory.fence_fixture').stdout != native_sql:
        raise RuntimeError('replacement data equality failed')
    return {'image': IMAGE, 'native_fences': results, 'runtime_sha256': environment_digest,
            'replacement_preserves_absolute_expiry': True, 'old_process_evidence_rejected': True,
            'replacement_http_status': 200, 'replacement_data_equal': True,
            'argv_sha256': command_digest, 'pod_identity_checked': True, 'root_state_protected': True,
            'capabilities_unchanged': True, 'production_capture_adapter_exercised': PRODUCTION_HOLDER.exists(),
            'production_shared_books_acceptance': False}


def main():
    docker('info')
    for image in (IMAGE, MARIA):
        docker('pull', '--platform=linux/amd64', image, timeout=600)
    config = json.loads(docker('image', 'inspect', IMAGE).stdout)[0]['Config']
    if config['Entrypoint'] != NATIVE[:1] or config['Cmd'] != NATIVE[1:]:
        raise RuntimeError('exact native entrypoint changed')
    try:
        receipt = exercise()
    finally:
        owned_cleanup()
    receipt['owned_cleanup_verified'] = True
    print(json.dumps(receipt, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Numeric line only. Never propagate private Docker/runtime values.
        tb = error.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        line = tb.tb_lineno if tb is not None else 0
        raise SystemExit('Grimmory native fence prerequisite failed at line ' + str(line)) from None
