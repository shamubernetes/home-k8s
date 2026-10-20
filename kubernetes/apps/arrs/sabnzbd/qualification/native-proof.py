#!/usr/bin/env python3
"""Boot each original on an isolated disposable copy. Never print private data."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

from importlib.util import module_from_spec, spec_from_file_location


def db_digest(path, summary=False):
    uri = path.resolve().as_uri() + '?mode=ro'
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('database integrity rejected')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        inventory = []
        for table in tables:
            rows = connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"').fetchall()
            encoded = [json.dumps(list(row), default=lambda v: {'bytes': v.hex()},
                                  separators=(',', ':'), sort_keys=True) for row in rows]
            inventory.append([table, sorted(encoded)])
        if summary:
            return [{'table': name if name in ('history', 'rss', 'sqlite_sequence') else 'other',
                     'rows': len(rows),
                     'dataSha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest()}
                    for name, rows in inventory]
        return hashlib.sha256(json.dumps(inventory).encode()).hexdigest()


def native_database_equality(original, restored):
    before = db_digest(original, summary=True)
    after = db_digest(restored, summary=True)
    versions = []
    for database in (original, restored):
        with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            versions.append(connection.execute('PRAGMA user_version').fetchone()[0])
    if versions[0] > 8 or versions[1] != 8:
        raise ValueError('unexpected pinned native database version')
    migration = []
    if before != after:
        # Exact pinned SABnzbd database.py upgrades pre-v8 history by adding an
        # empty RSS table. Every original table and record must remain identical.
        if not (versions[0] < 8 and versions[1] == 8 and
                [row['table'] for row in before] == ['history']):
            raise ValueError('unsupported native database change')
        rss = [row for row in after if row['table'] == 'rss']
        retained = [row for row in after if row['table'] != 'rss']
        empty_hash = hashlib.sha256(b'[]').hexdigest()
        if retained != before or rss != [{'table': 'rss', 'rows': 0, 'dataSha256': empty_hash}]:
            raise ValueError('original records changed during native recovery')
        migration = ['rss']
    return {'originalRecordsEqual': True, 'sourceDataSha256': db_digest(original),
            'sourceUserVersion': versions[0], 'restoredUserVersion': versions[1],
            'addedEmptyNativeTables': migration}


def prepare(original, destination):
    from configobj import ConfigObj
    if destination.exists():
        raise ValueError('disposable destination already exists')
    shutil.copytree(original, destination, symlinks=False)
    admin = destination / 'admin'
    admin.mkdir(exist_ok=True)
    target = admin / 'history1.db'
    for suffix in ('', '-wal', '-shm', '-journal'):
        Path(str(target) + suffix).unlink(missing_ok=True)
    shutil.copyfile(original / '.kopiur-consistent/database.sqlite3', target)
    cfg = ConfigObj(str(destination / 'sabnzbd.ini'), interpolation=False)
    misc = cfg['misc']
    # These changes affect only disposable copies, never original recovery data.
    misc.update({'host': '127.0.0.1', 'port': '8080', 'enable_https': '0',
                 'download_dir': str(destination / 'incomplete'),
                 'complete_dir': str(destination / 'complete'),
                 'admin_dir': str(admin), 'log_dir': str(destination / 'logs'),
                 'nzb_backup_dir': str(destination / 'nzb-backup'),
                 'dirscan_dir': '', 'email_endjob': '0', 'email_full': '0',
                 'check_new_rel': '0', 'auto_browser': '0',
                 'history_retention': '', 'history_retention_option': 'all'})
    cfg['servers'] = {}
    misc['schedlines'] = []
    misc['script_dir'] = ''
    for section in ('ncenter', 'acenter', 'ntfosd', 'prowl', 'pushover',
                    'pushbullet', 'nscript', 'apprise'):
        if section in cfg:
            cfg[section]['enable'] = '0'
    cfg['scheduling'] = {'lines': []}
    cfg.write()
    return cfg


def run_native(destination, cfg, expected):
    command = [sys.executable, '/app/SABnzbd.py', '--config-file',
               str(destination / 'sabnzbd.ini'), '--server', '127.0.0.1:8080',
               '--browser', '0', '--pause', '--disable-file-log', '--console']
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={'PATH': os.environ['PATH'], 'HOME': '/work', 'TMPDIR': '/tmp'})
    try:
        deadline = time.monotonic() + 90
        version = None
        query = urllib.parse.urlencode({'mode': 'version', 'output': 'json',
                                        'apikey': cfg['misc']['api_key']})
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ValueError('native app exited')
            try:
                with urllib.request.urlopen('http://127.0.0.1:8080/api?' + query, timeout=2) as reply:
                    body = json.load(reply)
                version = body.get('version')
                if version != '5.1.3':
                    raise ValueError('wrong native version')
                break
            except (OSError, json.JSONDecodeError):
                time.sleep(1)
        else:
            raise ValueError('native health timeout')
        query = urllib.parse.urlencode({'mode': 'queue', 'output': 'json',
                                        'apikey': cfg['misc']['api_key']})
        with urllib.request.urlopen('http://127.0.0.1:8080/api?' + query, timeout=3) as reply:
            queue = json.load(reply)['queue']
        if queue.get('paused') is not True:
            raise ValueError('disposable downloader not paused')
        result = {'version': version, 'paused': True, 'apiHealthy': True}
        return result
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # Application is now stopped only in this isolated disposable recovery test.
        database = destination / 'admin/history1.db'
        if sys.exc_info()[0] is None:
            original = Path('/nas/.kopiur-consistent/database.sqlite3')
            if db_digest(original) != expected:
                raise ValueError('original source changed')
            result['databaseEquality'] = native_database_equality(original, database)


STAGE = "startup"


def main():
    global STAGE
    spec = spec_from_file_location('original', '/contract/verify-original.py')
    if spec is None or spec.loader is None:
        raise ValueError('missing original verification contract')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    STAGE = 'original-pair-verification'
    pair = module.verify('/nas', '/r2')
    expected = db_digest(Path('/nas/.kopiur-consistent/database.sqlite3'))
    results = []
    for backend in ('nas', 'r2'):
        source = Path('/' + backend)
        destination = Path('/work') / backend
        STAGE = backend + '-disposable-prepare'
        cfg = prepare(source, destination)
        STAGE = backend + '-initial-native-boot'
        initial = run_native(destination, cfg, expected)
        STAGE = backend + '-restart-native-boot'
        restarted = run_native(destination, cfg, expected)
        results.append({'backend': backend, 'initial': initial, 'restarted': restarted,
                        'nativeDatabaseDataSha256': expected})
    if module.verify('/nas', '/r2') != pair:
        raise ValueError('original roots changed')
    print(json.dumps({'originalPair': pair, 'nativeConsumers': results,
                      'productionTouched': False, 'metadataReplayProved': False,
                      'runtimeEscrowProved': False, 'retirementAllowed': False}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        line = frames[-1].lineno if frames else 0
        sys.exit(f'isolated SABnzbd proof rejected stage={STAGE} code={type(error).__name__} line={line}; private details withheld')
