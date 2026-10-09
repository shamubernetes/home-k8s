"""Qualify selected TubeArchivist release bytes without relying on OCI labels.

The caller supplies reviewed source digests. Read a merged immutable image archive,
never execute the application, and return only digests. This proves selected bytes,
not whole-image source provenance or application-loaded configuration.
"""
import argparse
import hashlib
import json
import re
import signal
import subprocess
import sys
import tarfile

from kopiur_elasticsearch_escrow import EscrowError

SOURCE_URL = 'https://github.com/tubearchivist/tubearchivist'
SOURCE_PATHS = {
    'docker_assets/run.sh': 'app/run.sh',
    'docker_assets/beat_auto_spawn.sh': 'app/beat_auto_spawn.sh',
    'docker_assets/backend_start.py': 'app/backend_start.py',
    'backend/appsettings/index_mapping.json': 'app/appsettings/index_mapping.json',
    'backend/appsettings/src/index_setup.py': 'app/appsettings/src/index_setup.py',
    'backend/common/src/env_settings.py': 'app/common/src/env_settings.py',
    'backend/common/src/es_connect.py': 'app/common/src/es_connect.py',
    'backend/common/src/index_generic.py': 'app/common/src/index_generic.py',
    'backend/common/src/searching.py': 'app/common/src/searching.py',
    'backend/config/settings.py': 'app/config/settings.py',
}


def validate_witness(plan):
    witness = plan.get('source_witness')
    if (plan['application'] != 'media/tubearchivist' or not isinstance(witness, dict)
            or set(witness) != {'image', 'source_url', 'source_revision', 'files'}
            or any(witness[k] != plan[k] for k in ('image', 'source_url', 'source_revision'))
            or plan['source_url'] != SOURCE_URL
            or not re.fullmatch(r'docker\.io/bbilly1/tubearchivist@sha256:[0-9a-f]{64}', plan['image'])
            or not isinstance(witness['files'], list) or len(witness['files']) != len(SOURCE_PATHS)
            or set(plan['source_files']) != set(SOURCE_PATHS)):
        raise EscrowError('complete reviewed TubeArchivist artifact witness required')
    sources, artifacts = set(), set()
    for item in witness['files']:
        if (not isinstance(item, dict) or set(item) != {'source', 'artifact', 'sha256'}
                or not isinstance(item['source'], str) or item['source'] not in SOURCE_PATHS
                or item['source'] in sources or item['artifact'] != SOURCE_PATHS[item['source']]
                or item['artifact'] in artifacts or item['sha256'] != plan['source_files'][item['source']]):
            raise EscrowError('TubeArchivist artifact witness mapping invalid')
        sources.add(item['source']); artifacts.add(item['artifact'])
    return {item['artifact']: item['sha256'] for item in witness['files']}


def archive_digests(stream):
    """Read merged rootfs bytes with no disk extraction or archive path execution."""
    required = set(SOURCE_PATHS.values())
    ancestors = {p.rsplit('/', 1)[0] for p in required}
    ancestors.update('/'.join(p.split('/')[:i]) for p in required for i in range(1, len(p.split('/'))))
    result = {}
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        for entry in archive:
            name = entry.name.removeprefix('./')
            if name in ancestors and not entry.isdir():
                raise EscrowError('artifact source ancestor is not a directory')
            if name not in required:
                # Reject noncanonical spellings of a selected path, rather than
                # allowing a later traversal/alias to hide a different member.
                parts = name.split('/')
                if any(p in ('', '.', '..') for p in parts):
                    normalized = []
                    for p in parts:
                        if p == '..':
                            if normalized: normalized.pop()
                        elif p not in ('', '.'):
                            normalized.append(p)
                    if '/'.join(normalized) in required | ancestors:
                        raise EscrowError('ambiguous artifact source member')
                continue
            if name in result or not entry.isfile() or not 0 < entry.size <= 1024 * 1024:
                raise EscrowError('artifact source member invalid')
            member = archive.extractfile(entry)
            if member is None:
                raise EscrowError('artifact source member missing')
            data = member.read(entry.size + 1)
            if len(data) != entry.size:
                raise EscrowError('artifact source member incomplete')
            result[name] = hashlib.sha256(data).hexdigest()
    if set(result) != required:
        raise EscrowError('complete artifact source coverage required')
    return result


def image_digests(image):
    if not re.fullmatch(r'docker\.io/bbilly1/tubearchivist@sha256:[0-9a-f]{64}', image):
        raise EscrowError('immutable TubeArchivist artifact required')
    process = subprocess.Popen(['crane', 'export', image, '-'], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    if process.stdout is None:
        process.kill(); process.wait()
        raise EscrowError('artifact source stream missing')
    def timeout(signum, frame):
        raise EscrowError('artifact source export timed out')
    previous = signal.signal(signal.SIGALRM, timeout)
    # Finish cleanup inside the authority reader's outer 60-second I/O limit.
    signal.alarm(45)
    try:
        result = archive_digests(process.stdout)
        # The tar end marker must not conceal a failed export or block the writer
        # on remaining padding. Drain before checking the actual exporter exit.
        while process.stdout.read(65536):
            pass
        if process.wait(timeout=30) != 0:
            raise EscrowError('immutable artifact export failed')
        return result
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        process.stdout.close()
        if process.poll() is None:
            process.kill(); process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(image_digests(args.image), sort_keys=True))
    except Exception:
        sys.stderr.write('consumer artifact witness read failed\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
