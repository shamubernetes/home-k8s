#!/usr/bin/env python3
"""Read retained Grimmory originals and boot them without any external network.

Production bytes remain in ARC-owned tmpfs. Reuse the existing private identity
channel and native Docker mechanics; never mutate or purge either repository.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import time

import kopiur_pds_native as native
from kopiur_pds_original import Original

MARIA = 'docker.io/linuxserver/mariadb:11.8.8@sha256:fa63c6ff84bd6bab0a188d68e1b4cd1e4e1b34c22101fc52d544a53dfd502d45'
GRIMM = 'docker.io/grimmory/grimmory:v3.5.0@sha256:83597bf02da48a8ec4042fd4d4f58b9631f5b131d6dee7d2fcc5cf3f2eec2526'
HELPERS = Path(__file__).resolve().parent.parent / 'kubernetes/apps/media/grimmory/app'


def validate_original(payload):
    original = payload['original']
    required = {'snapshots', 'generation', 'sql_sha256', 'runtime_sha256', 'data_sha256'}
    if set(original) not in (required, required | {'books_sha256'}):
        raise ValueError('unexpected Grimmory original contract')
    points = original['snapshots']
    if set(points) != {'nas', 'r2'} or points['nas'] == points['r2'] or not all(
            isinstance(x, str) and re.fullmatch(r'[0-9a-f]{32}', x) for x in points.values()):
        raise ValueError('exact distinct original IDs required')
    for key in ('sql_sha256', 'runtime_sha256', 'data_sha256', *(['books_sha256'] if 'books_sha256' in original else [])):
        if not re.fullmatch(r'[0-9a-f]{64}', original[key]):
            raise ValueError('exact original digest required')
    if not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', original['generation']):
        raise ValueError('exact original generation required')
    return original


class Restore(Original):
    def volume(self, suffix):
        # Disposable proof capacity, not a production retention or recovery policy.
        name = self.prefix + '-' + suffix
        native.docker('volume', 'create', '--label', 'k8s92.pds='+self.prefix,
                      '--driver', 'local', '--opt', 'type=tmpfs', '--opt', 'device=tmpfs',
                      '--opt', 'o=size=512m,uid=568,gid=568,mode=0700', name)
        self.volumes.append(name)
        self.start('keep-'+suffix, native.PYTHON, {name:'/memory'},
                   command=['python3','-c','import time;time.sleep(1800)'], user='568:568')
        return name

    def start_consumer(self, kind, database, appdata, tools, books=None):
        if books is None:
            return self.start('app-'+kind, GRIMM, {appdata:'/app/data', tools:'/tools:ro'},
                command=['/tools/boot.sh'], entrypoint='sh',
                network='container:'+database, user='568:568')
        # The same restored volume must appear twice, with bookdrop as a subpath.
        # Keep this special mount contract local rather than changing other drills.
        name = self.prefix + '-app-' + kind
        self.containers.append(name)
        native.docker('run', '-d', '--name', name, '--log-driver=none',
            '--network', 'container:'+database, '--user', '568:568',
            '--label', 'k8s92.pds='+self.prefix, '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,uid=568,gid=568',
            '-v', appdata+':/app/data', '-v', tools+':/tools:ro', '-v', books+':/books:ro',
            '--mount', 'type=volume,src='+books+',dst=/bookdrop,volume-nocopy,volume-subpath=bookdrop,readonly',
            '--entrypoint', 'sh', GRIMM, '/tools/boot.sh')
        return name


CHECK = '''import hashlib,json,pathlib,sys,tarfile,os,shlex
expected=json.load(sys.stdin);root=pathlib.Path('/data/restored')
b=root/'mariadb-config/kopiur';r=b/'runtime'
for p in [b,r,*b.iterdir(),*r.iterdir()]:assert not p.is_symlink()
def digest(p):return hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
assert digest(b/'grimmory.sql')==expected['sql_sha256']
assert digest(r/'runtime.sha256')==expected['runtime_sha256']
assert (b/'COMPLETE').read_text().strip()==expected['generation']
m=dict(line.split('=',1)for line in (b/'manifest.txt').read_text().splitlines())
assert m['format']=='mariadb-logical-runtime-v2' and m['runtime_generation']==expected['generation']
assert m['sql_sha256']==expected['sql_sha256'] and m['runtime_manifest_sha256']==expected['runtime_sha256']
assert m['base_tables']=='73'
checks={}
for line in (r/'runtime.sha256').read_text().splitlines():
 h,n=line.split('  ',1);assert n not in checks;checks[n]=h
roster={'application.env0','application.argv0','app-data.tar','process-start.txt','runtime.txt'}
if 'books_sha256' in expected:roster.update({'books.tar','books.txt'})
assert set(checks)==roster
assert all(digest(r/n)==h for n,h in checks.items())
def unpack(filename,point):
 with tarfile.open(filename) as archive:
  members=archive.getmembers();seen=set();rows=[]
  assert sum(m.size for m in members)<512*1024*1024
  for member in members:
   p=pathlib.PurePosixPath(member.name);name=str(p)
   assert not p.is_absolute() and '..' not in p.parts and name not in seen;seen.add(name)
   assert member.isdir() or member.isfile()
   assert not member.mode & 0o4000 and (member.isdir() or not member.mode & 0o2000)
   data=archive.extractfile(member) if member.isfile() else None
   rows.append([name,member.mode,member.uid,member.gid,hashlib.file_digest(data,'sha256').hexdigest() if data else None])
  # Original numeric IDs must survive a different recovery image's name database.
  archive.extractall(point,filter='fully_trusted',numeric_owner=True)
 return members,rows
source=root/'grimmory-data';point=pathlib.Path('/appdata');point.mkdir(exist_ok=True)
unpack(r/'app-data.tar',point)
def tree(root):
 rows=[]
 for p in [root,*sorted(root.rglob('*'))]:
  s=p.lstat();assert not p.is_symlink() and (p.is_file()or p.is_dir())
  rows.append([str(p.relative_to(root)),s.st_mode&0o7777,s.st_uid,s.st_gid,digest(p)if p.is_file()else None])
 return rows
before,after=tree(source),tree(point);assert before==after,'original app tree metadata or bytes differ'
receipt={'base_tables':73,'runtime_sha256':checks,'app_entries':len(before),
 'app_tree_sha256':hashlib.sha256(json.dumps(before).encode()).hexdigest()}
if 'books_sha256' in expected:
 assert digest(r/'books.tar')==expected['books_sha256']
 runtime=dict(line.split('=',1)for line in (r/'runtime.txt').read_text().splitlines())
 assert runtime['format']=='grimmory-runtime-books-v2'
 assert (r/'books.txt').read_text()=='format=grimmory-books-v1\\nsource=/books\\nbookdrop_subpath=bookdrop\\nconsistency=repeated-byte-metadata-stability-not-writer-fenced\\n'
 books=pathlib.Path('/bookdata');books.mkdir(exist_ok=True)
 members,rows=unpack(r/'books.tar',books)
 assert sorted(rows)==sorted(tree(books)), 'original Books bytes or metadata differ'
 assert (books/'bookdrop').is_dir() and not (books/'bookdrop').is_symlink()
 receipt.update(books_entries=len(rows),books_files=sum(m.isfile()for m in members),
  books_archive_sha256=expected['books_sha256'],
  books_tree_sha256=hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),
  bookdrop_subtree_restored=True,books_original_bytes_metadata_equal=True,
  shared_writer_fencing_qualified=False)
print(json.dumps(receipt))
'''

def checked_original(drill, checker, expected):
    # The checker may fail on credential-bearing files. Export only a source-line
    # number, never exception messages, paths, original bytes or tracebacks.
    program = 'import json,traceback\ntry:\n exec(compile('+repr(CHECK)+',"<grimmory-original-check>","exec"))\nexcept Exception as error:\n frames=traceback.extract_tb(error.__traceback__)\n line=next((f.lineno for f in reversed(frames) if f.filename=="<grimmory-original-check>"),0)\n print(json.dumps({"check_failed_line":line}))\n'
    result=json.loads(drill.python(checker,program,json.dumps(expected)))
    if 'check_failed_line' in result:
        line=result['check_failed_line']
        if type(line) is not int or not 0<=line<=len(CHECK.splitlines()):
            raise RuntimeError('invalid original checker diagnostic')
        raise RuntimeError('original integrity checker failed at source line '+str(line))
    return result


BOOT = '''import json,pathlib,sys,shlex,os
c=json.load(sys.stdin);r=pathlib.Path('/data/restored/mariadb-config/kopiur/runtime')
env={}
for item in (r/'application.env0').read_bytes().split(b'\\0'):
 if item:
  k,v=item.decode().split('=',1);assert k not in env and k.isidentifier();env[k]=v
args=[x.decode()for x in (r/'application.argv0').read_bytes().split(b'\\0')if x]
assert args and args[0].endswith('java') and '-jar' in args
# Only disposable connection/auth inputs change. Original escrow stays immutable.
env.update(DATABASE_URL='jdbc:mariadb://127.0.0.1:3306/grimmory',DATABASE_USERNAME='grimmory',
 DATABASE_PASSWORD=c['password'],FORCE_DISABLE_OIDC='true')
code='#!/bin/sh\\nset -eu\\ncd /app\\n'+''.join('export '+k+'='+shlex.quote(v)+'\\n'for k,v in env.items())
code+='exec '+shlex.join(args)+'\\n'
p=pathlib.Path('/tools/boot.sh');p.write_text(code);os.chown(p,568,568);p.chmod(0o500)
for name,code in c['helpers'].items():(pathlib.Path('/tools')/name).write_text(code)
print(json.dumps({'original_runtime_hash':__import__('hashlib').sha256((r/'application.env0').read_bytes()).hexdigest(),
 'disposable_boot_hash':__import__('hashlib').sha256(p.read_bytes()).hexdigest()}))
'''


def verify_network(info, database_id, *, database=False):
    host = info['HostConfig']
    assert not host.get('PortBindings') and host['LogConfig']['Type'] == 'none'
    # Docker normalizes a container network reference to the full container ID.
    assert host['NetworkMode'] == ('none' if database else 'container:' + database_id)


def data_digest(database):
    cmd = ('MYSQL_PWD="$MYSQL_PASSWORD" mariadb-dump --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" '
           '--skip-comments --compact --no-create-info --skip-add-locks --skip-disable-keys '
           '--skip-extended-insert --order-by-primary --hex-blob grimmory')
    deadline = native.COMMAND_DEADLINE.get()
    assert deadline is not None
    remaining = deadline - time.monotonic()
    result = subprocess.run(['docker','exec',database,'s6-setuidgid','abc','bash','-c',cmd],
                            capture_output=True,timeout=min(60,remaining))
    if result.returncode:
        raise RuntimeError('native data equality read failed; private output withheld')
    return hashlib.sha256(result.stdout).hexdigest()


def wait(check):
    deadline = native.COMMAND_DEADLINE.get()
    assert deadline is not None
    end = min(deadline, time.monotonic()+300)
    while time.monotonic() < end:
        try:
            if check():return
        except RuntimeError:
            pass
        time.sleep(2)
    raise TimeoutError('original native readiness expired')


def verify_consumer_books(app, expected):
    info=json.loads(native.docker('inspect',app))[0]
    mounts={m['Destination']:m for m in info['Mounts']}
    assert all(path in mounts and not mounts[path]['RW'] for path in ('/books','/bookdrop'))
    assert mounts['/books']['Name']==mounts['/bookdrop']['Name']
    program = '''import hashlib,json,os,pathlib,sys
root=pathlib.Path('/books');rows=[]
assert os.path.samefile(root/'bookdrop','/bookdrop')
for p in [root,*sorted(root.rglob('*'))]:
 s=p.lstat();assert not p.is_symlink() and (p.is_file() or p.is_dir())
 h=hashlib.file_digest(p.open('rb'),'sha256').hexdigest() if p.is_file() else None
 rows.append([str(p.relative_to(root)),s.st_mode&0o7777,s.st_uid,s.st_gid,h])
assert hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()==sys.argv[1]
'''
    native.docker('run', '--rm', '--log-driver=none', '--network=none',
        '--user', '568:568', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--volumes-from', app+':ro',
        native.PYTHON, 'python3', '-c', program, expected)


def recreate_consumer(drill, app, database, kind, appdata, tools, expected, books=None):
    # Docker replaces a restarted container's network namespace. Remove the
    # disposable consumer first and join the new namespace when recreating it.
    native.docker('rm','-f',app);drill.containers.remove(app)
    native.docker('restart',database)
    wait(lambda:data_digest(database)==expected)
    app=drill.start_consumer(kind,database,appdata,tools,books)
    wait(lambda:drill.http(app,'/api/v1/healthcheck',port=6060) is not None)
    if data_digest(database)!=expected:raise ValueError('data differs after consumer recreation')
    return app


def exercise(identity, payload, deadline):
    original = validate_original(payload)
    transport = identity.Drill('grimmory', payload['fields'])
    transport.deadline = min(deadline-90,time.monotonic()+1200)
    drill = Restore()
    token = native.COMMAND_DEADLINE.set(transport.deadline)
    results = []
    try:
        for image in (native.PYTHON, MARIA, GRIMM, identity.IMAGE):
            native.docker('pull','--platform','linux/amd64',image,timeout=600)
        for kind in ('nas','r2'):
            work, restored, appdata, tools, dbdata = [drill.volume(kind+'-'+s) for s in ('work','restored','app','tools','database')]
            checker_mounts={restored:'/data',appdata:'/appdata',tools:'/tools'}
            books=None
            if 'books_sha256' in original:
                books=drill.volume(kind+'-books')
                checker_mounts[books]='/bookdata'
            reader = drill.start('reader-'+kind,identity.IMAGE,
                {work:'/work',restored:'/restored',identity.TOOLS:'/tools:ro'},
                command=['sh','-c','/tools/sleep 1800'],entrypoint='/tools/busybox',network='bridge',user='568:568')
            backend = ('rclone --remote-path=mnemosyne:kopiur-grimmory/production/ --rclone-args=--config=/work/rclone.conf'
                if kind=='nas' else 's3 --bucket=kopiur-grimmory --prefix=production/ --endpoint='+identity.ACCOUNT+'.r2.cloudflarestorage.com --region=auto')
            password=payload['fields']['NAS_KOPIA_PASSWORD' if kind=='nas' else 'R2_KOPIA_PASSWORD']
            connect='k repository connect '+backend+' --readonly >/dev/null\n'
            transport.stage=kind+'-original-read'
            points=json.loads(transport.exec(reader,connect+'k snapshot list --all --json\n',password=password).stdout)
            matches=[p for p in points if p['id']==original['snapshots'][kind]]
            if len(matches)!=1:raise ValueError('original manifest absent or ambiguous')
            point=matches[0]
            if point['source']!={'userName':'grimmory','host':'media','path':'/pvc/grimmory'} or point.get('incompleteReason'):
                raise ValueError('original identity or completeness differs')
            obj=point['rootEntry']['obj']
            if not re.fullmatch(r'[A-Za-z0-9]+',obj):raise ValueError('invalid original root')
            transport.exec(reader,'k snapshot restore '+shlex.quote(obj)+' /restored/restored >/dev/null\n',password=password)
            native.docker('rm','-f',reader);drill.containers.remove(reader)
            # Restore Books setgid directories owned by gid100 while checker is gid568.
            checker=drill.start('checker-'+kind,native.PYTHON,checker_mounts,
                command=['python3','-c','import time;time.sleep(1800)'],user='0:568',capabilities=('CHOWN','FOWNER','DAC_OVERRIDE','FSETID'))
            drill.native_python={checker}
            receipt=checked_original(drill,checker,original)
            fresh_password=secrets.token_hex(24)
            receipt.update(json.loads(drill.python(checker,BOOT,json.dumps({'password':fresh_password,
                'helpers':{n:(HELPERS/n).read_text() for n in ('restore.sh','runtime.sh')}}))))
            native.docker('rm','-f',checker);drill.containers.remove(checker)
            # Only generated isolated DB credentials enter Docker config. Original
            # runtime stays in memory-backed tools and native logging is disabled.
            database=drill.prefix+'-database-'+kind;drill.containers.append(database)
            env={'MYSQL_ROOT_PASSWORD':secrets.token_hex(24),'MYSQL_USER':'grimmory','MYSQL_PASSWORD':fresh_password,
                 'MYSQL_DATABASE':'grimmory','PUID':'568','PGID':'568','TZ':'UTC'}
            args=['run','-d','--name',database,'--label','k8s92.pds='+drill.prefix,'--log-driver=none','--network=none',
                  '-v',dbdata+':/config','-v',restored+':/original:ro','-v',tools+':/tools:ro']
            for key in env:args+=['-e',key]
            native.docker(*args,MARIA,environ=env)
            wait(lambda:native.docker('exec',database,'bash','-c','MYSQL_PWD="$MYSQL_PASSWORD" mariadb --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" -e "SELECT 1"')!='')
            native.docker('exec','-e','K8S92_ISOLATED_RESTORE=YES',database,'bash','/tools/restore.sh',
                          '/original/restored/mariadb-config/kopiur')
            if data_digest(database)!=original['data_sha256']:raise ValueError('original native data digest differs')
            app=drill.start_consumer(kind,database,appdata,tools,books)
            wait(lambda:drill.http(app,'/api/v1/healthcheck',port=6060) is not None)
            if books:verify_consumer_books(app,receipt['books_tree_sha256'])
            native.docker('restart',app)
            wait(lambda:drill.http(app,'/api/v1/healthcheck',port=6060) is not None)
            if books:verify_consumer_books(app,receipt['books_tree_sha256'])
            if data_digest(database)!=original['data_sha256']:raise ValueError('data differs after native app restart')
            app=recreate_consumer(drill,app,database,kind,appdata,tools,original['data_sha256'],books)
            if books:
                verify_consumer_books(app,receipt['books_tree_sha256'])
                receipt['native_consumer_books_mounts_equal']=True
            receipt.update(native_database_restart_equal=True,native_consumer_recreation_equal=True)
            database_id = json.loads(native.docker('inspect',database))[0]['Id']
            assert re.fullmatch(r'[0-9a-f]{64}', database_id)
            for c in (app,database):
                info=json.loads(native.docker('inspect',c))[0]
                verify_network(info, database_id, database=c==database)
            receipt.update(backend=kind,snapshot_id=point['id'],start_time=point['startTime'],end_time=point['endTime'],
                original_data_sha256=original['data_sha256'],original_runtime_config_equal=True,
                native_health=True,native_container_restart_equal=True,networked_reader_removed_before_boot=True,
                native_database_network='none',production_repository_readonly=True)
            results.append(receipt)
            for c in (app,database):native.docker('rm','-f',c);drill.containers.remove(c)
        for key in ('runtime_sha256','app_tree_sha256','original_data_sha256','original_runtime_hash','start_time','end_time'):
            if results[0][key]!=results[1][key]:raise ValueError('independent originals differ: '+key)
        if 'books_sha256' in original:
            for key in ('books_entries','books_files','books_archive_sha256','books_tree_sha256'):
                if results[0][key]!=results[1][key]:raise ValueError('independent Books originals differ: '+key)
    finally:
        native.COMMAND_DEADLINE.set(min(deadline,time.monotonic()+90))
        try:drill.clean()
        finally:native.COMMAND_DEADLINE.reset(token)
    return {'app':'grimmory','runner':os.environ['RUNNER_NAME'],'original_production_restore':True,'results':results,
        'production_repositories_untouched':True,'owned_restore_resources_removed':True,
        'shared_books_bookdrop_qualified':False,'legacy_retirement_qualified':False}
