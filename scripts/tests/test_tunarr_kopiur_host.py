#!/usr/bin/env python3
"""Host-only recovery regressions. No Docker, network or application boot.

SQLite fixtures model only the columns used by the recovery contract, verified
against v1.3.15 and v2026.9.0 source. Native schema/boot tests remain separate.
"""
import copy
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts/tunarr-kopiur-capture.cjs'
POLICY = REPO / 'kubernetes/apps/media/tunarr/app/kopiur-policy.yaml'
# Resolve only this trusted scratch root, never recovery inputs.
SCRATCH = Path(os.environ.get('TMPDIR', Path.home() / '.hermes/cache/scratch')).resolve(strict=True)
CHANNELS = ['00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002']
PROGRAMS = ['10000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000002']
VERSIONS = ('', 'stable-1.3')


def module(name):
    spec = importlib.util.spec_from_file_location(name, REPO / 'scripts' / (name + '.py'))
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


RESTORE = module('tunarr-kopiur-restore')
BOOT = module('tunarr-kopiur-boot')


def dump(path, value):
    path.write_text(json.dumps(value))


def mutate_sql(path, kind):
    with closing(sqlite3.connect(path)) as db, db:
        if kind == 'replacement':
            # Same global IDs, table counts, durations and per-channel cardinality.
            db.execute('UPDATE channel_programs SET program_uuid = CASE program_uuid WHEN ? THEN ? ELSE ? END',
                       (PROGRAMS[0], PROGRAMS[1], PROGRAMS[0]))
        else:
            db.execute('UPDATE channel SET duration = duration + 1 WHERE uuid = ?', (CHANNELS[0],))


class TunarrHostTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='tunarr-host-', dir=SCRATCH)
        self.addCleanup(self.work.cleanup)
        self.parent = Path(self.work.name)
        self.source = self.fixture('source')

    def fixture(self, name):
        source = self.parent / name
        for version in VERSIONS:
            root = source / version
            (root / 'channel-lineups').mkdir(parents=True)
            (root / 'images').mkdir()
            dump(root / 'settings.json', {'settings': {'hdhr': {'autoDiscoveryEnabled': False}}, 'system': {'fixture': True}})
            with closing(sqlite3.connect(root / 'db.db')) as db, db:
                db.executescript('''
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE channel(uuid TEXT PRIMARY KEY, duration INTEGER NOT NULL);
                    CREATE TABLE program(uuid TEXT PRIMARY KEY);
                    CREATE TABLE channel_programs(channel_uuid TEXT REFERENCES channel(uuid),
                        program_uuid TEXT REFERENCES program(uuid), PRIMARY KEY(channel_uuid, program_uuid));
                    CREATE TABLE program_media_file(uuid TEXT PRIMARY KEY);
                    CREATE TABLE media_source(uuid TEXT PRIMARY KEY);
                ''')
                for channel, program in zip(CHANNELS, PROGRAMS):
                    db.execute('INSERT INTO channel VALUES (?, ?)', (channel, 60))
                    db.execute('INSERT INTO program VALUES (?)', (program,))
                    db.execute('INSERT INTO channel_programs VALUES (?, ?)', (channel, program))
                    dump(root / 'channel-lineups' / (channel + '.json'),
                         {'items': [{'type': 'content', 'id': program, 'durationMs': 60}]})
        return source

    def hook(self, source=None, injection=None):
        code = SCRIPT.read_text().replace("const ROOT = '/config/tunarr';", 'const ROOT = ' + json.dumps(str(source or self.source)) + ';')
        if injection:
            needle = 'const before = companions(source);'
            self.assertEqual(code.count(needle), 1)
            code = code.replace(needle, needle + '\n' + injection)
        return subprocess.run(['node', '--disable-warning=ExperimentalWarning', '-e', code],
                              capture_output=True, text=True, timeout=20)

    def capture(self, source=None):
        source = source or self.source
        result = self.hook(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = RESTORE.load(source / '.kopiur-consistent/manifest.json')
        RESTORE.verify(source, manifest['generation'], 0, time.time())
        return manifest

    def test_embedded_hook_exact(self):
        result = subprocess.run(['yq', '-o=json', 'select(.kind == "SnapshotPolicy") | .spec.hooks.beforeSnapshot[0].workloadExec.command', str(POLICY)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ['node', '--disable-warning=ExperimentalWarning', '-e', SCRIPT.read_text()])

    def test_capture_commit_before_json_gap(self):
        for version in VERSIONS:
            for kind in ('replacement', 'duration'):
                with self.subTest(version=version, kind=kind):
                    source = self.fixture('gap-' + str(len(list(self.parent.iterdir()))))
                    old = self.capture(source)
                    before = {p.name: p.read_bytes() for p in (source / version / 'channel-lineups').iterdir()}
                    sql = ("UPDATE channel_programs SET program_uuid = CASE program_uuid WHEN '" + PROGRAMS[0] + "' THEN '" + PROGRAMS[1] + "' ELSE '" + PROGRAMS[0] + "' END"
                           if kind == 'replacement' else 'UPDATE channel SET duration = duration + 1')
                    # Commit AFTER companion hashes, BEFORE the SQL backup. Leave
                    # JSON unchanged for the entire capture, with no timing race.
                    injection = 'if (version === ' + json.dumps(version) + ") {const writer = new DatabaseSync(path.join(source, 'db.db')); writer.exec(" + json.dumps('BEGIN; ' + sql + '; COMMIT;') + '); writer.close();}'
                    self.assertNotEqual(self.hook(source, injection).returncode, 0)
                    self.assertEqual(before, {p.name: p.read_bytes() for p in (source / version / 'channel-lineups').iterdir()})
                    self.assertEqual(RESTORE.load(source / '.kopiur-consistent/attempt.json')['state'], 'failed')
                    self.assertEqual(RESTORE.load(source / '.kopiur-consistent/manifest.json'), old)
                    with self.assertRaisesRegex(ValueError, 'incomplete capture'):
                        RESTORE.verify(source, old['generation'], 0, time.time())

    def test_restore_rejects_self_consistent_manifest_with_sql_json_gap(self):
        for version in VERSIONS:
            for kind in ('replacement', 'duration'):
                with self.subTest(version=version, kind=kind):
                    source = self.fixture('historical-' + str(len(list(self.parent.iterdir()))))
                    manifest = self.capture(source)
                    record = next(r for r in manifest['records'] if r['source'] == version)
                    db = source / '.kopiur-consistent' / ('capture-' + manifest['generation']) / record['artifact'] / 'db.db'
                    mutate_sql(db, kind)
                    # Model an old hook's accepted gap: all checksums/counts valid.
                    record['files']['db.db'] = RESTORE.digest(db)
                    dump(source / '.kopiur-consistent/manifest.json', manifest)
                    destination = self.parent / ('rejected-' + str(len(list(self.parent.iterdir()))))
                    with self.assertRaisesRegex(ValueError, 'lineup ' + ('membership' if kind == 'replacement' else 'duration') + ' mismatch'):
                        RESTORE.restore(source, destination, manifest['generation'], 0, time.time(), 'nas')
                    self.assertFalse(destination.exists())

    def mixed_lineups(self):
        for version in VERSIONS:
            root = self.source / version
            for n, channel in enumerate(CHANNELS):
                path = root / 'channel-lineups' / (channel + '.json')
                lineup = RESTORE.load(path)
                lineup['items'] += [dict(lineup['items'][0])] * (n + 1)
                lineup['items'] += [{'type': 'offline', 'durationMs': 10},
                                    {'type': 'redirect', 'channel': CHANNELS[1 - n], 'durationMs': 20}]
                dump(path, lineup)
                with closing(sqlite3.connect(root / 'db.db')) as db, db:
                    db.execute('UPDATE channel SET duration = ? WHERE uuid = ?',
                               (sum(i['durationMs'] for i in lineup['items']), channel))

    def test_repeats_offline_redirect_and_per_channel_api_counts(self):
        self.mixed_lineups()
        manifest = self.capture()
        destination = self.parent / 'promoted'
        RESTORE.restore(self.source, destination, manifest['generation'], 0, time.time(), 'nas')
        for record in manifest['records']:
            expected = BOOT.expected_api_program_counts(destination / record['source'], record, RESTORE)
            self.assertEqual(expected, dict(zip(CHANNELS, [2, 3])))
            counts = {k: record['counts'][k] for k in ('channel', 'program', 'channel_programs', 'program_media_file')}
            self.assertEqual(counts['channel_programs'], 2)
            actual = {'counts': counts, 'apiChannels': 2, 'apiPrograms': 5, 'apiProgramCounts': expected}
            BOOT.verify_boot_counts(actual, record, expected)
            # Same aggregate count on wrong channels must fail too.
            bad = copy.deepcopy(actual)
            bad['apiProgramCounts'] = dict(zip(CHANNELS, [3, 2]))
            with self.assertRaises(ValueError):
                BOOT.verify_boot_counts(bad, record, expected)
            for key in ('program', 'channel_programs'):
                bad = copy.deepcopy(actual)
                bad['counts'][key] += 1
                with self.assertRaises(ValueError):
                    BOOT.verify_boot_counts(bad, record, expected)

    def test_full_duration_includes_offline_and_redirect(self):
        self.mixed_lineups()
        for version in VERSIONS:
            with closing(sqlite3.connect(self.source / version / 'db.db')) as db, db:
                db.execute('UPDATE channel SET duration = duration - 30')
        self.assertNotEqual(self.hook().returncode, 0)

    def test_raw_search_snapshot_archived_not_importable(self):
        for version in VERSIONS:
            root = self.source / version
            (root / 'ms-snapshots').mkdir()
            (root / 'ms-snapshots/data.ms.snapshot').write_bytes(b'corrupt raw search snapshot\x00')
            (root / 'data.ms').mkdir()
            (root / 'data.ms/data.mdb').write_bytes(b'raw index')
        manifest = self.capture()
        source_hashes = {str(p.relative_to(self.source)): RESTORE.digest(p) for p in RESTORE.tree(self.source)}
        for backend in ('nas', 'r2'):
            destination = self.parent / backend
            RESTORE.restore(self.source, destination, manifest['generation'], 0, time.time(), backend)
            for record in manifest['records']:
                root = destination / record['source']
                self.assertFalse((root / 'ms-snapshots').exists())
                self.assertFalse((root / 'data.ms').exists())
                archive = destination / '.kopiur-raw' / record['artifact']
                self.assertEqual((archive / 'ms-snapshots/data.ms.snapshot').read_bytes(), b'corrupt raw search snapshot\x00')
                self.assertEqual((archive / 'data.ms/data.mdb').read_bytes(), b'raw index')
            self.assertEqual(len(list(destination.rglob('data.ms.snapshot'))), 2)
        self.assertEqual(source_hashes, {str(p.relative_to(self.source)): RESTORE.digest(p) for p in RESTORE.tree(self.source)})

    def test_empty_offline_and_redirect_only_channels(self):
        for version in VERSIONS:
            root = self.source / version
            with closing(sqlite3.connect(root / 'db.db')) as db, db:
                db.execute('DELETE FROM channel_programs')
                for channel, items in zip(CHANNELS, [[], [{'type': 'offline', 'durationMs': 10},
                                                         {'type': 'redirect', 'channel': CHANNELS[0], 'durationMs': 20}]]):
                    dump(root / 'channel-lineups' / (channel + '.json'), {'items': items})
                    db.execute('UPDATE channel SET duration = ? WHERE uuid = ?', (sum(i['durationMs'] for i in items), channel))
        self.capture()

    def test_invalid_durations_and_unknown_item_types_fail_closed(self):
        for value in (0, -1, True, '60', None):
            with self.subTest(value=value):
                path = self.source / 'channel-lineups' / (CHANNELS[0] + '.json')
                dump(path, {'items': [{'type': 'content', 'id': PROGRAMS[0], 'durationMs': value}]})
                self.assertNotEqual(self.hook().returncode, 0)
        dump(path, {'items': [{'type': 'unknown', 'durationMs': 60}]})
        self.assertNotEqual(self.hook().returncode, 0)

    def test_restore_item_and_full_duration_guards(self):
        self.mixed_lineups()
        manifest = self.capture()
        record = manifest['records'][0]
        relative = 'channel-lineups/' + CHANNELS[0] + '.json'
        path = self.source / '.kopiur-consistent' / ('capture-' + manifest['generation']) / record['artifact'] / relative
        original = RESTORE.load(path)
        cases = []
        for value in (0, -1, True, '60', None, float('nan'), float('inf')):
            changed = copy.deepcopy(original)
            changed['items'][0]['durationMs'] = value
            cases.append(changed)
        for kind in ('unknown', 'offline', 'redirect'):
            changed = copy.deepcopy(original)
            if kind == 'unknown':
                changed['items'][0]['type'] = kind
            else:
                # SQL still includes this non-content duration.
                changed['items'] = [i for i in changed['items'] if i['type'] != kind]
            cases.append(changed)
        for changed in cases:
            with self.subTest(items=changed):
                dump(path, changed)
                record['files'][relative] = RESTORE.digest(path)
                dump(self.source / '.kopiur-consistent/manifest.json', manifest)
                with self.assertRaises(ValueError):
                    RESTORE.verify(self.source, manifest['generation'], 0, time.time())

    def test_restore_rejects_corrupt_artifact_and_leaf_symlink(self):
        manifest = self.capture()
        record = manifest['records'][0]
        path = self.source / '.kopiur-consistent' / ('capture-' + manifest['generation']) / record['artifact'] / 'db.db'
        original = path.read_bytes()
        path.write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            RESTORE.verify(self.source, manifest['generation'], 0, time.time())
        path.write_bytes(original)
        sentinel = self.parent / 'sentinel'
        sentinel.write_bytes(original)
        path.unlink()
        path.symlink_to(sentinel)
        with self.assertRaises(ValueError):
            RESTORE.verify(self.source, manifest['generation'], 0, time.time())

    def test_restore_security_guards_unchanged(self):
        manifest = self.capture()
        generation = manifest['generation']
        with self.assertRaises(ValueError):
            RESTORE.verify(self.source, '0' * 32, 0, time.time())
        with self.assertRaises(ValueError):
            RESTORE.verify(self.source, generation, time.time() + 1, time.time() + 2)
        for destination in (self.source, self.source / 'nested'):
            with self.assertRaises(ValueError):
                RESTORE.restore(self.source, destination, generation, 0, time.time(), 'nas')
        link = self.parent / 'linked-parent'
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            RESTORE.verify(link / 'source', generation, 0, time.time())
        with self.assertRaises(ValueError):
            RESTORE.restore(self.source, link / 'destination', generation, 0, time.time(), 'nas')
        raw = self.source / 'ms-snapshots'
        raw.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            RESTORE.restore(self.source, self.parent / 'reject-link', generation, 0, time.time(), 'nas')
        self.assertFalse((self.parent / 'reject-link').exists())


if __name__ == '__main__':
    unittest.main()
