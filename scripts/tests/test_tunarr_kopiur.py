#!/usr/bin/env python3
"""Real pinned-image SQLite capture, promotion and network-none application boot.

The stable-only fixture proves active-version schema and dual-path preservation.
A separate fixture bootstraps each exact image independently to test both schemas.
Neither substitutes for independently retrieved NAS/R2 production recovery points.
"""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts/tunarr-kopiur-capture.cjs'
POLICY = REPO / 'kubernetes/apps/media/tunarr/app/kopiur-policy.yaml'
ROOT = '/config/tunarr'


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, REPO / 'scripts' / (name + '.py'))
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


BOOT = load_module('tunarr-kopiur-boot')
RESTORE = load_module('tunarr-kopiur-restore')
IMAGE = BOOT.IMAGES['stable-1.3']


def export_state(name, destination):
    import io
    import tarfile
    result = subprocess.run(['docker', 'exec', name, 'tar', 'cf', '-', '-C', ROOT, '.'], capture_output=True, check=True)
    destination.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(destination, filter='data')

SEED = r"""
const fs=require('fs'),crypto=require('crypto'),{DatabaseSync}=require('node:sqlite');
const root='/config/tunarr/stable-1.3';
const mounts=Object.fromEntries(fs.readFileSync('/proc/self/mountinfo','utf8').trim().split('\n').map(line=>line.split(' ')).filter(row=>['/tmp','/tmp/.cache/pkg','/config/tunarr'].includes(row[4])).map(row=>[row[4],row[5].split(',')]));
for(const p of ['/tmp','/config/tunarr'])if(!mounts[p]?.includes('noexec'))throw Error('fixture data mount must remain noexec: '+p);
if(!mounts['/tmp/.cache/pkg']||mounts['/tmp/.cache/pkg'].includes('noexec'))throw Error('pkg native module cache must allow executable mappings');
console.log('synthetic fixture mount flags: '+JSON.stringify(mounts));
// Only this fresh network-none fixture may expose bounded bootstrap diagnostics.
const bootstrap=require('child_process').spawnSync('/tunarr/tunarr',['server'],{encoding:'utf8',timeout:45000,maxBuffer:1024*1024});
fs.writeFileSync('/tmp/fixture-bootstrap.json',JSON.stringify({status:bootstrap.status,signal:bootstrap.signal,error:bootstrap.error?.code,stdout:bootstrap.stdout?.slice(-8192),stderr:bootstrap.stderr?.slice(-8192)}),{mode:0o600});
const settings=JSON.parse(fs.readFileSync(root+'/settings.json'));
settings.settings.hdhr.autoDiscoveryEnabled=false;
fs.writeFileSync(root+'/settings.json',JSON.stringify(settings));
fs.mkdirSync(root+'/images',{recursive:true});
fs.writeFileSync(root+'/images/fixture.bin','private image fixture');
fs.mkdirSync(root+'/channel-lineups',{recursive:true});
const db=new DatabaseSync(root+'/db.db');
const tx=db.prepare('SELECT uuid FROM transcode_config').get().uuid;
const channelIds=[crypto.randomUUID(),crypto.randomUUID()];
const ids=Array.from({length:17},()=>crypto.randomUUID());
for(let i=0;i<2;i++){
 db.prepare('INSERT INTO channel(uuid,duration,guide_minimum_duration,icon,name,number,offline,start_time,transcode_config_id,group_title) VALUES(?,?,?,?,?,?,?,?,?,?)')
 .run(channelIds[i],3600000,30000,JSON.stringify({path:'',width:0,duration:0,position:'bottom-right'}),'Restore fixture '+i,i+1,JSON.stringify({mode:'pic'}),Date.now(),tx,'Fixture');
}
for(let i=0;i<ids.length;i++){
 db.prepare('INSERT INTO program(uuid,duration,external_key,external_source_id,source_type,title,type) VALUES(?,?,?,?,?,?,?)').run(ids[i],60000,''+i,'offline-fixture','plex','Fixture '+i,'movie');
 const v=crypto.randomUUID();
 db.prepare('INSERT INTO program_version(uuid,created_at,updated_at,duration,scan_kind,width,height,program_id) VALUES(?,?,?,?,?,?,?,?)').run(v,Date.now(),Date.now(),60000,'progressive',1280,720,ids[i]);
 db.prepare('INSERT INTO program_media_file(uuid,path,program_version_id) VALUES(?,?,?)').run(crypto.randomUUID(),'/media/fixture-'+i+'.mkv',v);
 db.prepare('INSERT INTO channel_programs(channel_uuid,program_uuid) VALUES(?,?)').run(channelIds[i%2],ids[i]);
}
for(let i=0;i<2;i++){
 const items=ids.filter((_,n)=>n%2===i).map(id=>({type:'content',id,durationMs:60000}));
 // Distinct SQL membership stays at 17; the API counts 19 content occurrences.
 items.push({...items[0]},{type:'offline',durationMs:10000},{type:'redirect',channel:channelIds[1-i],durationMs:20000});
 const startTimeOffsets=[0];
 for(const item of items)startTimeOffsets.push(startTimeOffsets.at(-1)+item.durationMs);
 db.prepare('UPDATE channel SET duration=? WHERE uuid=?').run(startTimeOffsets.at(-1),channelIds[i]);
 fs.writeFileSync(root+'/channel-lineups/'+channelIds[i]+'.json',JSON.stringify({version:5,lastUpdated:Date.now(),items,startTimeOffsets}));
}
db.exec('PRAGMA wal_checkpoint(TRUNCATE)');db.close();
// Preserve a second tree without ever running the older binary over the live newer DB.
for(const name of ['db.db','settings.json','channel-lineups','images'])fs.cpSync(root+'/'+name,'/config/tunarr/'+name,{recursive:true});
console.log('fixture seeded from real application migrations');
"""


def seed_fixture(name, version):
    image = BOOT.IMAGES[version]
    BOOT.docker('image', 'inspect', image)
    database_path = ROOT + ('/stable-1.3' if version == 'stable-1.3' else '')
    BOOT.docker('run', '-d', '--name', name, '--platform', 'linux/amd64', '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '568:568',
                '--memory', '2g', '--cpus', '2', '--pids-limit', '256',
                '--tmpfs', '/tmp:uid=568,gid=568,mode=700',
                '--tmpfs', '/tmp/.cache/pkg:exec,uid=568,gid=568,mode=700',
                '--tmpfs', ROOT + ':uid=568,gid=568,mode=700',
                '-e', 'HOME=/tmp', '-e', 'TUNARR_DATABASE_PATH=' + database_path,
                '-e', 'MEILI_DUMP_DIR=' + database_path + '/dumps',
                '-e', 'MEILI_MAX_INDEXING_MEMORY=128MiB', '-e', 'MEILI_MAX_INDEXING_THREADS=1',
                '--entrypoint', 'node', image, '-e', 'setInterval(()=>{},1000)')
    code = SEED
    if version != 'stable-1.3':
        code = code.replace("const root='/config/tunarr/stable-1.3';", "const root='/config/tunarr';")
        code = code.replace('version:5,lastUpdated:', 'version:6,lastUpdated:')
        code = '\n'.join(line for line in code.splitlines() if not line.startswith('for(const name of'))
    result = BOOT.node(name, code, check=False)
    if result.returncode:
        # Never enable this in BOOT.boot: restored production state stays private.
        diagnostic = BOOT.node(name, "console.log(require('fs').readFileSync('/tmp/fixture-bootstrap.json','utf8'))", check=False)
        raise RuntimeError('synthetic fixture seed failed: ' + json.dumps({
            'version': version, 'exit': result.returncode,
            'stdout': result.stdout[-8192:], 'stderr': result.stderr[-8192:],
            'bootstrap': diagnostic.stdout[-20000:],
        }))
    print(result.stdout.strip())
    # The schema bootstrap can leave its search child behind after HDHR exits.
    BOOT.node(name, r"const fs=require('fs');for(const p of fs.readdirSync('/proc').filter(p=>/^\d+$/.test(p))){try{const a=fs.readFileSync('/proc/'+p+'/cmdline','utf8').split('\0');if(a[0].includes('meilisearch'))process.kill(Number(p),'SIGTERM')}catch{}}")


class TunarrCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='tunarr-kopiur-tests-')
        cls.name = 'k8s92-tunarr-test-' + uuid.uuid4().hex[:10]
        try:
            seed_fixture(cls.name, 'stable-1.3')
            # Template bytes are synthetic and kept private, no production data.
            cls.template = Path(cls.work.name) / 'template'
            export_state(cls.name, cls.template)
        except Exception:
            BOOT.docker('rm', '-f', cls.name, check=False)
            cls.work.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        BOOT.docker('rm', '-f', cls.name, check=False)
        cls.work.cleanup()

    def setUp(self):
        reset = BOOT.node(self.name, "const fs=require('fs');for(const n of fs.readdirSync('/config/tunarr'))fs.rmSync('/config/tunarr/'+n,{recursive:true,force:true});", check=False)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        # Tar extraction as app UID avoids Docker cp host ownership changes.
        import tarfile
        archive = Path(self.work.name) / 'template.tar'
        with tarfile.open(archive, 'w') as tar:
            tar.add(self.template, arcname='.')
        with archive.open('rb') as stream:
            result = subprocess.run(['docker', 'exec', '-i', self.name, 'tar', 'xf', '-', '-C', ROOT], stdin=stream, capture_output=True)
            self.assertEqual(result.returncode, 0)

    def hook(self, deadline=False):
        code = SCRIPT.read_text()
        if deadline:
            code = code.replace('const DEADLINE_MS = 90000;', 'const DEADLINE_MS = 1200;')
        return BOOT.node(self.name, code, check=False)

    def extract(self):
        dest = Path(self.work.name) / uuid.uuid4().hex
        export_state(self.name, dest)
        return dest

    def test_embedded_hook_exact(self):
        docs = subprocess.run(['yq', '-o=json', 'select(.kind == "SnapshotPolicy") | .spec.hooks.beforeSnapshot[0].workloadExec.command', str(POLICY)], capture_output=True, text=True, check=True)
        command = json.loads(docs.stdout)
        self.assertEqual(command, ['node', '--disable-warning=ExperimentalWarning', '-e', SCRIPT.read_text()])
        self.assertIn('continueOnFailure: false', POLICY.read_text())

    def test_dual_capture_independent_promotions_and_stable_boot(self):
        start = float(BOOT.node(self.name, 'console.log(Date.now()/1000)').stdout)
        self.assertEqual(self.hook().returncode, 0)
        end = float(BOOT.node(self.name, 'console.log(Date.now()/1000)').stdout)
        source = self.extract()
        manifest = RESTORE.load(source / '.kopiur-consistent/manifest.json')
        outputs = []
        for backend in ['nas', 'r2']:
            destination = Path(self.work.name) / (backend + '-' + uuid.uuid4().hex)
            receipt = RESTORE.restore(source, destination, manifest['generation'], start, end, backend)
            for record in receipt['records']:
                self.assertEqual(record['counts']['channel'], 2)
                self.assertEqual(record['counts']['program'], 17)
                self.assertEqual(record['counts']['program_media_file'], 17)
                self.assertTrue((destination / '.kopiur-raw' / record['artifact'] / 'db.db').exists())
            report = BOOT.boot(destination, 'stable-1.3')
            self.assertEqual(report['apiChannels'], 2)
            self.assertEqual(report['apiPrograms'], 19)
            outputs.append(report['counts'])
        self.assertEqual(*outputs)
        # Same fixture, two independent promotions. NOT real NAS/R2 retrieval.
        print('local promotions and isolated boots:', json.dumps(outputs))

    def test_exact_newer_schema_capture_promotions_and_boots(self):
        # Generate newer state with its own pinned binary. Never migrate the stable
        # fixture up and call it proof of recovery of the retained production state.
        name = 'k8s92-tunarr-newer-' + uuid.uuid4().hex[:10]
        try:
            seed_fixture(name, 'retained-2026.9')
            state = subprocess.run(['docker', 'exec', name, 'tar', 'cf', '-', '-C', ROOT,
                                    'db.db', 'settings.json', 'channel-lineups', 'images'],
                                   capture_output=True, check=True, timeout=120)
            # Replace only the synthetic retained tree, leaving stable-1.3 intact.
            BOOT.node(self.name, "const fs=require('fs');for(const n of ['db.db','db.db-wal','db.db-shm','settings.json','channel-lineups','images'])fs.rmSync('/config/tunarr/'+n,{recursive:true,force:true});")
            subprocess.run(['docker', 'exec', '-i', self.name, 'tar', 'xf', '-', '-C', ROOT],
                           input=state.stdout, capture_output=True, check=True, timeout=120)
        finally:
            BOOT.docker('rm', '-f', name, check=False)
        start = float(BOOT.node(self.name, 'console.log(Date.now()/1000)').stdout)
        self.assertEqual(self.hook().returncode, 0)
        end = float(BOOT.node(self.name, 'console.log(Date.now()/1000)').stdout)
        source = self.extract()
        manifest = RESTORE.load(source / '.kopiur-consistent/manifest.json')
        reports = []
        # Raw snapshots must survive only in the archive, never auto-import at boot.
        for record in manifest['records']:
            snapshots = source / record['source'] / 'ms-snapshots'
            snapshots.mkdir(exist_ok=True)
            (snapshots / 'data.ms.snapshot').write_bytes(b'corrupt raw snapshot')
        for backend in ['nas', 'r2']:
            destination = Path(self.work.name) / ('dual-schema-' + backend)
            RESTORE.restore(source, destination, manifest['generation'], start, end, backend)
            for record in manifest['records']:
                self.assertFalse((destination / record['source'] / 'ms-snapshots').exists())
                self.assertEqual((destination / '.kopiur-raw' / record['artifact'] / 'ms-snapshots/data.ms.snapshot').read_bytes(),
                                 b'corrupt raw snapshot')
            for version in ['stable-1.3', 'retained-2026.9']:
                report = BOOT.boot(destination, version)
                self.assertEqual(report['counts'], {'channel': 2, 'program': 17,
                                                    'channel_programs': 17, 'program_media_file': 17})
                self.assertEqual(report['apiChannels'], 2)
                self.assertEqual(report['apiPrograms'], 19)
                reports.append(report)
        print('exact-image synthetic recovery boots:', json.dumps(reports))

    def test_sql_commit_before_json_gap_and_historical_restore(self):
        # Native regression: intentionally hold JSON constant across a committed
        # SQL update. Run only after Docker capacity has been resolved.
        for version in ['', 'stable-1.3']:
            for kind in ['membership', 'duration']:
                with self.subTest(version=version, kind=kind):
                    self.setUp()
                    self.assertEqual(self.hook().returncode, 0)
                    source = self.extract()
                    manifest = RESTORE.load(source / '.kopiur-consistent/manifest.json')
                    mutation = ("const row=d.prepare('SELECT channel_uuid,program_uuid FROM channel_programs LIMIT 1').get();"
                                "const replacement=d.prepare('SELECT uuid FROM program WHERE uuid NOT IN (SELECT program_uuid FROM channel_programs WHERE channel_uuid=?) LIMIT 1').get(row.channel_uuid).uuid;"
                                "d.prepare('UPDATE channel_programs SET program_uuid=? WHERE channel_uuid=? AND program_uuid=?').run(replacement,row.channel_uuid,row.program_uuid);"
                                if kind == 'membership' else "d.exec('UPDATE channel SET duration=duration+1');")
                    injection = ('if(version===' + json.dumps(version) + "){const d=new DatabaseSync(path.join(source,'db.db'));d.exec('BEGIN');"
                                 + mutation + "d.exec('COMMIT');d.close();}")
                    code = SCRIPT.read_text().replace('const before = companions(source);',
                                                      'const before = companions(source);' + injection)
                    self.assertNotEqual(BOOT.node(self.name, code, check=False).returncode, 0)
                    attempt = json.loads(BOOT.node(self.name, "console.log(require('fs').readFileSync('/config/tunarr/.kopiur-consistent/attempt.json','utf8'))").stdout)
                    self.assertEqual(attempt['state'], 'failed')
                    # A pre-fix manifest could bless exactly these mismatched bytes.
                    import sqlite3
                    record = next(r for r in manifest['records'] if r['source'] == version)
                    dbfile = source / '.kopiur-consistent' / ('capture-' + manifest['generation']) / record['artifact'] / 'db.db'
                    with closing(sqlite3.connect(dbfile)) as db, db:
                        if kind == 'duration':
                            db.execute('UPDATE channel SET duration=duration+1')
                        else:
                            channel, program = db.execute('SELECT channel_uuid,program_uuid FROM channel_programs LIMIT 1').fetchone()
                            replacement = db.execute('SELECT uuid FROM program WHERE uuid NOT IN (SELECT program_uuid FROM channel_programs WHERE channel_uuid=?) LIMIT 1', (channel,)).fetchone()[0]
                            db.execute('UPDATE channel_programs SET program_uuid=? WHERE channel_uuid=? AND program_uuid=?', (replacement, channel, program))
                    record['files']['db.db'] = RESTORE.digest(dbfile)
                    (source / '.kopiur-consistent/manifest.json').write_text(json.dumps(manifest))
                    with self.assertRaisesRegex(ValueError, 'lineup ' + kind + ' mismatch'):
                        RESTORE.verify(source, manifest['generation'], 0, time.time())

    def test_wal_state_advances_and_sources_unchanged(self):
        holder = r"""const fs=require('fs'),{DatabaseSync}=require('node:sqlite');const db=new DatabaseSync('/config/tunarr/stable-1.3/db.db');db.exec('PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE wal_fixture(n INTEGER); INSERT INTO wal_fixture VALUES(1)');fs.writeFileSync('/tmp/wal-ready','');setInterval(()=>{},1000);"""
        BOOT.docker('exec', '-d', self.name, 'node', '-e', holder)
        try:
            for _ in range(50):
                if BOOT.node(self.name, "process.exit(require('fs').existsSync('/tmp/wal-ready')?0:1)", check=False).returncode == 0:
                    break
                time.sleep(.1)
            checksum = "const fs=require('fs'),c=require('crypto');console.log(JSON.stringify(['db.db','db.db-wal'].map(n=>c.createHash('sha256').update(fs.readFileSync('/config/tunarr/stable-1.3/'+n)).digest('hex'))))"
            source = None
            for n in range(1, 4):
                before = BOOT.node(self.name, checksum).stdout
                self.assertEqual(self.hook().returncode, 0)
                self.assertEqual(BOOT.node(self.name, checksum).stdout, before)
                source = self.extract()
                m = RESTORE.load(source / '.kopiur-consistent/manifest.json')
                RESTORE.verify(source, m['generation'], 0, time.time())
                self.assertEqual(m['records'][1]['counts']['wal_fixture'], n)
                mode = BOOT.node(self.name, "console.log(require('fs').statSync('/config/tunarr/.kopiur-consistent/capture-" + m['generation'] + "').mode & 0o777)").stdout
                self.assertEqual(int(mode), 0o700)
                BOOT.node(self.name, "const {DatabaseSync}=require('node:sqlite');let d=new DatabaseSync('/config/tunarr/stable-1.3/db.db');d.exec('INSERT INTO wal_fixture VALUES(1)');d.close()")
            assert source is not None
            self.assertEqual(len(list((source / '.kopiur-consistent').glob('capture-*'))), 2)
        finally:
            self.kill_helpers()

    def kill_helpers(self):
        BOOT.node(self.name, "const fs=require('fs');for(const p of fs.readdirSync('/proc').filter(x=>/^\\d+$/.test(x))){if(+p===1||+p===process.pid)continue;try{const s=fs.readFileSync('/proc/'+p+'/cmdline','utf8');if(s.includes('setInterval(()=>{},1000)')&&s.includes('DatabaseSync'))process.kill(+p,'SIGKILL')}catch{}}")

    def test_fail_closed_missing_corrupt_companions_and_symlinks(self):
        changes = [
            "fs.unlinkSync(root+'/db.db')",
            "fs.writeFileSync(root+'/db.db','not sqlite')",
            "fs.writeFileSync(root+'/settings.json','invalid json')",
            "fs.rmSync(root+'/images',{recursive:true})",
            "fs.unlinkSync(root+'/db.db');fs.symlinkSync('/tmp/sentinel',root+'/db.db')",
            "fs.renameSync(root+'/channel-lineups',root+'/old-lineups');fs.symlinkSync(root+'/old-lineups',root+'/channel-lineups')",
            "fs.writeFileSync(root+'/channel-lineups/00000000-0000-0000-0000-000000000000.json',JSON.stringify({items:[]}))",
        ]
        for version in ['', '/stable-1.3']:
            for change in changes:
                with self.subTest(version=version, mutation=change):
                    self.setUp()
                    self.assertEqual(self.hook().returncode, 0)
                    BOOT.node(self.name, "const fs=require('fs'),root='/config/tunarr" + version + "';fs.writeFileSync('/tmp/sentinel','sentinel');" + change)
                    self.assertNotEqual(self.hook().returncode, 0)
                    result = BOOT.node(self.name, "console.log(require('fs').readFileSync('/config/tunarr/.kopiur-consistent/attempt.json','utf8'))", check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    attempt = json.loads(result.stdout)
                    self.assertEqual(attempt['state'], 'failed')

    def test_companion_drift_and_capture_mutex(self):
        self.assertEqual(self.hook().returncode, 0)
        changed = SCRIPT.read_text().replace(
            'if (JSON.stringify(before) !== JSON.stringify(companions(source)))',
            "fs.appendFileSync(path.join(source, 'settings.json'), ' '); if (JSON.stringify(before) !== JSON.stringify(companions(source)))")
        self.assertNotEqual(BOOT.node(self.name, changed, check=False).returncode, 0)
        attempt = json.loads(BOOT.node(self.name, "console.log(require('fs').readFileSync('/config/tunarr/.kopiur-consistent/attempt.json','utf8'))").stdout)
        self.assertEqual(attempt['state'], 'failed')
        self.assertEqual(self.hook().returncode, 0)
        BOOT.node(self.name, "require('fs').mkdirSync('/config/tunarr/.kopiur-consistent/lock')")
        self.assertNotEqual(self.hook().returncode, 0)
        source = self.extract()
        m = RESTORE.load(source / '.kopiur-consistent/manifest.json')
        with self.assertRaises(ValueError):
            RESTORE.verify(source, m['generation'], 0, time.time())

    def test_exclusive_lock_deadline_rejects_stale_capture(self):
        self.assertEqual(self.hook().returncode, 0)
        code = "const fs=require('fs'),{DatabaseSync}=require('node:sqlite');const d=new DatabaseSync('/config/tunarr/db.db');d.exec('PRAGMA journal_mode=DELETE; BEGIN EXCLUSIVE');fs.writeFileSync('/tmp/locked','');setInterval(()=>{},1000)"
        BOOT.docker('exec', '-d', self.name, 'node', '-e', code)
        try:
            for _ in range(50):
                if BOOT.node(self.name, "process.exit(require('fs').existsSync('/tmp/locked')?0:1)", check=False).returncode == 0:
                    break
                time.sleep(.1)
            start = time.monotonic()
            self.assertNotEqual(self.hook(deadline=True).returncode, 0)
            self.assertLess(time.monotonic() - start, 8)
            source = self.extract()
            m = RESTORE.load(source / '.kopiur-consistent/manifest.json')
            with self.assertRaises(ValueError):
                RESTORE.verify(source, m['generation'], 0, time.time())
        finally:
            self.kill_helpers()

    def test_restore_rejects_corruption_freshness_traversal_and_nonempty(self):
        self.assertEqual(self.hook().returncode, 0)
        source = self.extract()
        m = RESTORE.load(source / '.kopiur-consistent/manifest.json')
        generation = m['generation']
        with self.assertRaises(ValueError):
            RESTORE.verify(source, generation, time.time() + 5, time.time() + 10)
        with self.assertRaises(ValueError):
            RESTORE.verify(source, '0' * 32, 0, time.time())
        with self.assertRaises(ValueError):
            RESTORE.restore(source, source, generation, 0, time.time(), 'nas')
        with self.assertRaises(ValueError):
            RESTORE.restore(source, source / 'nested', generation, 0, time.time(), 'nas')
        db = source / '.kopiur-consistent' / ('capture-' + generation) / 'stable-1.3/db.db'
        data = db.read_bytes()
        db.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with self.assertRaises(ValueError):
            RESTORE.verify(source, generation, 0, time.time())
        db.write_bytes(data)
        db.rename(db.with_suffix('.sentinel'))
        db.symlink_to(db.with_suffix('.sentinel'))
        with self.assertRaises(ValueError):
            RESTORE.verify(source, generation, 0, time.time())


if __name__ == '__main__':
    unittest.main()
