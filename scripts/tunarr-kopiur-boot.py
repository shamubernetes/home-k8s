#!/usr/bin/env python3
"""Boot a COPY of a promoted Tunarr restore in a network-none Docker container."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

IMAGES = {
    'stable-1.3': 'ghcr.io/chrisbenincasa/tunarr:1.3.15@sha256:ae8ec490459e773571b277e2f64248605c1ea64d49ee4d26aa01741409d60369',
    'retained-2026.9': 'ghcr.io/chrisbenincasa/tunarr:2026.9.0@sha256:af8790eb57dc3f3a4e9905859b789ec1fa519c5c537c2d0a996e25c22fe80d33',
}
ROOT = '/config/tunarr'


def docker(*args, check=True, timeout=120):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Docker operation failed; details withheld')
    return result


def node(name, code, check=True):
    return docker('exec', name, 'node', '--disable-warning=ExperimentalWarning', '-e', code, check=check)


def module():
    spec = importlib.util.spec_from_file_location('tunarr_restore', Path(__file__).with_name('tunarr-kopiur-restore.py'))
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def expected_api_program_counts(source_root, record, restore):
    # Called only after verification of the capture and promoted hashes.
    # Both pinned channel converters count content occurrences, not SQL rows.
    return {Path(name).stem: sum(item['type'] == 'content' for item in restore.load(source_root / name)['items'])
            for name in record['files'] if name.startswith('channel-lineups/')}


def verify_boot_counts(actual, record, api_program_counts):
    expected_sql = {k: record['counts'][k] for k in ('channel', 'program', 'channel_programs', 'program_media_file')}
    if (actual['counts'] != expected_sql or actual['apiChannels'] != expected_sql['channel']
            or actual['apiProgramCounts'] != api_program_counts
            or actual['apiPrograms'] != sum(api_program_counts.values())):
        raise ValueError('restored application counts mismatch')


def boot(source, version, timeout=120):
    restore = module()
    source = Path(source).absolute()
    restore.tree(source)
    receipt = restore.load(source / '.tunarr-restore.json')
    manifest, _ = restore.verify(source, receipt['generation'], *receipt['capture_window'])
    if manifest['records'] != receipt['records']:
        raise ValueError('receipt mismatch')
    record = next(r for r in receipt['records'] if r['artifact'] == version)
    source_root = source / record['source']
    # Verify promoted authoritative bytes, not just the archived capture.
    for name, digest in record['files'].items():
        restore.regular(source_root / name)
        if restore.digest(source_root / name) != digest:
            raise ValueError('promoted state mismatch')
    api_program_counts = expected_api_program_counts(source_root, record, restore)
    name = 'k8s92-tunarr-boot-' + uuid.uuid4().hex[:12]
    image = IMAGES[version]
    docker('image', 'inspect', image)
    database_path = ROOT + ('/stable-1.3' if version == 'stable-1.3' else '')
    try:
        docker('run', '-d', '--name', name, '--platform', 'linux/amd64', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '568:568',
               '--memory', '2g', '--cpus', '2', '--pids-limit', '256',
               '--tmpfs', '/tmp:uid=568,gid=568,mode=700',
               '--tmpfs', ROOT + ':uid=568,gid=568,mode=700',
               '--tmpfs', '/media:uid=568,gid=568,mode=700',
               '-e', 'HOME=/tmp', '-e', 'TUNARR_DATABASE_PATH=' + database_path,
               '-e', 'MEILI_DUMP_DIR=' + database_path + '/dumps',
               '-e', 'MEILI_MAX_INDEXING_MEMORY=128MiB', '-e', 'MEILI_MAX_INDEXING_THREADS=1',
               '--entrypoint', 'node', image, '-e', 'setInterval(()=>{},1000)')
        # Tar only authoritative files, never raw source WAL/index or credentials in logs.
        import io
        import tarfile
        with tempfile.TemporaryDirectory(prefix='tunarr-boot-') as work:
            archive = Path(work) / 'state.tar'
            with tarfile.open(archive, 'w') as tar:
                prefix = 'stable-1.3/' if version == 'stable-1.3' else ''
                for relative in ['channel-lineups', 'images']:
                    info = tarfile.TarInfo(prefix + relative)
                    info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o700, 568, 568
                    tar.addfile(info)
                for relative in record['files']:
                    data = (source_root / relative).read_bytes()
                    if relative == 'settings.json':
                        settings = json.loads(data)
                        # Network-none has no SSDP-capable interface. This change is
                        # confined to the throwaway boot copy, never promoted state.
                        settings['settings']['hdhr']['autoDiscoveryEnabled'] = False
                        data = json.dumps(settings).encode()
                    info = tarfile.TarInfo(prefix + relative)
                    info.size, info.mode, info.uid, info.gid = len(data), 0o600, 568, 568
                    tar.addfile(info, io.BytesIO(data))
            with archive.open('rb') as stream:
                result = subprocess.run(['docker', 'exec', '-i', name, 'tar', 'xf', '-', '-C', ROOT],
                                        stdin=stream, capture_output=True, timeout=120)
                if result.returncode:
                    raise RuntimeError('isolated copy failed')
        state = json.loads(docker('inspect', name).stdout)[0]
        if state['HostConfig']['NetworkMode'] != 'none' or state['HostConfig'].get('PortBindings'):
            raise ValueError('isolation not established')
        docker('exec', '-d', name, 'sh', '-c',
               'umask 077; exec dotenvx run -- /tunarr/tunarr server > /tmp/tunarr-boot.log 2>&1')
        deadline = time.monotonic() + timeout
        probe = "fetch('http://127.0.0.1:8000/api/version').then(async r=>{if(!r.ok)process.exit(1);console.log(JSON.stringify(await r.json()))}).catch(()=>process.exit(1))"
        while time.monotonic() < deadline:
            result = node(name, probe, check=False)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('isolated boot health timeout')
        actual_version = json.loads(result.stdout)['tunarr']
        expected_version = '1.3.15' if version == 'stable-1.3' else '2026.9.0'
        if actual_version != expected_version:
            raise ValueError('version mismatch')
        probe = "const {DatabaseSync}=require('node:sqlite');const d=new DatabaseSync(process.env.TUNARR_DATABASE_PATH+'/db.db',{readOnly:true});const counts={};for(const t of ['channel','program','channel_programs','program_media_file'])counts[t]=d.prepare('SELECT COUNT(*) n FROM '+t).get().n;d.close();fetch('http://127.0.0.1:8000/api/channels').then(async r=>{if(!r.ok)process.exit(1);const c=await r.json();console.log(JSON.stringify({counts,apiChannels:c.length,apiProgramCounts:Object.fromEntries(c.map(x=>[x.id,x.programCount])),apiPrograms:c.reduce((n,x)=>n+x.programCount,0)}))}).catch(()=>process.exit(1))"
        counts = json.loads(node(name, probe).stdout)
        verify_boot_counts(counts, record, api_program_counts)
        return {'backend': receipt['backend'], 'generation': receipt['generation'],
                'version': actual_version, 'isolation': 'network-none', **counts}
    finally:
        docker('rm', '-f', name, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--version', choices=IMAGES, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(boot(args.source, args.version)))
    except Exception:
        print('Tunarr isolated boot rejected; no production action performed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
