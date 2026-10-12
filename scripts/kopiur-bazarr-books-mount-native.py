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
    'radarr': {
        'image': 'ghcr.io/home-operations/radarr:6.4.4.10685@sha256:be53998a2d39cfa3c3315b70c7509a6a1f2a10c3aee9337653efc9f4c970430e',
        'uid': 568, 'gid': 568, 'writable': ['Movies'], 'shell_probe': True,
    },
    'sonarr': {
        'image': 'ghcr.io/home-operations/sonarr:4.0.20.3012@sha256:1f19eb5e0f421418c1a956bbe01310a0141423afe28bd9a4b1dcb8629ff2bce2',
        'uid': 568, 'gid': 568, 'writable': ['TV'], 'shell_probe': True,
    },
    'whisparr': {
        'image': 'ghcr.io/shamubernetes/whisparr-eros:3.3.7-release.979@sha256:9002dd8e29471d65f0b1a03802a3bf48bb58212435a053bbbe8971f2d2253194',
        'uid': 568, 'gid': 568, 'writable': ['Adult'], 'shell_probe': True,
    },
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


SHELL_SEED = r'''
set -euo pipefail
chmod 777 /media
for path in Movies TV Adult Audiobooks Downloads Books; do
    mkdir "/media/$path"
    chmod 777 "/media/$path"
done
printf original > /media/Books/original
chmod 666 /media/Books/original
'''
SHELL_PROBE = r'''
set -euo pipefail
export LC_ALL=C
[[ $(id -u) == "$EXPECT_UID" && $(id -g) == "$EXPECT_GID" ]]
root=/media
directory=${SHELL_WRITABLE_DIR:-Movies}
case "$directory" in Movies|TV|Adult) ;; *) exit 1 ;; esac
[[ $(cat "$root/Books/original") == original ]]
before=$(sha256sum "$root/Books/original")
printf 'disposable media' > "$root/$directory/disposable-write"
mv "$root/$directory/disposable-write" "$root/$directory/disposable-write.tmp"
rm "$root/$directory/disposable-write.tmp"
reject_readonly() {
    local output
    if output=$("$@" 2>&1); then
        printf 'Books is writable\n' >&2
        return 1
    fi
    [[ $output == *'Read-only file system'* ]]
}
if [[ $EXPECT_READONLY == true ]]; then
    reject_readonly bash -c 'printf probe > "$1/Books/new"' _ "$root"
    reject_readonly bash -c 'printf original > "$1/Books/original"' _ "$root"
    reject_readonly rm "$root/Books/original"
    reject_readonly mv "$root/Books/original" "$root/Books/renamed"
    ln -s "$root/Books" "$root/books-link"
    trap 'rm -f "$root/books-link"' EXIT
    reject_readonly bash -c 'printf probe > "$1/books-link/via-link"' _ "$root"
    rm "$root/books-link"
    trap - EXIT
elif [[ $EXPECT_READONLY == false ]]; then
    printf original > "$root/Books/original"
    printf probe > "$root/Books/new"
    mv "$root/Books/new" "$root/Books/renamed"
    rm "$root/Books/renamed"
else
    exit 1
fi
[[ $(sha256sum "$root/Books/original") == "$before" ]]
printf '{"uid":%s,"gid":%s,"books_readonly":%s,"writable_paths":["%s"],"books_original_equal":true}\n' "$EXPECT_UID" "$EXPECT_GID" "$EXPECT_READONLY" "$directory"
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
        shell_probe = contract.get('shell_probe', False)
        seed_command = ['--entrypoint=bash', image, '-c', SHELL_SEED] if shell_probe else [
            '--entrypoint=python3', image, '-c',
            'import pathlib,os; r=pathlib.Path("/media"); os.chmod(r,0o777); '
            '[(r/n).mkdir() for n in ("Movies","TV","Audiobooks","Downloads","Books")]; '
            '[os.chmod(r/n,0o777) for n in ("Movies","TV","Audiobooks","Downloads","Books")]; '
            '(r/"Books"/"original").write_bytes(b"original"); os.chmod(r/"Books"/"original",0o666)']
        docker('run', '--name', seed, '--label', LABEL + '=' + owner,
               '--user=0:0', '--network=none', '--log-driver=none', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--read-only',
               '--mount', 'type=volume,src=' + media + ',dst=/media', *seed_command)
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
            if shell_probe:
                if len(contract['writable']) != 1:
                    raise RuntimeError('shell probe requires one writable directory')
                args += ['-e', 'SHELL_WRITABLE_DIR=' + contract['writable'][0]]
                args += ['--entrypoint=bash', image, '-c', 'exec sleep 300']
            else:
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
                probe_command = ['bash', '-c', SHELL_PROBE] if shell_probe else ['python3', '-c', PROBE]
                receipt = json.loads(docker('exec', name, *probe_command).stdout)
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
