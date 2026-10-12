#!/usr/bin/env python3
"""Qualify Bazarr's disposable Books child mount and independent rollback."""
import argparse
import json
import subprocess
import uuid

IMAGE = 'ghcr.io/home-operations/bazarr:1.6.2@sha256:17dc1cff22e99694ffbfb06555526cb47a66b362b4d6c982f06b557f7caae2bb'
LABEL = 'k92.bazarr-books-owner'
CONTRACTS = {
    'bazarr': {'image': IMAGE, 'uid': 568, 'gid': 568, 'writable': ['Movies', 'TV']},
    'listenarr': {
        'image': 'ghcr.io/therobbiedavis/listenarr:canary-0.2.47@sha256:c917f40d7a79f89e10ecef754cf4fd189f018a55ad561f3a8f95f6766e47086b',
        'uid': 0, 'gid': 0, 'writable': ['Audiobooks', 'Downloads'],
    },
}
PROBE = r'''
import errno,json,os,pathlib
uid=int(os.environ['EXPECT_UID']); gid=int(os.environ['EXPECT_GID'])
if os.getuid() != uid or os.getgid() != gid:
    raise RuntimeError('wrong native identity')
root=pathlib.Path('/media')
for name in json.loads(os.environ['WRITABLE_DIRS']):
    p=root/name/'disposable-write'
    p.write_text('disposable subtitle')
    p.rename(p.with_suffix('.tmp'))
    p.with_suffix('.tmp').unlink()
books=root/'Books'
if (books/'original').read_bytes() != b'original':
    raise RuntimeError('Books source changed')
readonly=os.environ['EXPECT_READONLY']=='true'
for name in ('Books/new', 'Books/original'):
    try:
        (root/name).write_bytes(b'original' if name.endswith('original') else b'probe')
    except OSError as exc:
        if not readonly or exc.errno != errno.EROFS:
            raise RuntimeError('unexpected write rejection') from None
    else:
        if readonly:
            raise RuntimeError('Books is writable')
if readonly:
    for operation in (lambda: (books/'original').unlink(), lambda: (books/'original').rename(books/'renamed')):
        try:
            operation()
        except OSError as exc:
            if exc.errno != errno.EROFS:
                raise RuntimeError('unexpected metadata rejection') from None
        else:
            raise RuntimeError('Books metadata is writable')
    alias=root/'books-link'
    alias.symlink_to('/media/Books')
    try:
        try:
            (alias/'via-link').write_text('probe')
        except OSError as exc:
            if exc.errno != errno.EROFS:
                raise RuntimeError('unexpected alias rejection') from None
        else:
            raise RuntimeError('symlink bypass')
    finally:
        alias.unlink()
print(json.dumps({'uid':uid,'gid':gid,'books_readonly':readonly,'writable_paths':json.loads(os.environ['WRITABLE_DIRS']),'books_original_equal':True}))
'''


def docker(*args, check=True, timeout=60):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Bazarr owned Docker operation failed')
    return result


def inspect_owned(kind, name, owner):
    result = docker(kind, 'inspect', name, check=False)
    if result.returncode:
        if 'no such ' + kind in result.stderr.lower():
            return False
        raise RuntimeError('owned resource inspection failed')
    info = json.loads(result.stdout)[0]
    labels = info.get('Labels') if kind == 'volume' else info['Config']['Labels']
    if labels.get(LABEL) != owner:
        raise RuntimeError('resource ownership mismatch')
    return True


def cleanup(containers, volumes, owner):
    failed = False
    for kind, names in (('container', containers), ('volume', volumes)):
        for name in reversed(names):
            try:
                if inspect_owned(kind, name, owner):
                    if kind == 'container':
                        docker('rm', '-f', name)
                    else:
                        docker('volume', 'rm', name)
                if inspect_owned(kind, name, owner):
                    failed = True
            except Exception:
                failed = True
    if failed:
        raise RuntimeError('owned cleanup incomplete')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', choices=sorted(CONTRACTS), default='bazarr')
    app = parser.parse_args().app
    contract = CONTRACTS[app]
    image = contract['image']
    docker('info')
    docker('pull', '--platform=linux/amd64', image, timeout=600)
    owner = uuid.uuid4().hex
    media = 'k92-' + app + '-' + owner + '-media'
    containers = []
    volumes = []
    receipts = []
    try:
        for volume in (media,):
            volumes.append(volume)
            docker('volume', 'create', '--label', LABEL + '=' + owner, volume)
        seed = 'k92-' + app + '-' + owner + '-seed'
        containers.append(seed)
        docker('run', '--name', seed, '--label', LABEL + '=' + owner,
               '--user=0:0', '--network=none', '--log-driver=none', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--read-only',
               '--mount', 'type=volume,src=' + media + ',dst=/media',
               '--entrypoint=python3', image, '-c',
               'import pathlib,os; r=pathlib.Path("/media"); os.chmod(r,0o777); '
               '[(r/n).mkdir() for n in ("Movies","TV","Audiobooks","Downloads","Books")]; '
               '[os.chmod(r/n,0o777) for n in ("Movies","TV","Audiobooks","Downloads","Books")]; '
               '(r/"Books"/"original").write_bytes(b"original"); os.chmod(r/"Books"/"original",0o666)')
        for readonly in (True, False):
            name = 'k92-' + app + '-' + owner + ('-guarded' if readonly else '-rollback')
            containers.append(name)
            args = ['create', '--name', name, '--label', LABEL + '=' + owner,
                    '--user=' + str(contract['uid']) + ':' + str(contract['gid']), '--network=none', '--log-driver=none',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                    '--memory=512m', '--mount', 'type=volume,src=' + media + ',dst=/media',
                    '-e', 'EXPECT_READONLY=' + str(readonly).lower(),
                    '-e', 'EXPECT_UID=' + str(contract['uid']),
                    '-e', 'EXPECT_GID=' + str(contract['gid']),
                    '-e', 'WRITABLE_DIRS=' + json.dumps(contract['writable'])]
            if readonly:
                args += ['--mount', 'type=volume,src=' + media + ',dst=/media/Books,volume-subpath=Books,readonly']
            args += ['--entrypoint=python3', image, '-c', 'import time; time.sleep(300)']
            docker(*args)
            docker('start', name)
            for replacement in (False, True):
                if replacement:
                    previous_id = json.loads(docker('inspect', name).stdout)[0]['Id']
                    if not inspect_owned('container', name, owner):
                        raise RuntimeError('replacement source missing')
                    docker('rm', '-f', name)
                    docker(*args)
                    docker('start', name)
                info = json.loads(docker('inspect', name).stdout)[0]
                if replacement and info['Id'] == previous_id:
                    raise RuntimeError('container was not replaced')
                host = info['HostConfig']
                if host['NetworkMode'] != 'none' or host.get('PortBindings') or host['LogConfig']['Type'] != 'none':
                    raise RuntimeError('fixture isolation failed')
                mounts = {m['Destination']: m for m in info['Mounts']}
                if mounts['/media']['Name'] != media or not mounts['/media']['RW']:
                    raise RuntimeError('media mount mismatch')
                if readonly and (mounts['/media/Books']['Name'] != media or mounts['/media/Books']['RW']):
                    raise RuntimeError('Books child mount mismatch')
                receipt = json.loads(docker('exec', name, 'python3', '-c', PROBE).stdout)
                receipt['replacement'] = replacement
                receipts.append(receipt)
    finally:
        cleanup(containers, volumes, owner)
    print(json.dumps({'app': app, 'image': image, 'receipts': receipts, 'owned_cleanup_verified': True,
                      'production_books_capture_qualified': False}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Bazarr Books mount qualification failed') from None
