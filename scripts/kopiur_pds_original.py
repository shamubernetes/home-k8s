#!/usr/bin/env python3
"""Read retained PDS originals. Never write or remove production repository data.

Use the existing ARC identity channel. Dedicated transport credentials stay in
memory/tmpfs; native boots have no network and use no production Kubernetes access.
"""
import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path

import kopiur_pds_native as native


def validate_original(payload):
    original = payload['original']
    if set(original) != {'snapshots', 'bundle_sha256', 'runtime_sha256'}:
        raise ValueError('unexpected original contract')
    snapshots = original['snapshots']
    if set(snapshots) != {'nas', 'r2'} or not all(
            isinstance(x, str) and re.fullmatch(r'[0-9a-f]{32}', x)
            for x in snapshots.values()) or snapshots['nas'] == snapshots['r2']:
        raise ValueError('exact distinct original IDs required')
    files = original['bundle_sha256']
    if set(files) != {'application-state.tar', 'runtime.json', 'metadata.json'} or not all(
            isinstance(x, str) and re.fullmatch(r'[0-9a-f]{64}', x)
            for x in [*files.values(), original['runtime_sha256']]):
        raise ValueError('original hashes required')
    return original


class Original(native.Drill):
    def __init__(self):
        super().__init__()
        self.native_python = set()
        self.native_logs_disabled = True

    def start(self, *args, **kwargs):
        kwargs['log_driver'] = 'none'
        return super().start(*args, **kwargs)

    def volume(self, suffix):
        name = self.prefix + '-' + suffix
        native.docker('volume', 'create', '--label', 'k8s92.pds='+self.prefix,
                      '--driver', 'local', '--opt', 'type=tmpfs', '--opt', 'device=tmpfs',
                      '--opt', 'o=size=192m,uid=1000,gid=1000,mode=0700', name)
        self.volumes.append(name)
        # A local tmpfs volume loses its bytes when its last mount closes.
        # Keep one network-none mount alive through reader/checker/native handoff.
        self.start('keep-'+suffix, native.PYTHON, {name:'/memory'},
                   command=['python3','-c','import time;time.sleep(1800)'])
        return name


# Run inside an isolated Python container, never on the source workload.
EXTRACT = '''import hashlib,json,os,pathlib,sqlite3,sys,tarfile
VERIFY_METADATA=True
p=pathlib.Path('/data/restored/.kopiur-pds'); expected=json.load(sys.stdin)
assert not p.is_symlink() and p.is_dir()
assert not (p/'COMPLETE').is_symlink() and (p/'COMPLETE').is_file()
assert (p/'COMPLETE').read_text()=='complete\\n'
b=p/'current';assert not b.is_symlink() and b.is_dir()
assert {x.name for x in b.iterdir()}==set(expected)|{'SHA256SUMS'}
assert all(not f.is_symlink() and f.is_file() for f in b.iterdir())
actual={name:hashlib.file_digest((b/name).open('rb'),'sha256').hexdigest() for name in expected}
assert actual==expected,'original bundle hashes differ'
checksums={}
for line in (b/'SHA256SUMS').read_text().splitlines():
 digest,name=line.split('  ',1);assert name not in checksums;checksums[name]=digest
assert checksums==actual
metadata=json.loads((b/'metadata.json').read_text());assert metadata['format']==1 and metadata['contract']=='stopped-pds-whole-store'
point=pathlib.Path('/data/point');point.mkdir(mode=0o700)
with tarfile.open(b/'application-state.tar') as archive:
 members=archive.getmembers();seen=set()
 assert sum(m.size for m in members)<192*1024*1024
 for member in members:
  name=pathlib.PurePosixPath(member.name)
  assert not name.is_absolute() and '..' not in name.parts
  normalized=str(name);assert normalized not in seen;seen.add(normalized)
  assert member.isfile() or member.isdir()
  assert member.gid==1000 and (member.uid==1000 or member.isdir() and member.uid==0)
  assert not member.mode & 0o4000 and (member.isdir() or not member.mode & 0o2000)
 archive.extractall(point,filter='fully_trusted')
 if VERIFY_METADATA:
  for member in members:
   info=(point/member.name).lstat()
   assert (info.st_uid,info.st_gid,info.st_mode & 0o7777)==(member.uid,member.gid,member.mode),'original file metadata differs'
rows=[];databases=[];keys=0
for f in [point,*sorted(point.rglob('*'))]:
 info=f.lstat();assert f.is_dir() or f.is_file();assert not f.is_symlink()
 digest=hashlib.file_digest(f.open('rb'),'sha256').hexdigest() if f.is_file() else None
 rows.append([str(f.relative_to(point)),info.st_mode,info.st_uid,info.st_gid,digest])
 if f.name=='key':assert f.stat().st_size==32;keys+=1
 if f.name.endswith('.sqlite'):
  c=sqlite3.connect('file:'+str(f)+'?mode=ro',uri=True)
  assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
  tables={}
  for (table,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"):
   quoted='"'+table.replace('"','""')+'"'
   values=sorted(repr(tuple(r)) for r in c.execute('select * from '+quoted))
   tables[table]={'rows':len(values),'sha256':hashlib.sha256(json.dumps(values).encode()).hexdigest()}
  databases.append({'path_sha256':hashlib.sha256(str(f.relative_to(point)).encode()).hexdigest(),'tables':tables});c.close()
assert len(databases)>=3 and keys>=1
runtime_file=point/'.recovery-runtime.json'
assert not runtime_file.exists()
fd=os.open(runtime_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'wb') as out:out.write((b/'runtime.json').read_bytes())
if VERIFY_METADATA:os.chown(runtime_file,1000,1000)
print(json.dumps({'equal_entries':len(rows),'tree_sha256':hashlib.sha256(json.dumps(rows).encode()).hexdigest(),'databases':databases,'actor_keys':keys,'bundle_sha256':actual}))
'''


def original_api(drill, target, runtime, actor_keys, deadline):
    repos=[];cursor=None;pages=0;seen_cursors=set()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError('original API absolute deadline exceeded')
        from urllib.parse import quote
        page=drill.http(target,'/xrpc/com.atproto.sync.listRepos?limit=1'+
                       ('&cursor='+quote(cursor,safe='') if cursor else ''))
        repos.extend(page['repos']);pages+=1
        if not page.get('cursor'):break
        if page['cursor'] in seen_cursors:
            raise ValueError('native pagination cycle')
        seen_cursors.add(page['cursor'])
        cursor=page['cursor']
    if len(repos)!=actor_keys:
        raise ValueError('restored repository/key inventory differs')
    api=[]
    for repo in repos:
        did=quote(repo['did'],safe='')
        account=drill.http(target,'/xrpc/com.atproto.admin.getAccountInfo?did='+did,
                           auth='admin:'+runtime['PDS_ADMIN_PASSWORD'])
        if account['did']!=repo['did']:raise ValueError('original admin identity differs')
        car=drill.http(target,'/xrpc/com.atproto.sync.getRepo?did='+did,raw=True)
        if len(car)<100:raise ValueError('empty original repository CAR')
        blobs=[];blob_cursor=None;seen_blob_cursors=set()
        while True:
            page=drill.http(target,'/xrpc/com.atproto.sync.listBlobs?did='+did+
                           ('&cursor='+quote(blob_cursor,safe='') if blob_cursor else ''))
            for cid in page['cids']:
                data=drill.http(target,'/xrpc/com.atproto.sync.getBlob?did='+did+
                                '&cid='+quote(cid,safe=''),raw=True)
                blobs.append([cid,hashlib.sha256(data).hexdigest()])
            if not page.get('cursor'):break
            if page['cursor'] in seen_blob_cursors:raise ValueError('native blob pagination cycle')
            seen_blob_cursors.add(page['cursor']);blob_cursor=page['cursor']
        api.append({'did_sha256':hashlib.sha256(repo['did'].encode()).hexdigest(),
                    'blobs':blobs,
                    'repo':{k:v for k,v in repo.items() if k!='did'},
                    'car_sha256':hashlib.sha256(car).hexdigest(),
                    'account_sha256':hashlib.sha256(json.dumps(account,sort_keys=True).encode()).hexdigest()})
    return api, pages


def consumer_recovery(drill, target, runtime, actor_keys, deadline, api):
    # These 45-second acceptance holds affect only the isolated original restore.
    # Production's absolute hold and acquisition minimum remain unchanged.
    control = ['exec',target,'sh','/kopiur/recurring.sh']
    def state():
        return json.loads(native.docker('exec',target,'node','-e',
            "const fs=require('fs'),p=process.env.QUIESCENCE_STATE_DIR;"
            "const read=n=>fs.existsSync(p+'/'+n)?fs.readFileSync(p+'/'+n,'utf8'):null;"
            "console.log(JSON.stringify({request:read('request'),active:read('active'),"
            "now:Math.floor(Date.now()/1000)}))"))
    def resumed_state():
        # Timestamp the successful health request inside the container. A later
        # exec/state read can cross expiry and must not hide premature recovery.
        return json.loads(native.docker('exec',target,'node','-e',
            "const fs=require('fs'),p=process.env.QUIESCENCE_STATE_DIR;"
            "const read=n=>fs.existsSync(p+'/'+n)?fs.readFileSync(p+'/'+n,'utf8'):null;"
            "(async()=>{for(let i=0;i<60;i++){const healthAt=Math.floor(Date.now()/1000);"
            "try{const r=await fetch('http://127.0.0.1:3000/xrpc/_health',"
            "{signal:AbortSignal.timeout(2000)});if(!r.ok)throw Error('health');"
            "console.log(JSON.stringify({healthAt,now:Math.floor(Date.now()/1000),"
            "request:read('request'),active:read('active')}));return;}catch(e){}"
            "await new Promise(r=>setTimeout(r,1000));}process.exit(1)})()"))
    def stopped():
        proof = native.docker('exec',target,'node','-e',
            "fetch('http://127.0.0.1:3000/xrpc/_health',"
            "{signal:AbortSignal.timeout(2000)}).then(()=>process.exit(1))"
            ".catch(()=>console.log('writer-stopped'))")
        if proof != 'writer-stopped':
            raise ValueError('original writer remained available during hold')
    for recovery in ('release','watchdog','container-restart'):
        lease_token=os.urandom(16).hex()
        native.docker(*control,'acquire',lease_token,'45','30')
        accepted=state()
        request=accepted['request']
        if not request or request!=accepted['active']:
            raise ValueError('original hold was not acknowledged')
        parts=request.split()
        if len(parts)!=3 or parts[1]!=lease_token:
            raise ValueError('original accepted hold identity differs')
        expires=int(parts[2])
        if accepted['now']>=expires:
            raise ValueError('original hold expired before verification')
        if recovery == 'container-restart':
            native.docker('restart',target)
            # The supervisor must re-acknowledge the same accepted request before
            # starting any native child, without extending its absolute expiry.
            while True:
                replacement=state()
                if replacement['request']!=request or replacement['now']>=expires:
                    raise ValueError('original replacement lost or extended hold')
                if replacement['active']==request:break
                if time.monotonic()>=deadline:
                    raise TimeoutError('original replacement acknowledgement deadline')
                time.sleep(1)
        stopped()
        native.docker(*control,'liveness')
        current=state()
        if current['request']!=request or current['active']!=request or current['now']>=expires:
            raise ValueError('original hold expired before recovery action')
        if recovery == 'release':
            native.docker(*control,'release',lease_token)
        # Watchdog and replacement deliberately await the unchanged absolute expiry.
        resumed=resumed_state()
        if recovery=='release' and resumed['healthAt']>=expires:
            raise ValueError('original release was not proved before expiry')
        if recovery!='release' and resumed['healthAt']<expires:
            raise ValueError('original watchdog resumed before expiry')
        if resumed['request'] is not None or resumed['active'] is not None:
            raise ValueError('original consumer hold remained after '+recovery)
        recovered,_=original_api(drill,target,runtime,actor_keys,deadline)
        if recovered != api:
            raise ValueError('original API differs after '+recovery)
    return dict(original_supervisor_release_equal=True,
                original_supervisor_watchdog_equal=True,
                original_container_restart_equal=True,
                original_replacement_preserved_absolute_hold=True,
                original_consumer_hold_cleared=True)


def exercise(identity, payload, deadline):
    original = validate_original(payload)
    app, fields = payload['app'], payload['fields']
    transport = identity.Drill(app, fields)
    transport.deadline = min(deadline-90, time.monotonic()+1200)
    drill = Original()
    work_deadline = transport.deadline
    token = native.COMMAND_DEADLINE.set(work_deadline)
    results = []
    helpers = {'recurring.sh': Path(__file__).with_name('kopiur-pds-recurring').read_text(),
               'capture.cjs': Path(__file__).with_name('kopiur-pds-capture.cjs').read_text()}
    try:
        for image in (native.PDS, native.PYTHON, identity.IMAGE):
            native.docker('pull', '--platform', 'linux/amd64', image, timeout=600)
        for kind in ('nas', 'r2'):
            work, restored = drill.volume(kind+'-work'), drill.volume(kind+'-restore')
            tools = drill.volume(kind+'-consumer-tools')
            mover = drill.start('reader-'+kind, identity.IMAGE,
                                {work:'/work', restored:'/restored', identity.TOOLS:'/tools:ro'},
                                command=['sh','-c','/tools/sleep 1800'],
                                entrypoint='/tools/busybox', network='bridge')
            backend = ('rclone --remote-path=mnemosyne:kopiur-atproto-pds/production/ '
                       '--rclone-args=--config=/work/rclone.conf' if kind=='nas' else
                       's3 --bucket=kopiur-atproto-pds --prefix=production/ --endpoint='+
                       identity.ACCOUNT+'.r2.cloudflarestorage.com --region=auto')
            password = fields['NAS_KOPIA_PASSWORD' if kind=='nas' else 'R2_KOPIA_PASSWORD']
            connect = 'k repository connect '+backend+' --readonly >/dev/null\n'
            transport.stage = kind+'-original-read'
            shown = transport.exec(mover, connect+'k snapshot list --all --json\n',
                                   password=password)
            matches = [point for point in json.loads(shown.stdout)
                       if point['id'] == original['snapshots'][kind]]
            if len(matches) != 1:
                raise ValueError('exact original snapshot not found uniquely')
            snapshot = matches[0]
            source = snapshot['source']
            if source != {'userName':'atproto-pds','host':'services','path':'/pvc/atproto-pds'}:
                raise ValueError('original snapshot identity differs')
            if snapshot.get('incompleteReason'):
                raise ValueError('original snapshot incomplete')
            obj = snapshot['rootEntry']['obj']
            if not re.fullmatch(r'[A-Za-z0-9]+', obj):
                raise ValueError('unexpected original root')
            transport.exec(mover, 'k snapshot restore '+shlex.quote(obj)+' /restored/restored >/dev/null\n',
                           password=password)
            # No networked container remains mounted when original native secrets boot.
            native.docker('rm','-f',mover);drill.containers.remove(mover)
            # Source has root-owned PVC directories. Restore metadata only in
            # this owned tmpfs, with no host/Kubernetes mounts or network.
            checker = drill.start('check-'+kind, native.PYTHON, {restored:'/data',tools:'/consumer-tools'},
                                  command=['python3','-c','import time;time.sleep(1800)'],
                                  user='0:1000', capabilities=('CHOWN','FOWNER','DAC_OVERRIDE'))
            drill.native_python = {checker}
            checked_extract = ("try:\n exec(compile("+repr(EXTRACT)+", 'original-extract', 'exec'))\n"
                               "except Exception as error:\n import traceback\n"
                               " print(json.dumps({'extract_error':{'type':type(error).__name__,"
                               "'line':traceback.extract_tb(error.__traceback__)[-1].lineno}}))\n")
            receipt = json.loads(drill.python(checker, checked_extract, json.dumps(original['bundle_sha256'])))
            if 'extract_error' in receipt:
                raise ValueError('original extraction failed: '+json.dumps(receipt['extract_error']))
            runtime = json.loads(drill.python(checker,
                "import pathlib;print(pathlib.Path('/data/restored/.kopiur-pds/current/runtime.json').read_text())"))
            digest = hashlib.sha256(json.dumps(runtime,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if digest != original['runtime_sha256']:
                raise ValueError('original effective runtime escrow differs')
            drill.python(checker,
                "import json,pathlib,sys; files=json.load(sys.stdin); "
                "[(pathlib.Path('/consumer-tools')/name).write_text(code) for name,code in files.items()]",
                json.dumps(helpers))
            native.docker('rm','-f',checker);drill.containers.remove(checker)
            # Load original secrets inside Node from the memory-backed restore.
            # Docker configuration contains no original environment values.
            boot = ("const fs=require('fs'),p='/pds/.recovery-runtime.json';"
                    "Object.assign(process.env,JSON.parse(fs.readFileSync(p,'utf8')));"
                    "import('./index.ts').catch(()=>process.exit(1));")
            target = drill.start('boot-'+kind,native.PDS,{restored:'/pds',tools:'/kopiur:ro'},
                                 env={'QUIESCENCE_STATE_DIR':'/pds/.kopiur-pds/coordination'},
                                 command=['--','sh','/kopiur/recurring.sh','supervise',
                                          'node','--enable-source-maps','--eval',boot],
                                 entrypoint='dumb-init',volume_subpaths={restored:'point'})
            inspect=json.loads(native.docker('inspect',target))[0]
            configured_env=dict(entry.split('=',1) for entry in inspect['Config']['Env'])
            for secret_name in ('PDS_JWT_SECRET','PDS_ADMIN_PASSWORD',
                                'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX'):
                if secret_name in configured_env:
                    raise ValueError('original secret persisted in Docker configuration')
            if inspect['HostConfig']['LogConfig']['Type']!='none':
                raise ValueError('original native logging persisted')
            drill.ready(target,'/xrpc/_health')
            api,pages=original_api(drill,target,runtime,receipt['actor_keys'],work_deadline)
            signing = json.loads(native.docker('exec',target,'node','-e',
                "const fs=require('fs'),{createRequire}=require('module'),r=createRequire(require.resolve('@atproto/pds')),"
                "{Secp256k1Keypair,verifySignature}=r('@atproto/crypto');"
                "(async()=>{let keys=[];function walk(p){for(let n of fs.readdirSync(p)){let f=p+'/'+n,s=fs.lstatSync(f);"
                "if(s.isDirectory())walk(f);else if(n==='key')keys.push(f)}}walk('/pds');"
                "const msg=Buffer.from('K8S-92 offline actor signing proof');for(let p of keys){"
                "let k=await Secp256k1Keypair.import(fs.readFileSync(p)),sig=await k.sign(msg);"
                "if(!await verifySignature(k.did(),msg,sig)||await verifySignature(k.did(),Buffer.from('wrong'),sig))throw Error('signature');}"
                "console.log(JSON.stringify({offline_actor_signatures:keys.length}));})().catch(()=>process.exit(1));"))
            if signing['offline_actor_signatures']!=receipt['actor_keys']:
                raise ValueError('original actor signing inventory differs')
            receipt.update(consumer_recovery(drill,target,runtime,receipt['actor_keys'],work_deadline,api))
            receipt.update(signing)
            receipt.update(backend=kind,snapshot_id=original['snapshots'][kind],
                           original_backend_readonly=True,native_isolated_boot=True,
                           original_admin_authentication=True,restored_runtime_escrow_equal=True,
                           original_secrets_absent_from_docker_config=True,native_log_persistence=False,
                           networked_reader_removed_before_boot=True,network='none',
                           api=api,pages=pages)
            results.append(receipt)
            native.docker('rm','-f',target);drill.containers.remove(target)
        for key in ('bundle_sha256','tree_sha256','databases','actor_keys','api'):
            if results[0][key]!=results[1][key]:raise ValueError('independent originals differ: '+key)
        return {'app':app,'runner':os.environ['RUNNER_NAME'],'original_production_restore':True,
                'native_image':native.PDS,'mover_image':identity.IMAGE,'results':results,
                'production_repositories_untouched':True,'owned_restore_resources_removed':True}
    finally:
        # This is deliberately native cleanup only, never identity fixture purge.
        # Cleanup gets a separate bounded reserve, including failure during pulls.
        native.COMMAND_DEADLINE.set(min(deadline, time.monotonic()+90))
        try:
            drill.clean()
        finally:
            native.COMMAND_DEADLINE.reset(token)
