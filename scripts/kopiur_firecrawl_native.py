"""Deployed Firecrawl NUQ queue fixture, not the multi-store service.

Use the deployed ConfigMap SQL, not upstream pg_cron/server tuning. The live
postgres17 engine does not preload pg_cron. Scheduler and multi-store recovery
remain unqualified, rather than installing an undeployed scheduler in a fixture.
"""
import hashlib
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'docker.io/winkkgmbh/firecrawl@sha256:b9ccf2b29722257c154b20e6e5a233fc56d1181b70c6b9fd763cf55b4f3c8d87'
SCHEMA_SHA256 = 'a9440e8f7f624565389b5076d498e905bdefb85064fbb77ce325efa155f3628b'
DEPLOYED_SCHEMA = ROOT / 'kubernetes/apps/tools/firecrawl/app/nuq-schema-configmap.yaml'
DEPLOYED_SCHEMA_SHA256 = '39ccae62fb1b375511dbcfbafc16f41fa907c25f30885d65d7f06a526890293b'
MODULE_SHA256 = 'e5d4fd245f0635a9fd99729465b7e9633548e4d9f22b521b2a5a19f976f6b066'
JOB = '0e1d851d-1b07-400a-91bf-aac7010970dd'
BACKLOG = '0e1d851d-1b07-400a-91bf-aac7010970de'
QUEUED = '0e1d851d-1b07-400a-91bf-aac7010970df'
BOOT = "const fs=require('fs'),crypto=require('crypto'); const p='/app/dist/src/services/worker/nuq.js'; if(crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex')!==" + repr(MODULE_SHA256) + ")throw Error('Pinned NUQ module differs'); const cfg=JSON.parse(fs.readFileSync('/config/fixture.json')); const q=require(p); const uuid=require('module').createRequire(p)('uuid'); const owner=uuid.validate(cfg.owner)?cfg.owner:uuid.v5(cfg.owner,'0f38e00e-d7ee-4b77-8a7a-a787a3537ca2'); "
SEED = BOOT + "(async()=>{await q.scrapeQueue.addJob(cfg.job,{url:'http://fixture.invalid/recovery',mode:'single_urls'},{ownerId:cfg.owner}); const active=await q.scrapeQueue.getJobToProcess(); if(!active||active.id!==cfg.job)throw Error('NUQ dequeue differs'); if(!await q.scrapeQueue.jobFinish(active.id,active.lock,{title:'Recovery fixture',markdown:'Original native result'}))throw Error('NUQ finish denied'); await q.scrapeQueue.addJob(cfg.backlog,{url:'http://fixture.invalid/backlog',mode:'single_urls'},{ownerId:cfg.owner,backlogged:true}); await q.scrapeQueue.addJob(cfg.queued,{url:'http://fixture.invalid/queued',mode:'single_urls'},{ownerId:cfg.owner}); await q.nuqShutdown();})().catch(()=>{console.error('NUQ fixture seed failed');process.exit(1)});"
VISIBLE = BOOT + "(async()=>{const job=await q.scrapeQueue.getJob(cfg.job); const backlog=await q.scrapeQueue.getJobsFromBacklog([cfg.backlog]); const queued=await q.scrapeQueue.getJob(cfg.queued); if(!job||job.status!=='completed'||job.returnvalue?.title!=='Recovery fixture'||backlog.length!==1||!queued||queued.status!=='queued'||[job,backlog[0],queued].some(x=>x.ownerId!==owner))throw Error('NUQ restored state differs'); console.log('KOPIUR_STATE='+JSON.stringify({job,backlog,queued})); await q.nuqShutdown();})().catch(()=>{console.error('NUQ fixture read failed');process.exit(1)});"


def queue_schema():
    raw = (ROOT / 'scripts/fixtures/firecrawl-nuq.sql').read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
        raise ValueError('Pinned native NUQ schema differs')
    # The checked-in manifest uses one literal SQL block. Fail closed if its
    # structure changes, including a second data key or altered indentation.
    text = DEPLOYED_SCHEMA.read_text()
    marker = 'data:\n  nuq-schema.sql: |\n'
    if text.count(marker) != 1:
        raise ValueError('Unexpected deployed NUQ schema block')
    lines = text.split(marker, 1)[1].splitlines()
    if not lines or any(not line.startswith('    ') for line in lines):
        raise ValueError('Unexpected deployed NUQ schema indentation')
    sql = '\n'.join(line[4:] for line in lines) + '\n'
    if hashlib.sha256(sql.encode()).hexdigest() != DEPLOYED_SCHEMA_SHA256:
        raise ValueError('Pinned deployed NUQ schema differs')
    return sql


def contract():
    scope = runpy.run_path(str(ROOT / 'scripts/kopiur-postgres-drill'))
    native = scope['fixture'].__globals__
    native['CONTRACTS']['firecrawl'] = (IMAGE, ['firecrawl_nuq'], 'FIRECRAWL', 3002, '/config/fixture.json')
    base = native['DockerDrill']

    class FirecrawlDrill(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.initial_api_key = self.api_key

        def counts(self, database, name=None):
            name = name or self.databases[0]
            tables = self.sql(database, "SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname IN ('public','nuq') ORDER BY schemaname,tablename", name).splitlines()
            return {table: int(self.sql(database, 'SELECT count(*) FROM ' + '.'.join('"' + part.replace('"', '""') + '"' for part in table.split('.')), name)) for table in tables}

        def fingerprints(self, database, name):
            return {table: hashlib.sha256(self.sql(database, 'SELECT row_to_json(t)::text FROM ' + '.'.join('"' + part.replace('"', '""') + '"' for part in table.split('.')) + ' t ORDER BY row_to_json(t)::text', name).encode()).hexdigest() for table in self.counts(database, name)}

        def app(self, name, database, config):
            holder = self.start(name, self.image, network='container:' + database,
                                mounts=[(config, '/config', 'rw')], user='568:568',
                                env={'NUQ_DATABASE_URL': 'postgresql://app:' + self.password + '@127.0.0.1:5432/firecrawl_nuq',
                                     'NUQ_WAIT_MODE': 'poll', 'LOGGING_LEVEL': 'error'},
                                entrypoint='node', command=['-e', 'setInterval(()=>{},60000)'])
            if name == 'source-app':
                value = {'job': JOB, 'backlog': BACKLOG, 'queued': QUEUED, 'owner': self.api_key}
                native['run']('docker', 'exec', '-i', holder, 'node', '-e',
                              "require('fs').writeFileSync('/config/fixture.json',require('fs').readFileSync(0),{mode:384})",
                              stdin=json.dumps(value).encode())
                self.sql(database, queue_schema(), database='firecrawl_nuq', user='app')
                self.node(holder, SEED)
            return holder

        def node(self, container, code):
            result = native['run']('docker', 'exec', container, 'node', '-e', code,
                                   check=False, timeout=120)
            if result.returncode:
                # No arbitrary application logs, connection URLs or fixture keys.
                raise RuntimeError('Pinned Firecrawl isolated native module failed')
            return result.stdout.decode()

        def healthy(self, container):
            self.application_state(container)

        def isolated_config(self, raw):
            value = json.loads(raw)
            if len(raw) > 65536 or value.get('job') != JOB or value.get('backlog') != BACKLOG or value.get('queued') != QUEUED or not isinstance(value.get('owner'), str):
                raise ValueError('Unexpected Firecrawl fixture identity')
            return raw

        def application_state(self, container):
            raw = native['run']('docker', 'exec', container, 'cat', '/config/fixture.json').stdout
            self.isolated_config(raw)
            if self.api_key == self.initial_api_key:
                self.api_key = json.loads(raw)['owner']
            if json.loads(raw)['owner'] != self.api_key:
                raise ValueError('Original Firecrawl fixture owner differs')
            lines = [line.removeprefix('KOPIUR_STATE=') for line in self.node(container, VISIBLE).splitlines() if line.startswith('KOPIUR_STATE=')]
            if len(lines) != 1:
                raise ValueError('NUQ visible receipt count differs')
            value = json.loads(lines[0])
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    native['DockerDrill'] = FirecrawlDrill
    return native


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'firecrawl', [IMAGE],
                    {'pg_cron_scheduler_qualified': False, 'production_rabbitmq_redis_qualified': False,
                     'production_scrape_api_playwright_qualified': False})
