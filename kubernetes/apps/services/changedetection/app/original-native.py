#!/usr/bin/env python3
"""Consume both actual ChangeDetection JSON/history originals with its native store."""
import contextlib
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys


def require(value):
    if not value:
        raise ValueError('original ChangeDetection acceptance failed')


def inventory(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False,
                                     onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in sorted(dirs + files):
            p = Path(base) / name
            require(not p.is_symlink() and (p.is_file() or p.is_dir()))
            info = p.stat()
            digest = None
            if p.is_file():
                with p.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            result[str(p.relative_to(root))] = [info.st_mode, info.st_uid, info.st_gid,
                                                 info.st_mtime_ns if p.is_file() else None, digest]
    return result


def child(target, result_path):
    primary = json.loads((target / 'changedetection.json').read_text())
    records = {p.parent.name: json.loads(p.read_text()) for p in target.glob('*/watch.json')}
    expected = set(records) | set(primary.get('watching', {}))
    logging.disable(logging.CRITICAL)
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        from loguru import logger
        logger.remove()
        from changedetectionio.store import ChangeDetectionStore
        store = ChangeDetectionStore(datastore_path=str(target), include_default_watches=False)
        loaded = store.data
        require(set(loaded['watching']) == expected)
        for identity, record in records.items():
            for key in ('url', 'processor', 'paused', 'method', 'headers', 'body'):
                if key in record:
                    require(loaded['watching'][identity].get(key) == record[key])
        application = primary.get('settings', {}).get('application', {})
        for key in ('api_access_token', 'password'):
            if key in application:
                require(loaded['settings']['application'].get(key) == application[key])
        # Hash original persisted logical records, not native regenerated timestamps.
        logical = hashlib.sha256(json.dumps({'settings': primary, 'watches': records},
                                            sort_keys=True).encode()).hexdigest()
        result_path.write_text(json.dumps({'native_store_loaded': True,
                                           'watches': len(expected),
                                           'logical_sha256': logical,
                                           'original_native_auth_state_loaded': True}))
    # The library may start private autosave threads. This isolated parser has
    # completed all required reads; do not leave a daemon keeping the Job alive.
    os._exit(0)


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    points = json.loads(os.environ['ORIGINAL_POINTS'])
    sources = {b: Path('/original') / b for b in ('nas', 'r2')}
    trees = {b: inventory(p) for b, p in sources.items()}
    require(trees['nas'] and trees['nas'] == trees['r2'])
    results = {}
    for backend, original in sources.items():
        target = Path('/work') / backend
        require(not target.exists())
        shutil.copytree(original, target)
        json_count = 0
        history_bytes = 0
        for p in target.rglob('*'):
            if p.is_file() and p.suffix == '.json':
                json.loads(p.read_text()); json_count += 1
            elif p.is_file() and p.suffix == '.gz':
                with gzip.open(p, 'rb') as stream:
                    while block := stream.read(1024 * 1024):
                        history_bytes += len(block)
        require(json_count > 0)
        result_path = Path('/work') / (backend + '-native-result.json')
        env = dict(os.environ, DATASTORE=str(target), PYTHONDONTWRITEBYTECODE='1')
        with open(os.devnull, 'wb') as sink:
            process = subprocess.run([sys.executable, __file__, '--native-child', str(target), str(result_path)],
                                     env=env, stdout=sink, stderr=sink, timeout=120)
        if process.returncode != 0 and Path('/work/child-error.json').is_file():
            print(Path('/work/child-error.json').read_text(), flush=True)
        require(process.returncode == 0 and result_path.is_file())
        result = json.loads(result_path.read_text())
        require(inventory(original) == trees[backend])
        result.update(snapshot_id=points[backend], original_unchanged=True,
                      json_files=json_count, verified_history_bytes=history_bytes)
        results[backend] = result
    require(results['nas']['logical_sha256'] == results['r2']['logical_sha256'])
    receipt = {'app': 'changedetection', 'independent_original_native_recovery': True,
               'nas_r2_native_data_equal': True, 'full_original_tree_equal': True, 'results': results}
    Path('/work/native-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['--native-child']:
            child(Path(sys.argv[2]), Path(sys.argv[3]))
        else:
            main()
    except Exception as error:
        import traceback
        lines = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename == __file__]
        failure = json.dumps({'failed': True, 'class': type(error).__name__, 'source_lines': lines})
        if sys.argv[1:2] == ['--native-child']:
            Path('/work/child-error.json').write_text(failure)
        print(failure, flush=True)
        raise SystemExit(1) from None
