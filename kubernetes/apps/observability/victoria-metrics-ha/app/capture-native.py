"""Online VM native snapshot materialization on the same production volume."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import urllib.parse
import urllib.request
import uuid

ROOT = Path('/capture')
BASE = ROOT / '.kopiur-vm'
URL = 'http://victoria-metrics-ha.observability.svc.cluster.local:8428'


def api(path):
    with urllib.request.urlopen(URL + path, timeout=120) as response:
        assert response.status == 200
        value = json.load(response)
    assert value['status'] == 'ok'
    return value


def link_native(source, target, ancestors=()):
    resolved = source.resolve(strict=True)
    assert ROOT.resolve() in resolved.parents, 'native snapshot link escapes source volume'
    assert resolved not in ancestors, 'native snapshot link cycle'
    mode = resolved.stat().st_mode
    if stat.S_ISDIR(mode):
        target.mkdir(mode=0o700)
        total = [0, 0]
        for entry in sorted(resolved.iterdir()):
            files, size = link_native(entry, target / entry.name, ancestors + (resolved,))
            total[0] += files
            total[1] += size
        return total
    assert stat.S_ISREG(mode), 'unsupported native snapshot entry'
    # Native snapshot partitions are immutable. Hardlink without chmod/chown or
    # copy2 metadata changes to the production inode. No data copy or writer hold.
    os.link(resolved, target)
    return [1, resolved.stat().st_size]


def main():
    os.umask(0o077)
    BASE.mkdir(mode=0o700, exist_ok=True)
    with (BASE / 'capture.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = BASE / ('pending-' + uuid.uuid4().hex)
        name = None
        try:
            name = api('/snapshot/create')['snapshot']
            assert re.fullmatch(r'[0-9]{14}-[A-F0-9]+', name)
            source = ROOT / 'snapshots' / name
            files, size = link_native(source, pending)
            # Observed v1.154 native API layout is data + metadata, not the
            # older top-level indexdb layout. Native original recovery passed.
            assert files > 0 and (pending / 'data').is_dir() and (pending / 'metadata').exists()
            manifest = {'format': 1, 'application': 'victoria-metrics',
                        'contract': 'online-native-snapshot-same-volume-csi',
                        'native_snapshot': name, 'files': files, 'bytes': size,
                        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
            with (pending / 'COMPLETE.json').open('w') as stream:
                json.dump(manifest, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            previous = BASE / 'previous'
            current = BASE / 'current'
            # Only private materialization staging rotates, never repository
            # snapshots, retained originals or legacy backup history.
            if previous.exists():
                shutil.rmtree(previous)
            if current.exists():
                current.rename(previous)
            pending.rename(current)
            fd = os.open(BASE, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            print(json.dumps({'native_snapshot_materialized': True, 'files': files,
                              'bytes': size, 'production_writer_held': False}), flush=True)
        finally:
            if name is not None:
                api('/snapshot/delete?snapshot=' + urllib.parse.quote(name, safe=''))
            if pending.exists():
                shutil.rmtree(pending)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                 if frame.filename == __file__]
        print(json.dumps({'native_capture_failed': type(error).__name__,
                          'source_lines': lines,
                          'private_details_withheld': True}), flush=True)
        raise SystemExit(1) from None
