#!/usr/bin/env python3
"""Remote non-production PDS whole-store capture and native recovery.

Two real encrypted NAS/R2 repositories, exact mover, pinned production PDS,
source removed before fresh isolated native boots. No production workload change.
The Docker copy establishes the non-production point, not a CSI claim.
"""
import base64
import json
from typing import Any
import subprocess
import time
import uuid
import hashlib
import re
import secrets

PYTHON = 'python:3.13-alpine@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0'
PDS = 'ghcr.io/bluesky-social/pds@sha256:74c650cd620eee2e6e0272b4cb76036a0271523b1742f61f813b79dc5a8b5273'


def docker(*args, data=None, timeout=120):
    result = subprocess.run(['docker', *args], input=data, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Test-only container errors may contain generated auth material. Keep it
        # out of the parent transcript. Report the command class, not its payload.
        import re
        status = re.search(r'DRILL_HTTP_STATUS ([0-9]{3})', result.stderr)
        detail = ' HTTP ' + status.group(1) if status else ''
        raise RuntimeError('Docker ' + args[0] + ' failed' + detail + '; output withheld to protect fixture credentials')
    return result.stdout.strip()


class Drill:
    def __init__(self):
        self.prefix = 'k8s92-native-' + uuid.uuid4().hex[:10]
        self.containers, self.volumes = [], []

    def volume(self, suffix):
        name = self.prefix + '-' + suffix
        docker('volume', 'create', '--label', 'k8s92.pds='+self.prefix, name)
        self.volumes.append(name)
        docker('run','--rm','--network=none','-v',name+':/data',PYTHON,
               'python3','-c','import os;os.chown("/data",1000,1000)')
        return name

    def start(self, suffix, image, mounts, env=None, command=None, network='none', entrypoint=None,
              volume_subpaths=None):
        name = self.prefix + '-' + suffix
        subpaths = volume_subpaths or {}
        args = ['run', '-d', '--name', name, '--network', network, '--user','1000:1000',
                '--label','k8s92.pds='+self.prefix,'--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--tmpfs','/tmp:rw,nosuid,uid=1000,gid=1000']
        for vol, path in mounts.items():
            if vol in subpaths:
                args += ['--mount','type=volume,src='+vol+',dst='+path+',volume-nocopy,volume-subpath='+subpaths[vol]]
            else:
                args += ['-v',vol+':'+path]
        for key, value in (env or {}).items(): args += ['-e', key + '=' + value]
        if entrypoint: args += ['--entrypoint', entrypoint]
        args += [image] + (command or [])
        self.containers.append(name)
        docker(*args)
        return name

    def python(self, container, code, data=None):
        if container in getattr(self, 'native_python', set()):
            return docker('exec', '-i', container, 'python3', '-c', code, data=data)
        return docker('run', '--rm', '-i', '--network', 'container:' + container,
                      PYTHON, 'python3', '-c', code, data=data)

    def http(self, container, path, body=None, auth=None, port=3000, raw=False, headers=None, method=None) -> Any:
        config = {'path': path, 'body': body, 'auth': auth, 'port': port, 'headers': headers or {}, 'method': method}
        result = self.python(container, '''import base64,json,sys,urllib.request
c=json.load(sys.stdin)
h=dict(c['headers'])
if c['body'] is not None: h['Content-Type']='application/json'
if c['auth']: h['Authorization']='Basic '+base64.b64encode(c['auth'].encode()).decode()
r=urllib.request.Request('http://127.0.0.1:'+str(c['port'])+c['path'], headers=h,
 data=None if c['body'] is None else json.dumps(c['body']).encode(),method=c['method'])
try:
 with urllib.request.urlopen(r,timeout=15) as f: print(base64.b64encode(f.read()).decode())
except urllib.error.HTTPError as e:
 print('DRILL_HTTP_STATUS '+str(e.code),file=sys.stderr);sys.exit(1)
''', json.dumps(config))
        value = base64.b64decode(result)
        if raw:
            return value
        try:
            return json.loads(value or b'{}')
        except json.JSONDecodeError:
            if value.strip() == b'OK':
                return {}
            raise

    def ready(self, container, path, port=3000, **kwargs):
        for _ in range(60):
            try:
                return self.http(container, path, port=port, **kwargs)
            except RuntimeError:
                if docker('inspect', '--format', '{{.State.Running}}', container) != 'true':
                    logs = subprocess.run(['docker', 'logs', container], capture_output=True, text=True).stderr
                    detail = next((line for line in logs.splitlines() if line.startswith('DRILL_ERROR ')), '')
                    raise RuntimeError('application exited before readiness: ' + detail) from None
                time.sleep(1)
        raise TimeoutError('application readiness timed out')

    def copy(self, source, target):
        if source == target:
            raise ValueError('recovery requires an independent volume')
        result = docker('run', '--rm', '--network=none', '-v', source + ':/from:ro', '-v', target + ':/to',
               PYTHON, 'python3', '-c', '''import hashlib,json,os,subprocess
from pathlib import Path
subprocess.run(['cp','-a','/from/.','/to/'],check=True)
def manifest(root):
 rows=[]
 for p in sorted(Path(root).rglob('*')):
  s=p.lstat()
  content=os.readlink(p) if p.is_symlink() else hashlib.file_digest(p.open('rb'),'sha256').hexdigest() if p.is_file() else None
  rows.append([str(p.relative_to(root)),s.st_mode,s.st_uid,s.st_gid,content])
 return rows
before,after=manifest('/from'),manifest('/to')
assert before==after,'copy contents, ownership or modes differ'
print(json.dumps({'entries':len(before),'manifestSHA256':hashlib.sha256(json.dumps(before).encode()).hexdigest(),'contentsOwnersModesMatch':True}))''')
        if not hasattr(self, 'copy_evidence'):
            self.copy_evidence = []
        self.copy_evidence.append(json.loads(result))

    def quiesce_copy_resume(self, container, pairs):
        source, nas = pairs[0]
        point = self.volume('immutable-point')
        started = time.monotonic()
        try:
            docker('stop','-t','60',container)
            state=json.loads(docker('inspect','--format','{{json .State}}',container))
            assert not state['Running'] and not state['OOMKilled'] and state['ExitCode'] != 137
            self.copy(source,point)
        finally:
            docker('start',container)
        self.quiesce_seconds=round(time.monotonic()-started,3)
        self.ready(container,'/xrpc/_health')
        self.point_manifest=self.manifest(point)
        captures=[self.transport.capture(kind,point) for kind in ('nas','r2')]
        # Both repository generations now exist. Remove the producer and all source
        # volumes BEFORE connecting independent fresh restore contexts.
        docker('rm','-f',container);self.containers.remove(container)
        for volume in (source,point):
            docker('volume','rm',volume);self.volumes.remove(volume)
        self.restore_volumes=[nas,self.volume('pds-r2-restored')]
        self.receipts=[]
        for capture,volume in zip(captures,self.restore_volumes):
            receipt=self.transport.restore(capture,volume)
            restored_manifest=self.manifest(volume, 'point')
            assert restored_manifest==self.point_manifest,'native bytes, owners or modes differ'
            receipt['manifest_sha256']=hashlib.sha256(json.dumps(restored_manifest).encode()).hexdigest()
            receipt['equal_entries']=len(restored_manifest)
            receipt['producer_and_source_volumes_removed_before_restore']=True
            self.receipts.append(receipt)

    def manifest(self, volume, subdirectory=''):
        return json.loads(docker('run','--rm','--network=none','-v',volume+':/data:ro',
                                PYTHON,'python3','-c','''import hashlib,json,os,sys
from pathlib import Path
root=Path('/data')/sys.argv[1];result=[]
for path in [root,*sorted(root.rglob('*'))]:
    info=path.lstat();assert not path.is_symlink()
    assert path.is_file() or path.is_dir()
    digest=hashlib.file_digest(path.open('rb'),'sha256').hexdigest() if path.is_file() else None
    result.append([str(path.relative_to(root)),info.st_mode,info.st_uid,info.st_gid,digest])
print(json.dumps(result))''',subdirectory))
    def clean(self):
        failures=[]
        for kind,names in (('container',self.containers),('volume',self.volumes)):
            for name in reversed(names):
                try:
                    label=docker(kind,'inspect','--format','{{ index .Labels "k8s92.pds" }}' if kind=='volume'
                                 else '{{ index .Config.Labels "k8s92.pds" }}',name)
                    assert label==self.prefix,'native fixture ownership changed'
                    docker('rm','-f',name) if kind=='container' else docker('volume','rm',name)
                except Exception:
                    failures.append(kind+'-cleanup')
        if failures:
            raise RuntimeError('owned PDS fixture cleanup failed: '+','.join(failures))

    def pds(self):
        source, restored = self.volume('pds'), self.volume('pds-copy')
        password = uuid.uuid4().hex
        env = {'PDS_DATA_DIRECTORY': '/pds', 'PDS_BLOBSTORE_DISK_LOCATION': '/pds/blocks',
            'PDS_HOSTNAME': 'localhost', 'PDS_DEV_MODE': 'true', 'PDS_INVITE_REQUIRED': 'false',
            'PDS_JWT_SECRET': uuid.uuid4().hex, 'PDS_ADMIN_PASSWORD': uuid.uuid4().hex,
            'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX': '01' * 32,
            'PDS_DID_PLC_URL': 'http://127.0.0.1:1', 'PDS_CRAWLERS': '',
            'PDS_ENABLE_DID_DOC_WITH_SESSION': 'false', 'DRILL_PASSWORD': password, 'LOG_ENABLED': 'false'}
        seed = '''const {createRequire}=require('module'), fs=require('fs');
const p=require('@atproto/pds'), r=createRequire(require.resolve('@atproto/pds'));
const {Secp256k1Keypair}=r('@atproto/crypto'), {cidForRawBytes}=r('@atproto/lex-data');
p.PDS.run({onStarted:async server=>{
 if(fs.existsSync('/pds/drill.json'))return;
 const actors=[];
 for(let i=0;i<2;i++){
  const did='did:plc:'+String.fromCharCode(97+i).repeat(24), handle='drill'+i+'.localhost';
  const key=await Secp256k1Keypair.create({exportable:true});
  await server.ctx.actorStore.create(did,key);
  const commit=await server.ctx.actorStore.transact(did,tx=>tx.repo.createRepo([]));
  const creds=await server.ctx.accountManager.createAccountAndSession({did,handle,email:'drill'+i+'@example.com',password:process.env.DRILL_PASSWORD,repoCid:commit.cid,repoRev:commit.rev,deactivated:false});
  await server.ctx.sequencer.sequenceAccountCreation(did,handle,commit);
  const blob=Buffer.from('recovery blob '+i), cid=await cidForRawBytes(blob);
  await server.ctx.blobstore(did).putPermanent(cid,blob);
  await server.ctx.actorStore.transact(did,tx=>tx.repo.blob.insertBlobMetadata({$type:'blob',ref:cid,mimeType:'text/plain',size:blob.length}));
  actors.push({did,handle,cid:String(cid),refreshJwt:creds.refreshJwt});
 }
 fs.writeFileSync('/pds/drill.json',JSON.stringify(actors),{mode:0o600});
}}).catch(error=>{
 let message=String(error.stack);
 for(const value of Object.values(process.env))if(value.length>=16)message=message.split(value).join('[redacted]');
 console.error('DRILL_ERROR '+message); process.exit(1);
});'''
        app = self.start('pds', PDS, {source: '/pds'}, env, ['-e', seed], entrypoint='node')
        self.ready(app, '/xrpc/_health')
        for _ in range(60):
            if docker('exec', app, 'node', '-e', "console.log(require('fs').existsSync('/pds/drill.json'))") == 'true':
                break
            time.sleep(1)
        else:
            raise TimeoutError('native PDS fixture initialization timed out')
        actors = json.loads(docker('exec', app, 'node', '-e', "console.log(require('fs').readFileSync('/pds/drill.json','utf8'))"))
        records = []
        for actor in actors:
            login = self.http(app, '/xrpc/com.atproto.server.createSession',
                {'identifier': actor['handle'], 'password': password})
            records.append(self.http(app, '/xrpc/com.atproto.repo.createRecord',
                {'repo': actor['did'], 'collection': 'app.bsky.feed.post',
                 'record': {'$type': 'app.bsky.feed.post', 'text': 'before recovery',
                            'createdAt': '2026-09-26T00:00:00.000Z'}},
                headers={'Authorization': 'Bearer ' + login['accessJwt']}))
        self.quiesce_copy_resume(app, [(source, restored)])
        for number,restored in enumerate(self.restore_volumes):
            # Verify every nested actor database and key, not just a sample CAR.
            check = docker('run', '--rm', '--network=none', '-v', restored + ':/data:ro',
                           '--tmpfs','/scratch:rw,nosuid,size=192m', PYTHON, 'python3', '-c', '''import json,sqlite3,subprocess
from pathlib import Path
# WAL readers may create shared-memory indexes. Materialize only a disposable
# verifier copy, never mutate the canonical point or ignore its WAL bytes.
subprocess.run(['cp','-a','/data/point','/scratch/point'],check=True)
p=Path('/scratch/point'); dbs=list(p.rglob('*.sqlite')); keys=list(p.rglob('key'))
assert len(dbs)>=5 and len(keys)==2
for db in dbs:
 c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True); assert c.execute('pragma integrity_check').fetchone()[0]=='ok'; c.close()
for key in keys: assert key.stat().st_size==32
assert len(list((p/'blocks').rglob('*')))>2
print(json.dumps({'sqliteDatabases':len(dbs),'actorKeys':len(keys)}))''')
            # Restore uses the unmodified production image entrypoint. No seed
            # callback is mounted or run against recovered state.
            target = self.start('pds-restore-'+str(number), PDS, {restored: '/pds'}, env,
                                volume_subpaths={restored:'point'})
            self.ready(target, '/xrpc/_health')
            recovered_actors = json.loads(docker('exec', target, 'node', '-e', "console.log(require('fs').readFileSync('/pds/drill.json','utf8'))"))
            assert actors == recovered_actors
            for i, actor in enumerate(actors):
                login = self.http(target, '/xrpc/com.atproto.server.createSession', {'identifier': actor['handle'], 'password': password})
                assert login['did'] == actor['did']
                refreshed = self.http(target, '/xrpc/com.atproto.server.refreshSession', method='POST',
                    headers={'Authorization': 'Bearer ' + actor['refreshJwt']})
                assert refreshed['did'] == actor['did']
                recovered = self.http(target, '/xrpc/com.atproto.repo.getRecord?repo=' + actor['did']
                    + '&collection=app.bsky.feed.post&rkey=' + records[i]['uri'].rsplit('/', 1)[1])
                assert recovered['cid'] == records[i]['cid'] and recovered['value']['text'] == 'before recovery'
                signed = self.http(target, '/xrpc/com.atproto.repo.createRecord',
                    {'repo': actor['did'], 'collection': 'app.bsky.feed.post',
                     'record': {'$type': 'app.bsky.feed.post', 'text': 'recovered fixture',
                                'createdAt': '2026-09-26T00:00:00.000Z'}},
                    headers={'Authorization': 'Bearer ' + login['accessJwt']})
                assert signed.get('cid')
                car = self.http(target, '/xrpc/com.atproto.sync.getRepo?did=' + actor['did'], raw=True)
                assert len(car) > 100
                blob = self.http(target, '/xrpc/com.atproto.sync.getBlob?did=' + actor['did'] + '&cid=' + actor['cid'], raw=True)
                assert blob == ('recovery blob ' + str(i)).encode()
            self.receipts[number].update(json.loads(check))
            self.receipts[number].update(account_logins=len(actors),repo_reads=len(actors),blob_reads=len(actors),
                                        refresh_sessions=len(actors),signed_repo_writes=len(actors),
                                        prior_records_recovered=len(records),native_isolated_boot=True)
        return self.receipts


def exercise(identity,app,fields,deadline):
    class Transport(identity.Drill):
        def __init__(self):
            super().__init__(app,fields)
            self.path='identity-fixtures/run8-native-'+self.nonce
            self.deadline=min(deadline-90,time.monotonic()+900)

        def start(self,volume=None,path=None,readonly=False):
            name='k8s92-identity-'+self.nonce+'-'+str(len(self.containers))
            self.containers.append(name)
            args=['docker','run','-d','--name',name,'--label','k8s92.identity='+self.nonce,
                  '--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges',
                  '--read-only','--memory','512m','--tmpfs','/tmp:rw,nosuid,mode=1777',
                  '--tmpfs','/work:rw,nosuid,size=192m,uid=1000,gid=1000,mode=0700',
                  '--mount','type=volume,src='+identity.TOOLS+',dst=/tools,readonly,volume-nocopy']
            if volume:
                args+=['--mount','type=volume,src='+volume+',dst='+path+',volume-nocopy'+(',readonly' if readonly else '')]
            args+=['--entrypoint','/tools/busybox',identity.IMAGE,'sh','-c','/tools/sleep 900']
            self.run(args)
            return name

        def capture(self,kind,point):
            self.stage=kind+'-native-capture'
            password=fields['NAS_KOPIA_PASSWORD' if kind=='nas' else 'R2_KOPIA_PASSWORD']
            container=self.start(point,'/source',True)
            result=self.exec(container,'k repository create '+self.backend(kind)+' >/dev/null\n'
                             +'k snapshot create /source --json\n',password=password)
            snapshot=json.loads(result.stdout)
            object_id=snapshot['rootEntry']['obj']
            assert re.fullmatch(r'[A-Za-z0-9]+',object_id)
            self.remove(container)
            return {'backend':kind,'snapshot_id':snapshot['id'],'object_id':object_id}

        def restore(self,capture,volume):
            kind=capture['backend']
            self.stage=kind+'-wrong-password'
            wrong=self.start()
            refused=self.exec(wrong,'k repository connect '+self.backend(kind)+' >/dev/null\n',
                              password=secrets.token_hex(32),check=False)
            assert refused.returncode and any(code in refused.stderr.lower() for code in
                (b'invalid repository password',b'unable to decrypt'))
            self.remove(wrong)
            self.stage=kind+'-native-restore'
            container=self.start(volume,'/restored')
            # Keep Kopia's sibling placeholders on the owned writable volume,
            # never on the intentionally read-only container root filesystem.
            self.exec(container,'k repository connect '+self.backend(kind)+' >/dev/null\n'
                      +'k snapshot restore '+capture['object_id']+' /restored/point >/dev/null\n',
                      password=fields['NAS_KOPIA_PASSWORD' if kind=='nas' else 'R2_KOPIA_PASSWORD'])
            self.remove(container)
            return capture|{'restored_volume_subdirectory':'point',
                            'wrong_encryption_password_denied':True,'fresh_container_direct_restore':True}

    for image in (PDS,PYTHON,identity.IMAGE):
        docker('pull','--platform','linux/amd64',image,timeout=600)
    transport=Transport()
    native=Drill();native.transport=transport
    try:
        results=native.pds()
        receipt={'app':app,'runner':__import__('os').environ['RUNNER_NAME'],
                 'mover_image':identity.IMAGE,'native_image':PDS,'results':results,
                 'nonproduction_source_stop_copy_resume_seconds':native.quiesce_seconds,
                 'nonproduction_capture_boundary':'stopped whole-volume immutable Docker copy, not CSI',
                 'all_native_validation_egress':'none','owned_fixture_repositories_removed':True}
    finally:
        try:
            # A failed mover may still mount a restored native volume.
            # Stop owned transport containers before native volume cleanup.
            transport.cleanup()
        finally:
            native.clean()
    return receipt
