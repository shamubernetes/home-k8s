#!/usr/bin/env python3
"""Qualify the existing Books startup guard on the pinned CWA-BDL image.

No credentials, Kubernetes, repositories or production Books. This is one
writer's prerequisite, not a complete shared-store fence.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/calibrain/calibre-web-automated-book-downloader:latest@sha256:4ef757349c3c7a7d90ba95b1e2a46ce4dd460a4776efbcf2aa1b1c476e7d5de5'
SOURCE = ROOT / 'kubernetes/apps/media/calibre-web-automated/app/kopiur-startup-hold.py'
SHIM = '#!/bin/sh\nexec /usr/bin/dumb-init -- /app/entrypoint.sh\n'
PYTHON = '/app/.venv/bin/python3'
spec = importlib.util.spec_from_file_location('cwa_fixture', ROOT / 'scripts/kopiur-cwa-startup-hold-native.py')
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
docker = fixture.docker
wait = fixture.wait


def marker(name):
    result = docker('exec', name, PYTHON, '/hold.py', 'evidence', ok=False)
    if result.returncode == 3:
        return None
    if result.returncode:
        raise RuntimeError('BDL evidence read failed')
    return json.loads(result.stdout)


def probe(name, evidence, minimum=1):
    return docker('exec', name, PYTHON, '/hold.py', 'check',
                  json.dumps(evidence, sort_keys=True), str(minimum), ok=False).returncode == 0


def no_native_writer(name):
    return docker('exec', name, PYTHON, '-c',
                  'from pathlib import Path; '
                  'assert not any(b"gunicorn" in p.read_bytes() for p in Path("/proc").glob("[0-9]*/cmdline") if p.parent.name not in ("self", str(__import__("os").getpid()))); '
                  'assert not any(Path("/config").iterdir()); '
                  'assert not any(Path("/books").iterdir())', ok=False).returncode == 0


def native_ready(name):
    return docker('exec', name, PYTHON, '-c',
                  'import urllib.request; '
                  'assert urllib.request.urlopen("http://127.0.0.1:8084/api/health",timeout=3).status==200',
                  ok=False).returncode == 0


def await_expiry(name, evidence):
    until = time.monotonic() + 65
    while time.monotonic() < until:
        current = marker(name)
        expired = time.time() >= evidence['deadline'] or time.monotonic() >= evidence['monotonic_end']
        if current is None:
            assert expired, 'BDL holder released early'
            return
        assert current == evidence
        absent = no_native_writer(name)
        expired_after = time.time() >= evidence['deadline'] or time.monotonic() >= evidence['monotonic_end']
        assert absent or expired_after, 'BDL native writer started early'
        time.sleep(0.25)
    raise RuntimeError('BDL did not release at fixed expiry')


def cleanup_pair(name, monitor, owner, attempted, monitor_attempted):
    try:
        if monitor_attempted:
            fixture.cleanup(monitor, owner)
    finally:
        if attempted:
            fixture.cleanup(name, owner)


def exercise(replacement):
    owner = uuid.uuid4().hex
    name = 'k92-cwa-startup-' + owner
    monitor = 'k92-cwa-startup-' + uuid.uuid4().hex
    pod_uid = str(uuid.uuid4())
    attempted = False
    monitor_attempted = False
    with tempfile.TemporaryDirectory(prefix='k92-bdl-') as temp:
        shim = Path(temp) / 'init'
        shim.write_text(SHIM)
        shim.chmod(0o755)
        try:
            assert not docker('container', 'ls', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').stdout.strip()
            attempted = True
            docker('create', '--name', name, '--label', 'k92.cwa-startup-owner=' + owner,
                   '--network=none', '--log-driver=none', '--memory=2g',
                   '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID',
                   '--cap-add=SETGID', '--cap-add=FOWNER', '--cap-add=DAC_OVERRIDE',
                   '--security-opt=no-new-privileges', '--tmpfs=/config:rw,size=128m',
                   '--tmpfs=/books:rw,size=8m', '-e', 'PUID=568', '-e', 'PGID=568',
                   '-e', 'TZ=UTC', '-e', 'FLASK_PORT=8084', '-e', 'INGEST_DIR=/books',
                   '-e', 'KOPIUR_POD_UID=' + pod_uid,
                   '--entrypoint=/bin/sh', IMAGE, '-ec',
                   'if [ ! -e /fixture-deadline ]; then '
                   '/app/.venv/bin/python3 -c "import time;print(int(time.time())+60)" >/fixture-deadline; fi; '
                   'exec /app/.venv/bin/python3 /hold.py start "$(cat /fixture-deadline)"')
            docker('cp', str(SOURCE), name + ':/hold.py')
            docker('cp', str(shim), name + ':/init')
            docker('start', name)
            wait(lambda: marker(name) is not None, seconds=15)
            first = marker(name)
            assert first and probe(name, first, 25) and no_native_writer(name)
            info = json.loads(docker('inspect', name).stdout)[0]
            assert info['HostConfig']['NetworkMode'] == 'none'
            assert not info['HostConfig'].get('PortBindings')
            assert info['HostConfig']['LogConfig']['Type'] == 'none'
            expected = first
            if replacement:
                docker('kill', '--signal=KILL', name)
                docker('start', name)
                wait(lambda: marker(name) is not None, seconds=10)
                second = marker(name)
                assert second and second['nonce'] != first['nonce']
                assert second['pid1_start'] != first['pid1_start']
                assert second['deadline'] == first['deadline']
                assert not probe(name, first)
                assert probe(name, second, 10) and no_native_writer(name)
                expected = second
            await_expiry(name, expected)
            assert not probe(name, first)
            wait(lambda: native_ready(name), seconds=300)
            docker('exec', name, PYTHON, '-c',
                   'from pathlib import Path; '
                   'assert Path("/proc/1/cmdline").read_bytes()==b"/usr/bin/dumb-init\\0--\\0/app/entrypoint.sh\\0"')
            # The stat-only monitor needs no writable Books mount. Prove the
            # same native image can stat a read-only Books mount and reach the
            # app on its disconnected network, with no added capabilities.
            monitor_attempted = True
            docker('run', '-d', '--name', monitor, '--label', 'k92.cwa-startup-owner=' + owner,
                   '--network=container:' + name, '--log-driver=none', '--read-only',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=64m',
                   '--tmpfs=/books:ro,size=8m', '--entrypoint=' + PYTHON, IMAGE,
                   '-c', 'import time; time.sleep(300)')
            docker('exec', monitor, PYTHON, '-c',
                   'import os,urllib.request; os.stat("/books"); '
                   'assert urllib.request.urlopen("http://127.0.0.1:8084/api/health",timeout=3).status==200; '
                   'rows=[r.split() for r in open("/proc/self/mountinfo")]; '
                   'assert any(r[4]=="/books" and "ro" in r[5].split(",") for r in rows)')
            return {'replacement_rejected': replacement, 'startup_without_native_writers': True,
                    'absolute_expiry_native_entrypoint_resumed': True, 'http_status': 200,
                    'monitor_readonly_books_health': True, 'production_capture_qualified': False}
        finally:
            cleanup_pair(name, monitor, owner, attempted, monitor_attempted)


def main():
    docker('info')
    docker('pull', '--platform=linux/amd64', IMAGE, timeout=600)
    receipts = [exercise(False), exercise(True)]
    print(json.dumps({'image': IMAGE, 'cwa_bdl_startup_hold': receipts,
                      'owned_containers_removed': True, 'shared_books_fence_qualified': False}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('BDL native startup hold qualification failed') from None
