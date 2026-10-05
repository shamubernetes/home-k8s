"""Populated native admission/drain/generation fixture for trusted ARC dispatch.

Storage operations below are explicitly labelled representative fixture writes.
They exercise real native schemas and engines, not synthetic API responses.
No source/transport/credential from production is used.
"""
import json
from pathlib import Path
import secrets
import time


def native(r):
    docker, start, remove, wait = (r[name] for name in ('docker', 'start', 'remove', 'wait'))
    root, scratch, evidence = (r[name] for name in ('ROOT', 'SCRATCH', 'EVIDENCE'))
    appdir = root / 'kubernetes/apps/media/tubearchivist/app'
    net = r['network']('coordination')
    esenv = {'discovery.type':'single-node', 'xpack.security.enabled':'false',
             'ES_JAVA_OPTS':'-Xms256m -Xmx256m -XX:ActiveProcessorCount=2',
             'xpack.ml.enabled':'false', 'ingest.geoip.downloader.enabled':'false'}
    esenv['path.repo']='/usr/share/elasticsearch/data/snapshot'
    es = start('coherent-es-source', r['ES'], net, dict(esenv, **{'cluster.name':'k8s92-ta-source'}),
               extra=('--network-alias','source-es','--memory=1600m'))
    wait(lambda: r['esready'](es), 300)
    redis = start('coherent-redis-source', r['REDIS'], net, args=r['REDIS_ARGS'],
                  extra=('--network-alias','source-redis'))
    env = {'ES_URL':'http://source-es:9200', 'REDIS_CON':'redis://source-redis:6379/15',
           'TA_HOST':'http://localhost:8000', 'TA_USERNAME':'fixture', 'TA_PASSWORD':secrets.token_hex(24),
           'ELASTIC_PASSWORD':secrets.token_hex(24), 'TA_PORT':'8000', 'TA_BACKEND_PORT':'8080',
           'HOST_UID':'1000','HOST_GID':'1000','TZ':'UTC'}

    def contracts(container):
        docker('cp', str(appdir), container + ':/backup-contract')
        docker('cp', str(appdir / 'backup.py'), container + ':/contract.py')

    app = start('coherent-ta-source', r['TA'], net, env,
                args=('/backup-contract/supervisor.py',), extra=('--entrypoint','python'), setup=contracts)

    def health(container):
        return docker('exec', container, 'curl', '-fsS', '-H', 'Host: localhost:8000',
                      'http://localhost:8000/api/health/', ok=False).returncode == 0

    try:
        evidence['coordination_phase']='native-startup'
        wait(lambda: health(app), 300)
        docker('exec', '-i', app, 'python', '-', data=r['ta_fixture_script'](seed=True))
        # Real Celery task with shipped native exporter, fully completed before
        # capture. It exercises callbacks, result storage and worker reaping.
        real_task = """import sys,os,time
sys.path.insert(0,'/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django; django.setup()
from task.celery import app
from task.tasks import run_backup
result=run_backup.delay(reason='coordinator-fixture')
until=time.monotonic()+60
while not result.ready():
    assert time.monotonic()<until
    time.sleep(.1)
assert result.successful()
from pathlib import Path
import redis
queued=run_backup.apply_async(kwargs={'reason':'queued-coherent-fixture'},countdown=120)
Path('/cache/queued-native-task-id').write_text(queued.id)
client=redis.Redis.from_url(os.environ['REDIS_CON'])
until=time.monotonic()+20
while client.hlen('unacked')==0:
    assert time.monotonic()<until
    time.sleep(.1)
"""
        evidence['coordination_phase']='native-task-and-reserved-eta'
        docker('exec', '-i', app, 'python', '-', data=real_task, timeout=90)
        # The representative writer is admitted before closing, then commits
        # cross-store intent in stages. Capture must wait, never freeze midway.
        writer = """import sys,os,time,sqlite3
from pathlib import Path
sys.path.insert(0,'/backup-contract'); import coordination as g, backup as b
with g.writer():
    Path('/cache/kopiur-coordination/fixture-writer-active').touch()
    with sqlite3.connect('/cache/db.sqlite3') as db:
        db.execute('CREATE TABLE checkpoint_fixture (id INTEGER PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO checkpoint_fixture VALUES(1,'complete-fixture')")
    time.sleep(.4)
    b.request('ta_config/_doc/checkpoint-fixture?refresh=true','PUT',{'value':'complete-fixture'})
    time.sleep(.4)
    b.redis_client().set('fixture:checkpoint',b'complete-fixture')
    Path('/cache/checkpoint-marker').write_bytes(b'complete-fixture')
    Path('/cache/download/coherent.partial').parent.mkdir(parents=True,exist_ok=True)
    Path('/cache/download/coherent.partial').write_bytes(b'\\x00\\xffpartial-download')
    Path('/youtube/checkpoint-marker').write_bytes(b'complete-fixture')
Path('/cache/kopiur-coordination/fixture-writer-completed').touch()
"""
        docker('exec', app, 'python', '-c', writer, timeout=15)
        # Also prove a long admitted writer causes a failed bounded drain and
        # remains alive. All native services reopen; no force-kill of tasks.
        slow = """import sys,time
from pathlib import Path
sys.path.insert(0,'/backup-contract'); import coordination as g
with g.writer():
    (g.ROOT/'slow-ready').touch(); time.sleep(4)
(g.ROOT/'slow-completed').touch()
"""
        docker('exec', '-d', app, 'python', '-c', slow)
        wait(lambda: docker('exec', app, 'test', '-f', '/cache/kopiur-coordination/slow-ready', ok=False).returncode == 0, 10)
        started = time.monotonic()
        failed = docker('exec', app, 'python', '/backup-contract/coordination.py', 'capture',
                        '/cache/kopiur/failed-drain', '--drain', '.2', '--hold', '1', ok=False, timeout=10)
        assert failed.returncode != 0
        failed_drain_elapsed = time.monotonic() - started
        assert failed_drain_elapsed < 3
        wait(lambda: health(app), 30)
        wait(lambda: docker('exec', app, 'test', '-f', '/cache/kopiur-coordination/slow-completed', ok=False).returncode == 0, 10)
        # Admission closed by the real coordinator, worker/beat warm-drained,
        # collection complete under one exclusive lease, native web resumed.
        evidence['coordination_phase']='closed-generation-capture'
        result = docker('exec', app, 'python', '/backup-contract/coordination.py', 'capture',
                        '/cache/kopiur/generations/qualified', '--drain', '20', '--hold', '30', timeout=65)
        timing = json.loads(result.stdout.strip().splitlines()[-1])
        assert timing['phase'] == 'RESUMED'
        assert timing['resumed_at'] - timing['started_at'] <= 50
        wait(lambda: health(app), 30)
        verify = """import sys,json
from pathlib import Path
sys.path.insert(0,'/backup-contract'); import coordination as g,backup as b
manifest=b.verify(Path('/cache/kopiur/generations/qualified'))
assert manifest['consistency']=='qualified-native-idle-writer-generation'
assert manifest['generation']==g.verify_generation(Path('/cache/kopiur/generations/qualified'))['generation']
s=g.current(); assert s['phase']=='RESUMED' and s['admission']=='OPEN'
assert manifest['filetrees']['cache']['download/coherent.partial']
assert manifest['filetrees']['media']['checkpoint-marker']
"""
        docker('exec', app, 'python', '-c', verify)
        docker('cp', app + ':/cache/kopiur/generations/qualified', str(scratch / 'coherent-generation'))
        # Source changes AFTER resume must not leak into the closed generation.
        docker('exec', app, 'python', '-c', "from pathlib import Path; Path('/youtube/checkpoint-marker').write_bytes(b'later-source')")
        remove(app)
        remove(es)
        remove(redis)
        restored_es = start('coherent-es-restored', r['ES'], net,
                            dict(esenv, **{'cluster.name':'k8s92-ta-restore'}),
                            extra=('--network-alias','restore-es','--memory=1600m'))
        wait(lambda: r['esready'](restored_es), 300)
        restored_redis = start('coherent-redis-restored', r['REDIS'], net, args=r['REDIS_ARGS'],
                               extra=('--network-alias','restore-redis'))
        toolenv = dict(env, ES_URL='http://restore-es:9200', REDIS_CON='redis://restore-redis:6379/15')
        tool = start('coherent-restore-tool', r['TA'], net, toolenv,
                     args=('infinity',), extra=('--entrypoint','sleep'))
        contracts(tool)
        docker('cp', str(scratch / 'coherent-generation'), tool + ':/bundle')
        for operation in ('restore-es', 'restore-redis', 'restore-files'):
            evidence['coordination_phase']='isolated-'+operation
            docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python',
                   '/backup-contract/backup.py', operation, '/bundle', timeout=90)
        check = """import sys,sqlite3,json
from pathlib import Path
sys.path.insert(0,'/backup-contract'); import backup as b
assert b.request('ta_config/_doc/checkpoint-fixture')['_source']=={'value':'complete-fixture'}
assert b.redis_client().get('fixture:checkpoint')==b'complete-fixture'
with sqlite3.connect('/cache/db.sqlite3') as db:
    assert db.execute('SELECT value FROM checkpoint_fixture').fetchone()==('complete-fixture',)
assert Path('/cache/checkpoint-marker').read_bytes()==b'complete-fixture'
assert Path('/youtube/checkpoint-marker').read_bytes()==b'complete-fixture'
assert Path('/cache/download/coherent.partial').read_bytes()==b'\\x00\\xffpartial-download'
queued=Path('/cache/queued-native-task-id').read_text()
assert any(json.loads(message)['headers']['id']==queued
           for message in b.redis_client().lrange('celery',0,-1))
"""
        docker('exec', tool, 'python', '-c', check)
        docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', tool, 'python', '/backup-contract/recovery.py',
               '/bundle', '/cache/recovery-hold.json', timeout=90)
        docker('exec', tool, 'python', '-c', check)
        evidence['coordination'] = {
            'scope':'representative native fixture, not production/NAS/R2 acceptance',
            'idle_worker_and_clean_beat_drain':'pass', 'real_native_task_callbacks_and_results':'pass',
            'native_reserved_eta_returned_to_broker_and_restored_without_execution':'pass',
            'long_admitted_writer_not_killed':'pass', 'failed_drain_elapsed_seconds':failed_drain_elapsed,
            'closed_immutable_cache_partial_media_and_all_datastores':'pass',
            'source_removed_before_isolated_restore':True,
            'sqlite_es_redis_cache_media_equal_point':'pass', 'held_startup_preserves_generation':'pass',
            'later_source_mutation_excluded':'pass', 'timing':timing,
            'admission_closed_seconds':timing['resumed_at']-timing['started_at'],
            'exclusive_hold_seconds':timing['resumed_at']-timing['frozen_at'],
            'automatic_replay':False,
        }
        print(json.dumps({'coordination':evidence['coordination']}), flush=True)
        remove(tool)
        remove(restored_es)
        remove(restored_redis)
    except Exception:
        # Fixed summaries only; credential-bearing native logs remain private.
        summary = {}
        for name in list(r['CONTAINERS']):
            if 'coherent-' in name:
                logs = docker('logs','--tail','100',name,ok=False)
                text = logs.stdout + logs.stderr
                (scratch / (name + '-private.log')).write_text(text)
                summary[name.rsplit('coherent-',1)[-1]] = r['ta_failure_markers'](text)
                for marker in ('path.repo env var not found', 'unqualified native', 'ConnectionError', 'CommandError'):
                    if marker in text:
                        summary[name.rsplit('coherent-',1)[-1]].append(marker)
        evidence['coordination_failure'] = summary
        print(json.dumps({'coordination_failure':summary,'phase':evidence.get('coordination_phase')}), flush=True)
        raise
