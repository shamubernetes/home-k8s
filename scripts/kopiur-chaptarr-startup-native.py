#!/usr/bin/env python3
"""Disconnected, UID568 qualification of Chaptarr's Books startup boundary."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'docker.io/chaptarr/chaptarr:0.9.965@sha256:bb00d67cbe1485d1cd61ef178362a29b42aa08f86b290841aab4b115ad2647a6'
HOLDER = ROOT / 'scripts/kopiur-chaptarr-startup-hold.sh'

def docker(*args, check=True, timeout=60):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('native fixture Docker operation failed')
    return result

def wait(predicate, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.25)
    raise RuntimeError('native fixture deadline expired')

def exercise(replacement):
    owner = uuid.uuid4().hex
    name = 'k92-chaptarr-' + owner
    uid = str(uuid.uuid4())
    attempted = False
    with tempfile.TemporaryDirectory(prefix='k92-chaptarr-') as temp:
        deadline_file = Path(temp) / 'deadline'
        deadline_file.write_text('0\n')
        deadline_file.chmod(420)
        try:
            attempted = True
            docker('create', '--name', name, '--label', 'k92.chaptarr-owner=' + owner, '--user=568:568', '--network=none', '--log-driver=none', '--memory=2g', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--tmpfs=/config:rw,uid=568,gid=568,size=128m', '--tmpfs=/tmp:rw,uid=568,gid=568,size=32m', '--tmpfs=/media:rw,uid=568,gid=568,size=8m', '--tmpfs=/ebooks:rw,uid=568,gid=568,size=8m', '-e', 'KOPIUR_POD_UID=' + uid, '-e', 'UMASK=002', '--entrypoint=/bin/bash', IMAGE, '-c', 'exec /bin/bash /hold.sh start "$(cat /deadline)"')
            docker('cp', str(HOLDER), name + ':/hold.sh')
            deadline = int(time.time()) + 40
            deadline_file.write_text(str(deadline) + '\n')
            docker('cp', str(deadline_file), name + ':/deadline')
            docker('start', name)

            def status():
                return docker('exec', name, '/bin/bash', '/hold.sh', 'status', check=False)
            wait(lambda: status().returncode == 0, 10)
            first = status().stdout.strip()
            if not len(first.splitlines()) == 7:
                raise RuntimeError('native qualification check failed')
            if not first.splitlines()[0] == uid:
                raise RuntimeError('native qualification check failed')

            def check(evidence):
                return docker('exec', name, '/bin/bash', '/hold.sh', 'check', evidence, '1', check=False).returncode == 0

            def no_writer():
                return docker('exec', name, '/bin/bash', '-c', 'set -e; test "$(id -u):$(id -g)" = 568:568; found=$(find /config /media /ebooks -mindepth 1 -print -quit) || exit 1; test -z "$found"; for p in /proc/[0-9]*/comm; do read -r c < "$p" || continue; test "$c" != dotnet || exit 1; done', check=False).returncode == 0
            if not (check(first) and no_writer()):
                raise RuntimeError('native qualification check failed')
            info = json.loads(docker('inspect', name).stdout)[0]
            if not info['Config']['User'] == '568:568':
                raise RuntimeError('native qualification check failed')
            if not info['HostConfig']['NetworkMode'] == 'none':
                raise RuntimeError('native qualification check failed')
            if not not info['HostConfig'].get('PortBindings'):
                raise RuntimeError('native qualification check failed')
            if not info['HostConfig']['LogConfig']['Type'] == 'none':
                raise RuntimeError('native qualification check failed')
            if not info['HostConfig']['CapDrop'] == ['ALL']:
                raise RuntimeError('native qualification check failed')
            current = first
            if replacement:
                docker('kill', '--signal=KILL', name)
                docker('start', name)
                wait(lambda: status().returncode == 0, 10)
                current = status().stdout.strip()
                if not (current != first and (not check(first))):
                    raise RuntimeError('native qualification check failed')
                if not current.splitlines()[5] == first.splitlines()[5]:
                    raise RuntimeError('native qualification check failed')
                if not (check(current) and no_writer()):
                    raise RuntimeError('native qualification check failed')
            while time.time() < deadline - 1:
                if not (check(current) and no_writer()):
                    raise RuntimeError('native qualification check failed')
                time.sleep(0.25)
            wait(lambda: status().returncode != 0, 5)
            if not not check(first):
                raise RuntimeError('native qualification check failed')
            wait(lambda: docker('exec', name, '/bin/bash', '-c', 'curl --fail --silent --max-time 2 http://127.0.0.1:8789/ping >/dev/null', check=False).returncode == 0, 120)
            command = docker('exec', name, '/bin/bash', '-c', 'tr "\\0" " " </proc/1/cmdline').stdout
            if not command.strip() == 'dotnet /app/Chaptarr.dll -nobrowser -data=/config':
                raise RuntimeError('native qualification check failed')
            if not docker('exec', name, 'id', '-u').stdout.strip() == '568':
                raise RuntimeError('native qualification check failed')
            return {'replacement': replacement, 'stale_identity_rejected': True, 'no_native_writer_during_hold': True, 'same_absolute_deadline': True, 'native_pid1_restored': True, 'uid': 568, 'http_status': 200, 'production_books_capture_qualified': False}
        finally:
            if attempted:
                result = docker('inspect', name, check=False)
                if result.returncode and 'No such object:' not in result.stderr and 'No such container:' not in result.stderr:
                    raise RuntimeError('owned cleanup inspection failed')
                if result.returncode == 0:
                    info = json.loads(result.stdout)[0]
                    if not info['Config']['Labels']['k92.chaptarr-owner'] == owner:
                        raise RuntimeError('native qualification check failed')
                    docker('rm', '-f', name)
                    remaining = docker('container', 'ls', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}')
                    if remaining.stdout.strip():
                        raise RuntimeError('owned container remains')

def main():
    docker('info')
    docker('pull', '--platform=linux/amd64', IMAGE, timeout=600)
    config = json.loads(docker('image', 'inspect', IMAGE).stdout)[0]['Config']
    if not config['Entrypoint'] == ['/entrypoint.sh']:
        raise RuntimeError('native qualification check failed')
    if not config['Cmd'] == ['dotnet', '/app/Chaptarr.dll', '-nobrowser', '-data=/config']:
        raise RuntimeError('native qualification check failed')
    print(json.dumps({'image': IMAGE, 'native_startup': [exercise(False), exercise(True)], 'owned_containers_removed': True, 'shared_books_fence_qualified': False}, sort_keys=True))
if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Chaptarr native startup qualification failed') from None
