#!/usr/bin/env python3
"""Exact CWA image startup-hold prerequisite on existing ARC DinD.

No Kubernetes, credentials, NAS, R2 or production content. Native /init and all
five installed services boot only after autonomous expiry. This qualifies one
writer's startup guard, never a running-service drain or the shared Books fence.
"""
import json
from pathlib import Path
import subprocess
import time
import uuid

IMAGE = 'ghcr.io/crocodilestick/calibre-web-automated:V3.1.4@sha256:577e846f104fd21453ef306eefb4a95dd95b3b9ddd2463a150944494284da0fd'
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'kubernetes/apps/media/calibre-web-automated/app/kopiur-startup-hold.py'
SERVICES = ('cwa-auto-zipper', 'cwa-ingest-service', 'metadata-change-detector',
            'svc-calibre-web-automated', 'svc-cron')


def docker(*args, ok=True, timeout=120):
    result = subprocess.run(['docker', *args], capture_output=True, timeout=timeout)
    if ok and result.returncode:
        raise RuntimeError('isolated CWA Docker operation failed')
    return result


def wait(check, seconds=300):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if check():return
        time.sleep(1)
    raise RuntimeError('isolated CWA deadline expired')


def marker(name):
    result = docker('exec', name, '/lsiopy/bin/python3', '/hold.py', 'evidence', ok=False)
    if result.returncode == 3:
        return None
    if result.returncode:
        raise RuntimeError('native evidence read failed, not confirmed absent')
    return json.loads(result.stdout)


def probe(name, expected, minimum=1):
    return docker('exec', name, '/lsiopy/bin/python3', '/hold.py', 'check',
                  json.dumps(expected, sort_keys=True), str(minimum), ok=False).returncode == 0


def no_native_services(name):
    return docker('exec', name, 'sh', '-c',
                  'test ! -d /run/service && test ! -e /config/app.db && '
                  'test ! -e /config/metadata.db', ok=False).returncode == 0


def native_ready(name):
    return docker('exec', name, '/lsiopy/bin/python3', '-c',
                  'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8083/",timeout=3).status==200',
                  ok=False).returncode == 0


def service_states(name):
    states = {}
    for service in SERVICES:
        result = docker('exec', name, 's6-svstat', '-o', 'up,wantedup,pid', '/run/service/' + service, ok=False)
        values = result.stdout.decode().split()
        if result.returncode or len(values) != 3 or not values[2].lstrip('-').isdigit():
            states[service] = ['unavailable']
        else:
            states[service] = values[:2] if int(values[2]) > 1 else ['no-native-pid']
    return states


def cleanup(name, owner):
    listing = ('container', 'ls', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}')
    ids = docker(*listing).stdout.decode().split()
    if not ids:
        return
    assert len(ids) == 1
    info = json.loads(docker('inspect', ids[0]).stdout)[0]
    assert info['Config'].get('Labels', {}).get('k92.cwa-startup-owner') == owner
    docker('rm', '-fv', ids[0])
    assert not docker(*listing).stdout.strip()


def await_expiry(name, expected):
    until = time.monotonic() + 45
    while time.monotonic() < until:
        current = marker(name)
        expired = time.time() >= expected['deadline'] or time.monotonic() >= expected['monotonic_end']
        if current is None:
            assert expired, 'native holder released before expiry'
            return
        assert current == expected
        # Account for expiry crossing during this Docker round trip, never
        # accept an early writer start while the holder is still valid.
        absent = no_native_services(name)
        expired_after = time.time() >= expected['deadline'] or time.monotonic() >= expected['monotonic_end']
        assert absent or expired_after, 'native services started before expiry'
        time.sleep(0.25)
    raise RuntimeError('isolated CWA did not release at expiry')


def exercise(replacement):
    owner = uuid.uuid4().hex
    name = 'k92-cwa-startup-' + owner
    pod_uid = str(uuid.uuid4())
    deadline = int(time.time()) + 40
    attempted = False
    try:
        assert not docker('container', 'ls', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').stdout.strip()
        attempted = True
        docker('create', '--name', name, '--label', 'k92.cwa-startup-owner=' + owner,
               '--network=none', '--log-driver=none',
               '--memory=2g', '--cap-drop=ALL', '--cap-add=CHOWN',
               '--cap-add=SETUID', '--cap-add=SETGID', '--cap-add=FOWNER',
               '--cap-add=DAC_OVERRIDE', '--security-opt=no-new-privileges',
               '--tmpfs=/config:rw,size=128m', '--tmpfs=/calibre-library:rw,size=128m',
               '--tmpfs=/cwa-book-ingest:rw,size=8m', '-e', 'PUID=568', '-e', 'PGID=568',
               '-e', 'TZ=UTC', '-e', 'KOPIUR_POD_UID=' + pod_uid,
               '--entrypoint=/lsiopy/bin/python3', IMAGE, '/hold.py', 'start', str(deadline))
        docker('cp', str(SOURCE), name + ':/hold.py')
        docker('start', name)
        wait(lambda:marker(name) is not None, seconds=15)
        first = marker(name)
        assert first and probe(name, first, minimum=15) and no_native_services(name)
        expected = first
        info = json.loads(docker('inspect', name).stdout)[0]
        assert info['HostConfig']['NetworkMode'] == 'none' and not info['HostConfig'].get('PortBindings')
        assert info['HostConfig']['LogConfig']['Type'] == 'none'
        assert not native_ready(name)
        if replacement:
            # Isolated native holder death, not a production signal or pause.
            docker('kill', '--signal=KILL', name)
            docker('start', name)
            wait(lambda:marker(name) is not None, seconds=10)
            second = marker(name)
            assert second and second['nonce'] != first['nonce']
            assert second['pid1_start'] != first['pid1_start']
            assert not probe(name, first)
            assert probe(name, second, minimum=5) and no_native_services(name)
            expected = second
        else:
            time.sleep(2)
            assert probe(name, first, minimum=5) and no_native_services(name)
        await_expiry(name, expected)
        assert not probe(name, first)
        wait(lambda:native_ready(name))
        states = {}
        def services_ready():
            states.update(service_states(name))
            return all(values == ['true', 'true'] for values in states.values())
        try:
            wait(services_ready)
        except RuntimeError:
            # Only service names and booleans, never native startup log content.
            raise RuntimeError('CWA native services not ready: ' + json.dumps(states, sort_keys=True)) from None
        return {'replacement_rejected': replacement, 'startup_without_native_writers': True,
                'absolute_expiry_native_init_resumed': True, 'http_status': 200,
                'native_service_states': states, 'production_capture_qualified': False}
    finally:
        if attempted:
            cleanup(name, owner)


def main():
    docker('info')
    docker('pull', '--platform=linux/amd64', IMAGE, timeout=600)
    receipts = [exercise(False), exercise(True)]
    print(json.dumps({'image': IMAGE, 'cwa_startup_hold': receipts,
                      'owned_containers_removed': True, 'shared_books_fence_qualified': False}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('CWA native startup hold qualification failed') from None
