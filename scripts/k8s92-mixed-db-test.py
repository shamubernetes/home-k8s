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
# Keep the workflow-owned REDIS constant name, but qualify the actual shared
# Dragonfly engine/digest rather than claiming Redis proves its serialization.
REDIS = 'ghcr.io/dragonflydb/dragonfly:v2.0.0@sha256:7426fdb31ddcf7bd9499b4205f36ebaa83b26149ba1609a0d5f8f474b3631233'
# ARC DinD blocks io_uring. Use the engine's supported epoll mode for the
# disposable fixture without granting privileges or changing production args.
# Dragonfly v2 requires 256 MiB per proactor. Match production's two-thread
# 512 MiB setting rather than a fixture value that the engine refuses to boot.
REDIS_ARGS = ('--force_epoll', '--proactor_threads=2', '--maxmemory=512Mi', '--cluster_mode=emulated',
              '--lock_on_hashtags', '--default_lua_flags=allow-undeclared-keys')
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


def ta_fixture_documents():
    """Index-specific v0.5.12 documents, not generic dynamically mapped fields.

    Sources under https://github.com/tubearchivist/tubearchivist/tree/v0.5.12/backend:
    appsettings/index_mapping.json; channel/src/index.py; video/src/index.py,
    comments.py and subtitle.py; download/src/queue.py; playlist/src/index.py.
    ta_startup migrations require video.channel.channel_tabs and all four stats.
    Config remains the real appsettings document created by normal startup.
    """
    channel_id = 'UC0000000000000000000000'
    video_id = 'K8S92vid001'
    download_id = 'K8S92dl0001'
    playlist_id = 'PL00000000000000000000000000000000'
    timestamp = 1750000000
    text = 'Synthetic recovery fixture 雪'
    artwork = 'https://example.invalid/fixture.jpg'
    channel = {
        'channel_id': channel_id, 'channel_name': text, 'channel_active': True,
        'channel_description': text, 'channel_last_refresh': timestamp,
        'channel_subs': 42, 'channel_subscribed': False, 'channel_tags': ['fixture'],
        'channel_tabs': ['videos', 'streams', 'shorts'],
        'channel_thumb_url': artwork, 'channel_banner_url': artwork,
        'channel_tvart_url': artwork,
    }
    video = {
        'youtube_id': video_id, 'title': text, 'description': text, 'active': True,
        'channel': channel.copy(), 'date_downloaded': timestamp,
        'published': timestamp, 'vid_last_refresh': timestamp,
        'vid_thumb_url': artwork, 'vid_type': 'videos', 'category': ['fixture'],
        'tags': ['fixture'], 'media_url': channel_id + '/' + video_id + '.mp4',
        'media_size': 42, 'streams': [], 'playlist': [playlist_id],
        'player': {'duration': 42, 'duration_str': '42s', 'watched': False},
        'stats': {'view_count': 42, 'like_count': 3, 'dislike_count': 0, 'average_rating': 4.5},
        'comment_count': 1,
    }
    download = {
        'youtube_id': download_id, 'title': text, 'channel_id': channel_id,
        'channel_name': text, 'channel_indexed': True, 'duration': '42s',
        'published': timestamp, 'timestamp': timestamp, 'vid_thumb_url': artwork,
        'vid_type': 'videos', 'status': 'ignore', 'auto_start': False,
    }
    playlist = {
        'playlist_id': playlist_id, 'playlist_name': text, 'playlist_active': True,
        'playlist_channel': text, 'playlist_channel_id': channel_id,
        'playlist_description': text, 'playlist_thumbnail': artwork,
        'playlist_last_refresh': timestamp, 'playlist_type': 'regular',
        'playlist_subscribed': False, 'playlist_sort_order': 'top',
        'playlist_entries': [{'youtube_id': video_id, 'title': text,
                              'uploader': text, 'idx': 0, 'downloaded': True}],
    }
    subtitle_id = video_id + '-en-1'
    subtitle = {
        'subtitle_fragment_id': subtitle_id, 'youtube_id': video_id,
        'subtitle_channel': text, 'subtitle_channel_id': channel_id,
        'subtitle_lang': 'en', 'subtitle_last_refresh': timestamp,
        'subtitle_source': 'user', 'title': text, 'subtitle_index': 1,
        'subtitle_line': text, 'subtitle_start': '00:00:00.000',
        'subtitle_end': '00:00:05.000',
    }
    comment = {
        'youtube_id': video_id, 'comment_channel_id': channel_id,
        'comment_last_refresh': timestamp,
        'comment_comments': [{
            'comment_author': text, 'comment_author_id': channel_id,
            'comment_author_is_uploader': True, 'comment_author_thumbnail': artwork,
            'comment_id': 'synthetic-comment-1', 'comment_is_favorited': False,
            'comment_likecount': 0, 'comment_parent': 'root', 'comment_text': text,
            'comment_time_text': '2025-06-15', 'comment_timestamp': timestamp,
        }],
    }
    return [
        ('ta_channel', channel_id, channel), ('ta_video', video_id, video),
        ('ta_download', download_id, download), ('ta_playlist', playlist_id, playlist),
        ('ta_subtitle', subtitle_id, subtitle), ('ta_comment', video_id, comment),
    ]


def ta_fixture_script(seed=False):
    """Check real Django tables and fixture contents in the pinned running app."""
    return """import os, sys, json
from pathlib import Path
sys.path.insert(0, '/')
sys.path.insert(0, '/app')
import contract
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.contrib.auth import get_user_model
from django.conf import settings
assert settings.TA_VERSION == 'v0.5.12'
account = get_user_model()
assert account._meta.label == 'user.Account'
assert account._meta.db_table == 'user_account'
counts = contract.sqlite_inventory(Path('/cache/db.sqlite3'))
assert 'auth_user' not in counts
assert all(counts[name] > 0 for name in ('user_account', 'django_migrations', 'task_customperiodictask'))
fixtures = json.loads(%r)
assert {index for index, _, _ in fixtures} == set(contract.INDICES) - {'ta_config'}
for index, doc_id, document in fixtures:
    path = index + '/_doc/' + doc_id
    if %r:
        contract.request(path + '?refresh=true', 'PUT', document)
    assert contract.request(path)['_source'] == document, 'fixture document differs'
assert contract.request('ta_config/_doc/appsettings')['found']
""" % (json.dumps(ta_fixture_documents()), seed)


def ta_contract(container, operation, directory, isolated=False, **kwargs):
    # Call the same entrypoint, but retain a traceback in the runner's PRIVATE
    # failure.log. Production /contract.py still suppresses exception details.
    code = "import sys; sys.path.insert(0, '/'); import contract; contract.main()"
    flags = ('-e', 'K8S92_ISOLATED_RESTORE=YES') if isolated else ()
    return docker('exec', *flags, container, 'python', '-c', code, operation, directory, **kwargs)


def ta_failure_markers(text):
    """Only fixed labels leave private logs. Never echo arbitrary error text."""
    markers = {
        'missing-django-state-tables': 'missing Django state tables',
        'sqlite-integrity': 'SQLite integrity failed',
        'fixture-content-mismatch': 'fixture document differs',
        'es-http-error': 'Elasticsearch HTTP ',
        'native-export-http-error': 'native exporter HTTP ',
        'es-version-mismatch': 'ES version changed; requalify restore',
        'index-coverage-mismatch': 'application index set changed; requalify coverage',
        'native-zip-missing': 'native ZIP missing',
        'export-count-mismatch': 'index count changed or incomplete export',
        'restore-content-mismatch': 'restored Elasticsearch content differs',
        'contract-deadline': 'capture/restore deadline exceeded',
        'es-script-error': 'script_exception',
        'es-null-pointer': 'null_pointer_exception',
        'startup-migration-failed': 'failed to run ',
        'sqlite-inventory-frame': 'in sqlite_inventory',
        'assertion-error': 'AssertionError',
        'permission-error': 'PermissionError',
        'connection-error': 'ConnectionError',
        'dragonfly-invalid-flags': 'Unknown command line flag',
        'dragonfly-flag-validation': 'Illegal value',
        'dragonfly-io-uring-permission': 'Operation not permitted',
        'dragonfly-io-uring-init': 'io_uring',
        'dragonfly-locked-memory': 'Cannot lock memory',
        'dragonfly-minimum-thread-memory': 'are required. Exiting...',
        'redis-instance-identity': 'Redis server has no supported instance identity',
        'redis-key-mutated': 'Redis key changed during capture',
        'redis-key-set-mutated': 'Redis key set changed during capture',
        'redis-value-mutated': 'Redis value changed during capture',
        'redis-expiry-mutated': 'Redis expiry changed during capture',
        'redis-native-response-error': 'ResponseError',
        'native-task-registration': "KeyError: 'download_pending'",
        'native-api-type-error': 'TypeError:',
        'native-api-attribute-error': 'AttributeError:',
        'native-startup-source-drift': 'unqualified native recovery startup source',
        'native-recovery-conflict': 'Redis recovery record conflict',
        'native-held-startup-failure': 'held startup failed:',
        'native-recovery-unowned': 'unowned or conflicting Redis target',
        'native-recovery-key-set': 'Redis recovery target key set differs',
        'native-contract-import': 'ModuleNotFoundError:',
    }
    return [label for label, marker in markers.items() if marker in text] or ['unclassified']


def ta_failure_diagnostics():
    """Bounded synthetic-only diagnostics; raw content stays in private files."""
    sources = []
    failure = SCRATCH / 'failure.log'
    if failure.exists():
        with failure.open('rb') as stream:
            stream.seek(max(0, failure.stat().st_size - 65536))
            sources.append(('command', stream.read(65536).decode(errors='replace')))
    # Exact names registered by this disposable run, never a production target.
    for label in ('ta-source', 'ta-restore-tool', 'es-source', 'es-restored',
                  'redis-source', 'redis-restored'):
        container = PREFIX + '-' + label
        if container not in CONTAINERS:
            continue
        logs = docker('logs', '--tail', '100', container, ok=False, timeout=15)
        sources.append((label, (logs.stdout + logs.stderr)[-65536:]))
        if label == 'ta-restore-tool':
            code = ("from pathlib import Path; p=Path('/cache/lane-boot.log'); "
                    "f=p.open('rb'); f.seek(max(0,p.stat().st_size-65536)); "
                    "print(f.read(65536).decode(errors='replace'))")
            boot = docker('exec', container, 'python', '-c', code, ok=False, timeout=15)
            sources.append(('restored-boot', (boot.stdout + boot.stderr)[-65536:]))
    summary = {}
    for label, text in sources:
        (SCRATCH / ('ta-diagnostic-' + label + '.log')).write_text(text)
        summary[label] = ta_failure_markers(text)
        # Frame numbers are safe, unlike traceback source/value text. They map
        # precisely to the synthetic inline script on this reviewed candidate.
        if label == 'command':
            import re
            summary['inline-python-lines'] = [int(line) for line in re.findall(r'File "<(?:stdin|string)>", line (\d+)', text)]
    EVIDENCE['tubearchivist_failure'] = summary
    print(json.dumps({'synthetic_ta_failure': summary,
                      'phase': EVIDENCE.get('tubearchivist_phase', 'unknown')}), flush=True)


def ta():
    EVIDENCE['tubearchivist_phase'] = 'source-startup'
    net = network('ta')
    source = start('es-source', ES, net, {'discovery.type': 'single-node', 'xpack.security.enabled': 'false', 'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2', 'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false', 'cluster.name': 'k8s92-ta-source', 'path.repo': '/usr/share/elasticsearch/data/snapshot'}, extra=('--network-alias', 'es', '--memory=1600m'))
    wait(lambda: esready(source), 300)
    redis = start('redis-source', REDIS, net, args=REDIS_ARGS, extra=('--network-alias', 'source-redis'))
    env_redis_source = 'redis://source-redis:6379/15'
    env = {'ES_URL': 'http://es:9200', 'REDIS_CON': 'redis://redis:6379/15', 'TA_HOST': 'http://localhost:8000', 'TA_USERNAME': 'fixture', 'TA_PASSWORD': secrets.token_hex(24), 'ELASTIC_PASSWORD': secrets.token_hex(24), 'TA_PORT': '8000', 'TA_BACKEND_PORT': '8080', 'HOST_UID': '1000', 'HOST_GID': '1000', 'TZ': 'UTC'}
    env['REDIS_CON'] = env_redis_source
    app = start('ta-source', TA, net, env)
    wait(lambda: docker('exec', app, 'curl', '-fsS', '-H', 'Host: localhost:8000', 'http://localhost:8000/api/health/', ok=False).returncode == 0, 300)
    docker('cp', str(ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py'), app + ':/contract.py')
    EVIDENCE['tubearchivist_phase'] = 'seed-and-check-real-schema'
    docker('exec', '-i', app, 'python', '-', data=ta_fixture_script(seed=True))
    # Freeze ONLY this disposable source's native worker/beat processes before
    # producing executable broker payloads. The production writer-fence gate
    # belongs to the downstream coherence lane, not this synthetic fixture.
    freeze = """import os,signal
from pathlib import Path
found = 0
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try: args = (entry / 'cmdline').read_bytes().split(b'\\x00')
    except (FileNotFoundError, PermissionError): continue
    if args[0].startswith(b'celery') or any(Path(a.decode(errors='replace')).name == 'celery' for a in args[:2]):
        os.kill(int(entry.name), signal.SIGSTOP)
        found += 1
assert found >= 1
"""
    docker('exec', '-i', app, 'python', '-', data=freeze)
    # Exercise shipped native writers and actual Kombu serialization, including
    # a reserved message left by a producer-process crash. Nothing is executed.
    seed_redis = """import sys
sys.path.insert(0, '/')
import contract
sys.path.insert(0, '/app')
import os,json,django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from common.src.ta_redis import RedisQueue,TaskRedis
from task.src.task_manager import TaskManager
from task.celery import app as celery_app
# Celery autodiscovery is deferred in a producer-only Python process. Import
# the actual shipped task module, as worker initialization does, before lookup.
from task.tasks import download_pending
from django.contrib.sessions.backends.db import SessionStore
RedisQueue('download:video').add_list(['dlfixture01','dlfixture02'])
RedisQueue('reindex:ta_video').add_list(['vidfixture1','vidfixture2'])
task = celery_app.tasks['download_pending']
task.push_request(id='11111111-1111-4111-8111-111111111111')
TaskManager().init(task)
TaskRedis().set_command(task.request.id, 'STOP')
task.send_progress(['Recovery fixture interrupted'],progress=0.5)
task.pop_request()
TaskRedis().set_key('22222222-2222-4222-8222-222222222222',
    {'task_id':'22222222-2222-4222-8222-222222222222','name':'download_pending','status':'SUCCESS'},expire=True)
celery_app.send_task('download_pending',task_id='33333333-3333-4333-8333-333333333333',kwargs={'auto_only':False})
celery_app.send_task('download_pending',task_id='22222222-2222-4222-8222-222222222222',kwargs={'auto_only':False})
connection = celery_app.connection_for_read()
channel = connection.channel()
message = channel.basic_get(queue='celery',no_ack=False)
assert message is not None
session = SessionStore()
session['recovery_fixture'] = 'native-db-session'
session.set_expiry(3600)
session.save()
from pathlib import Path
Path('/cache/native-session-id').write_text(session.session_key)
r = contract.redis_client()
assert r.hlen('unacked') >= 1
r.set('celery-task-meta-invalid', b'not-json')
r.hset('celery-task-meta-wrongtype', mapping={'opaque': 'preserved'})
r.set('fixture:expired',b'original-retained',px=60000)
r.set(b'fixture:binary', b'\\x00\\xffprivate')
r.rpush('fixture:jobs', b'first', b'\\x00second')
r.hset('fixture:progress', mapping={'cursor': b'\\x00\\xff', 'done': '3'})
r.sadd('fixture:set', b'\\x00', b'member')
r.zadd('fixture:scores', {b'member': 1.5})
r.xadd('fixture:stream', {'body': b'\\x00\\xff'})
r.set('fixture:session', b'private-session', px=3600000)
sys.stdout.flush()
# Do not let Kombu channel.close() requeue the reserved message. Qualify its
# real native unacked records after an abrupt process exit instead.
os._exit(0)
"""
    docker('exec', '-i', app, 'python', '-', data=seed_redis)
    EVIDENCE['tubearchivist_phase'] = 'capture'
    ta_contract(app, 'capture', '/cache/kopiur/current', timeout=660)
    docker('cp', app + ':/cache/kopiur/current', str(SCRATCH / 'tubearchivist'))
    # Preserve the app cache too; replace its raw SQLite with the online artifact below.
    docker('cp', app + ':/cache', str(SCRATCH / 'ta-cache'))
    remove(app)
    remove(source)
    remove(redis)
    EVIDENCE['tubearchivist_phase'] = 'fresh-restore-services'
    # Brand new server and fresh Redis. No source container or data path is reused.
    restored = start('es-restored', ES, net, {'discovery.type': 'single-node', 'xpack.security.enabled': 'false', 'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2', 'xpack.ml.enabled': 'false', 'ingest.geoip.downloader.enabled': 'false', 'cluster.name': 'k8s92-ta-restore', 'path.repo': '/usr/share/elasticsearch/data/snapshot'}, extra=('--network-alias', 'es', '--memory=1600m'))
    wait(lambda: esready(restored), 300)
    freshredis = start('redis-restored', REDIS, net, args=REDIS_ARGS, extra=('--network-alias', 'redis'))
    env = dict(env, REDIS_CON='redis://redis:6379/15')
    tool = start('ta-restore-tool', TA, net, env, args=('infinity',), extra=('--entrypoint', 'sleep'))
    docker('cp', str(ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py'), tool + ':/contract.py')
    docker('cp', str(ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py'), tool + ':/backup.py')
    docker('cp', str(ROOT / 'kubernetes/apps/media/tubearchivist/app/recovery.py'), tool + ':/recovery.py')
    docker('cp', str(SCRATCH / 'tubearchivist'), tool + ':/bundle')
    assert docker('exec', tool, 'python', '-c',
                  "import redis; assert redis.Redis.from_url('redis://redis:6379/15').dbsize() == 0").returncode == 0
    EVIDENCE['tubearchivist_phase'] = 'reject-unowned-target'
    ta_contract(tool, 'verify', '/bundle')
    restarted_source = start('redis-source-restarted', REDIS, net, args=REDIS_ARGS,
                             extra=('--network-alias', 'source-redis'))
    reject_restart = """import sys
sys.path.insert(0,'/')
import contract
from pathlib import Path
r=contract.redis_client()
assert r.dbsize()==0
assert contract.redis_instance_id(r) != contract.verify(Path('/bundle'))['redis']['source_instance_id']
try: contract.restore_redis(Path('/bundle'))
except RuntimeError as exc: assert str(exc)=='refuse source Redis endpoint even after restart'
else: raise AssertionError('restarted source accepted')
assert r.dbsize()==0
"""
    docker('exec', '-i', '-e', 'K8S92_ISOLATED_RESTORE=YES', '-e', 'REDIS_CON=redis://source-redis:6379/15',
           tool, 'python', '-', data=reject_restart)
    remove(restarted_source)
    # Unowned populated target is rejected unchanged. Only a target atomically
    # claimed for this exact bundle may resume after a killed restore process.
    unowned = """import sys
sys.path.insert(0,'/')
import contract
from pathlib import Path
r=contract.redis_client()
r.set('unowned',b'preserve')
before=contract.redis_inventory(r)
try: contract.restore_redis(Path('/bundle'))
except RuntimeError: pass
else: raise AssertionError('unowned target accepted')
assert contract.redis_inventory(r) == before
# Fixture-only owned sentinel cleanup, no database reset.
assert r.delete('unowned') == 1
"""
    docker('exec', '-i', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '-', data=unowned)
    EVIDENCE['tubearchivist_phase'] = 'crash-partial-owned-restore'
    crash_restore = """import sys,os
sys.path.insert(0,'/')
import contract
from pathlib import Path
r=contract.redis_client()
original=r.eval
count=0
def interrupted(*args,**kwargs):
    global count
    result=original(*args,**kwargs)
    count+=1
    if count == 4: os._exit(73)
    return result
r.eval=interrupted
contract.redis_client=lambda:r
contract.restore_redis(Path('/bundle'))
"""
    assert docker('exec', '-i', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '-',
                  data=crash_restore, ok=False).returncode == 73
    EVIDENCE['tubearchivist_phase'] = 'resume-owned-restore'
    ta_contract(tool, 'restore-redis', '/bundle', isolated=True)
    ta_contract(tool, 'restore-redis', '/bundle', isolated=True)
    check_redis = """import sys,json
from pathlib import Path
sys.path.insert(0, '/')
import contract
expected = json.loads(Path('/bundle/redis.json').read_text())
client=contract.redis_client()
owner=contract.recovery_owner(Path('/bundle'),contract.verify(Path('/bundle')),client)
contract.verify_restored_redis(client,expected,owner)
assert len([key for key in expected if contract.base64.b64decode(key).startswith(b'fixture:')]) == 8
"""
    docker('exec', '-i', tool, 'python', '-', data=check_redis)
    result = ta_contract(tool, 'restore-es', '/bundle', timeout=660).stdout.strip()
    EVIDENCE['tubearchivist_phase'] = 'reject-owned-target-conflicts'
    conflicts = """import sys
sys.path.insert(0,'/')
import contract
from pathlib import Path
r=contract.redis_client()
owner=r.get(contract.RECOVERY_KEY)
original=r.get('fixture:binary')
def rejected():
    before=contract.redis_inventory(r)
    try: contract.restore_redis(Path('/bundle'))
    except RuntimeError: pass
    else: raise AssertionError('conflict accepted')
    contract.compare_redis_inventory(before,contract.redis_inventory(r))
r.set(contract.RECOVERY_KEY,b'wrong-bundle')
rejected()
r.set(contract.RECOVERY_KEY,owner)
r.set('fixture:binary',b'wrong-value')
rejected()
r.set('fixture:binary',original)
r.pexpire('fixture:binary',3600000)
rejected()
r.persist('fixture:binary')
r.set('unexpected-owned-key',b'preserve')
rejected()
assert r.delete('unexpected-owned-key')==1
contract.restore_redis(Path('/bundle'))
"""
    docker('exec', '-i', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '-', data=conflicts)
    assert ta_contract(tool, 'restore-es', '/bundle', ok=False).returncode != 0
    # Boot a working copy. The immutable recovery artifact stays separate.
    cache = SCRATCH / 'ta-cache'
    shutil.copy2(SCRATCH / 'tubearchivist/db.sqlite3', cache / 'db.sqlite3')
    for suffix in ('-wal', '-shm', '-journal'):
        (cache / ('db.sqlite3' + suffix)).unlink(missing_ok=True)
    docker('cp', str(cache) + '/.', tool + ':/cache')
    EVIDENCE['tubearchivist_phase'] = 'restored-held-native-startup'
    # Ordinary run.sh destroys required state and launches invalid work before
    # reconciliation. Run pinned native migrations/startup with no execution.
    docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '/recovery.py',
           '/bundle', '/cache/recovery-hold.json', timeout=660)
    # Crash/restart the app process container while keeping the restored engine.
    docker('restart', tool)
    docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '/recovery.py',
           '/bundle', '/cache/recovery-hold.json', timeout=660)
    EVIDENCE['tubearchivist_phase'] = 'post-startup-fixture-verification'
    docker('exec', '-i', tool, 'python', '-', data=ta_fixture_script())
    docker('exec', '-i', tool, 'python', '-', data=check_redis)
    native_check = """import sys,os,json,hashlib
from pathlib import Path
sys.path.insert(0,'/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from common.src.ta_redis import RedisQueue,TaskRedis,RedisArchivist
from task.src.task_config import TASK_CONFIG
from django.contrib.sessions.backends.db import SessionStore
assert RedisQueue('download:video').get_all() == ['dlfixture01','dlfixture02']
assert RedisQueue('reindex:ta_video').get_all() == ['vidfixture1','vidfixture2']
task=TaskRedis().get_single('11111111-1111-4111-8111-111111111111')
assert task['status']=='PENDING' and task['command']=='STOP'
progress_key='message:'+TASK_CONFIG['download_pending']['group']+':11111111'
progress=RedisArchivist().get_message_dict(progress_key)
assert progress['progress']==0.5 and progress['command']=='STOP'
session=SessionStore(session_key=Path('/cache/native-session-id').read_text())
assert session['recovery_fixture']=='native-db-session'
report=json.loads(Path('/cache/recovery-hold.json').read_text())
assert report['execution']=='HELD' and report['automatic_replay'] is False
values=set(report['records'].values())
assert {'interrupted-requires-reconciliation','terminal-no-replay',
        'invalid-or-unknown-no-replay','progress-preserved','opaque-or-broker-no-replay'} <= values
"""
    docker('exec', '-i', tool, 'python', '-', data=native_check)
    # Let a captured TTL elapse on the Redis clock, then retry. No resurrection.
    expiry_check = """import sys,time,json
from pathlib import Path
sys.path.insert(0,'/')
import contract
r=contract.redis_client()
ttl=r.pttl('fixture:expired')
assert ttl < 60001
if ttl > 0: time.sleep((ttl+20)/1000)
assert not r.exists('fixture:expired')
contract.restore_redis(Path('/bundle'))
assert not r.exists('fixture:expired')
assert any(contract.base64.b64decode(k)==b'fixture:expired'
           for k in json.loads(Path('/bundle/redis.json').read_text()))
"""
    docker('exec', '-i', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '-', data=expiry_check)
    manifest = json.loads((SCRATCH / 'tubearchivist/manifest.json').read_text())
    EVIDENCE['tubearchivist'] = {'native_export': 'pass', 'documents': {k: v['count'] for k, v in manifest['documents'].items()}, 'sqlite_tables': manifest['sqlite_tables'], 'source_removed_before_restore': True, 'es_restore': result, 'redis_initial_dbsize': 0, 'restore_rejects_unowned_nonempty_target': 'pass', 'restored_native_startup_under_hold': 'pass'}
    EVIDENCE['tubearchivist'].update({'django_user_table': 'user_account', 'fixture_documents_after_held_startup': 'pass'})
    EVIDENCE['tubearchivist'].update({'redis_native_types_and_binary_roundtrip': 'pass',
                                   'redis_engine': REDIS,
                                   'redis_absolute_expiry_preserved': 'pass',
                                   'redis_repeat_restore_preserves_target': 'pass'})
    EVIDENCE['tubearchivist'].update({'killed_partial_restore_resumes': 'pass',
                                   'restarted_source_rejected_without_writes': 'pass',
                                   'owned_bundle_value_expiry_extra_key_conflicts_rejected': 'pass',
                                   'native_queues_tasks_commands_progress_and_db_session': 'pass',
                                   'native_kombu_queued_and_reserved_payloads_preserved': 'pass',
                                   'app_crash_repeated_startup_preserves_state': 'pass',
                                   'invalid_terminal_and_ambiguous_work_not_replayed': 'pass',
                                   'expired_state_archived_not_resurrected': 'pass',
                                   'automatic_release': False})
    EVIDENCE['tubearchivist_phase'] = 'complete'
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
            # The trusted ARC dispatcher invokes this entrypoint. Run the focused
            # schema/fixture regressions before starting native TA containers.
            import sys
            subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                            '-s', str(ROOT / 'scripts/tests'),
                            '-p', 'test_k8s92_tubearchivist.py', '-v'], check=True)
            try:
                ta()
            except Exception:
                try:
                    ta_failure_diagnostics()
                except Exception:
                    print('Synthetic TA diagnostics unavailable; private logs retained', flush=True)
                raise
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
