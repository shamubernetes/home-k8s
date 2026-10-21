#!/usr/bin/env python3
"""Run the existing Stash restore recipe on both actual isolated originals."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from urllib.request import Request, urlopen


def require(value):
    if not value:
        raise ValueError('original Stash native recovery acceptance failed')


def database(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {t: db.execute('SELECT count(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0] for t in tables}
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode() + b'\n')
        return {'tables': len(tables), 'rows': sum(counts.values()), 'sha256': digest.hexdigest()}, counts


def graphql(query):
    request = Request('http://127.0.0.1:9999/graphql',
                      data=json.dumps({'query': query}).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        value = json.load(response)
    require(not value.get('errors') and bool(value.get('data')))
    return value['data']


def original_keys(config):
    # Preserve the actual native JWT/session key lines, never log their values.
    lines = [line for line in config.splitlines()
             if re.match(r'^(jwt_secret_key|session_store_key):', line)]
    require(len(lines) == 2)
    return lines


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    module = importlib.util.spec_from_file_location('stash_original_restore', '/kopiur/restore.py')
    if module is None or module.loader is None:
        raise ValueError('native restore module unavailable')
    recipe = importlib.util.module_from_spec(module)
    module.loader.exec_module(recipe)
    work = Path('/work')
    results = {}
    expected_checksums = None
    for backend in ('nas', 'r2'):
        source = Path('/original') / backend / '.kopiur-native'
        require((source / 'COMPLETE').read_text() == 'complete\n')
        bundle = source / 'current'
        checksums = json.loads((bundle / 'checksums.json').read_text())
        require(expected_checksums is None or checksums == expected_checksums)
        expected_checksums = checksums
        expected, counts = database(bundle / 'stash-go.sqlite')
        require(expected['tables'] > 0 and expected['rows'] > 0)
        keys = original_keys((bundle / 'config.yml').read_text())
        for name in ('config', 'data', 'plugins', 'scrapers'):
            path = work / name
            if path.exists():
                shutil.rmtree(path)
            path.mkdir()
        recipe.restore(bundle, *(work / name for name in ('config', 'data', 'plugins', 'scrapers')))
        env = dict(os.environ, HOME='/work/config', STASH_CONFIG_FILE='/work/config/config.yml',
                   STASH_DATABASE='/work/config/stash-go.sqlite', STASH_BLOBS_PATH='/work/data/blobs',
                   STASH_BLOBS_STORAGE='FILESYSTEM', STASH_METADATA='/work/data/metadata',
                   STASH_PLUGINS_PATH='/work/plugins', STASH_SCRAPERS_PATH='/work/scrapers',
                   STASH_PORT='9999', STASH_HOST='127.0.0.1',
                   STASH_CACHE='/work/cache', STASH_GENERATED='/work/generated')
        with open(os.devnull, 'wb') as sink:
            process = subprocess.Popen(['/usr/bin/stash', '--config', '/work/config/config.yml',
                                        '--host', '127.0.0.1', '--port', '9999', '--nobrowser'],
                                       env=env, cwd='/work/config',
                                       stdout=sink, stderr=sink)
            try:
                deadline = time.monotonic() + 180
                while True:
                    require(process.poll() is None)
                    try:
                        status = graphql('{ systemStatus { status } }')['systemStatus']['status']
                        require(status == 'OK')
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('native Stash startup deadline') from None
                        time.sleep(1)
                native_counts = {}
                for entity in ('Scenes', 'Performers', 'Studios', 'Tags', 'Images', 'Galleries'):
                    field = 'find' + entity
                    count = graphql('{ ' + field + '(filter:{per_page:1}) { count } }')[field]['count']
                    require(count == counts[entity.lower()])
                    native_counts[entity.lower()] = count
                require(original_keys((work / 'config/config.yml').read_text()) == keys)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        recovered, _ = database(work / 'config/stash-go.sqlite')
        require(recovered == expected)
        # Revalidate every original payload after the native consumer exits.
        for name, expected_hash in checksums.items():
            with (bundle / name).open('rb') as stream:
                require(hashlib.file_digest(stream, 'sha256').hexdigest() == expected_hash)
        results[backend] = dict(expected, native_entity_counts=native_counts,
                               snapshot_id=os.environ[backend.upper() + '_SNAPSHOT'],
                               native_graphql=True, original_native_keys_preserved=True,
                               original_unchanged=True)
    require(results['nas']['sha256'] == results['r2']['sha256'])
    receipt = {'app': 'stash', 'independent_original_native_recovery': True,
               'nas_r2_native_data_equal': True, 'full_bundle_equal': True, 'results': results}
    (work / 'native-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        frames = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                  if frame.filename == __file__]
        print(json.dumps({'failed': True, 'class': type(error).__name__,
                          'source_lines': frames}), flush=True)
        raise SystemExit(1) from None
