"""Exercise the deployed online PDS escrow, without accessing production data."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
APP = REPO / 'kubernetes/apps/services/atproto-pds/app'
SCRIPT = APP / 'kopiur-runtime/capture.cjs'


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = (Path(self.tmp.name) / 'pds').resolve()
        self.root.mkdir()
        for name in ('account', 'sequencer', 'did_cache'):
            with closing(sqlite3.connect(self.root / (name + '.sqlite'))) as db:
                db.execute('create table fixture(n integer)')
                db.execute('insert into fixture values(0)')
                db.commit()
        (self.root / 'actors').mkdir()
        (self.root / 'blocks').mkdir()
        self.bundle = self.root / '.kopiur-pds'
        self.env = os.environ | {
            'PDS_DATA_DIRECTORY': str(self.root),
            'PDS_BLOBSTORE_DISK_LOCATION': str(self.root / 'blocks'),
            'PDS_JWT_SECRET': 'fixture-jwt',
            'PDS_ADMIN_PASSWORD': 'fixture-admin',
            'PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX': '01' * 32,
        }
        self.env.pop('NODE_OPTIONS', None)

    def run_helper(self, command='capture', extra=None):
        return subprocess.run(['node', str(SCRIPT), command], env=self.env | (extra or {}),
                              capture_output=True, text=True, timeout=15)

    def capture(self):
        p = self.run_helper()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('fixture-admin', p.stdout + p.stderr)
        self.assertFalse(json.loads(p.stdout)['snapshotCompleted'])
        self.assertEqual(self.run_helper('verify').returncode, 0)
        return json.loads((self.bundle / 'COMPLETE').read_text())

    def test_atomic_generation_hashes_runtime_and_private_modes(self):
        first = self.capture()
        second = self.capture()
        self.assertNotEqual(first['generation'], second['generation'])
        current = self.bundle / 'current'
        self.assertFalse((current / 'application-state.tar').exists())
        metadata = json.loads((current / 'metadata.json').read_text())
        self.assertEqual(metadata['generation'], second['generation'])
        self.assertFalse(metadata['applicationConsistent'])
        sums = (current / 'SHA256SUMS').read_bytes()
        self.assertEqual(hashlib.sha256(sums).hexdigest(), second['manifestSHA256'])
        for line in sums.decode().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(hashlib.sha256((current / name).read_bytes()).hexdigest(), digest)
            self.assertEqual((current / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(current.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((current / 'runtime.json').read_text())['PDS_JWT_SECRET'], 'fixture-jwt')

    def test_upgrade_retains_private_legacy_generation(self):
        self.bundle.mkdir(mode=0o700)
        current = self.bundle / 'current'
        current.mkdir(mode=0o700)
        (current / 'application-state.tar').write_bytes(b'legacy-tar-fixture')
        (current / 'application-state.tar').chmod(0o644)
        (self.bundle / 'capture.lock').write_bytes(b'')
        (self.bundle / 'capture.lock').chmod(0o644)
        (self.bundle / 'COMPLETE').write_text('complete\n')
        self.capture()
        self.assertEqual((self.bundle / 'previous/application-state.tar').read_bytes(), b'legacy-tar-fixture')
        self.assertEqual((self.bundle / 'capture.lock').stat().st_mode & 0o777, 0o600)

    def test_unpaired_tampered_and_legacy_generations_rejected(self):
        for mutation in ('marker', 'runtime', 'legacy'):
            with self.subTest(mutation=mutation):
                self.capture()
                if mutation == 'runtime':
                    (self.bundle / 'current/runtime.json').write_text('{}')
                elif mutation == 'legacy':
                    (self.bundle / 'COMPLETE').write_text('complete\n')
                else:
                    marker = json.loads((self.bundle / 'COMPLETE').read_text())
                    marker['generation'] = 'f' * 32
                    (self.bundle / 'COMPLETE').write_text(json.dumps(marker))
                self.assertNotEqual(self.run_helper('verify').returncode, 0)

    def test_no_writer_control_and_concurrent_wal_progress(self):
        # A real WAL writer stays alive and commits while capture prepares escrow.
        code = '''import sqlite3,sys,time
c=sqlite3.connect(sys.argv[1]); c.execute('pragma journal_mode=WAL')
print('ready',flush=True)
while True:
 c.execute('update fixture set n=n+1');c.commit();time.sleep(.001)
'''
        writer = subprocess.Popen([sys.executable, '-c', code, str(self.root / 'account.sqlite')],
                                  stdout=subprocess.PIPE, text=True)
        assert writer.stdout is not None
        try:
            self.assertEqual(writer.stdout.readline().strip(), 'ready')
            with closing(sqlite3.connect(self.root / 'account.sqlite')) as c:
                before = c.execute('select n from fixture').fetchone()[0]
            for _ in range(3):
                self.capture()
                self.assertIsNone(writer.poll())
            with closing(sqlite3.connect(self.root / 'account.sqlite')) as c:
                self.assertGreater(c.execute('select n from fixture').fetchone()[0], before)
        finally:
            writer.terminate()
            writer.wait(timeout=5)
            writer.stdout.close()
        source = SCRIPT.read_text()
        self.assertNotIn('child_process', source)
        self.assertNotIn('control(', source)
        shell = (SCRIPT.parent / 'recurring.sh').read_text()
        self.assertNotIn('kill ', shell)
        for command in ('acquire', 'check', 'release'):
            self.assertEqual(subprocess.run(['sh', str(SCRIPT.parent / 'recurring.sh'), command]).returncode, 2)
        self.assertEqual(subprocess.run(['sh', str(SCRIPT.parent / 'recurring.sh'),
                                       'supervise', 'sh', '-c', 'exit 17']).returncode, 17)

    def test_failure_invalidates_old_success_and_does_not_expose_runtime(self):
        self.capture()
        self.env['PDS_JWT_SECRET'] = ''
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bundle / 'COMPLETE').exists())
        self.assertNotIn('fixture-admin', result.stdout + result.stderr)

    def test_native_links_unreadable_paths_and_external_locations_fail_closed(self):
        self.capture()
        bad = self.root / 'blocks/bad'
        for kind in ('symlink', 'hardlink', 'unreadable', 'external', 'hold'):
            with self.subTest(kind=kind):
                self.capture()
                env = {}
                if kind == 'symlink': bad.symlink_to(self.root / 'account.sqlite')
                elif kind == 'hardlink': os.link(self.root / 'account.sqlite', bad)
                elif kind == 'unreadable': bad.mkdir(); bad.chmod(0)
                elif kind == 'external': env['PDS_ACCOUNT_DB_LOCATION'] = str(self.root.parent / 'missing')
                else:
                    (self.bundle / 'coordination').mkdir(exist_ok=True, mode=0o700)
                    (self.bundle / 'coordination/request').write_text('incumbent')
                self.assertNotEqual(self.run_helper(extra=env).returncode, 0)
                self.assertFalse((self.bundle / 'COMPLETE').exists())
                if kind == 'hold': (self.bundle / 'coordination/request').unlink()
                if bad.is_symlink() or bad.is_file(): bad.unlink()
                elif bad.exists(): bad.chmod(0o700); bad.rmdir()

    def test_unsafe_helper_path_and_uid_fail_closed(self):
        self.capture()
        (self.bundle / 'pending').symlink_to(self.root / 'actors')
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertFalse((self.bundle / 'COMPLETE').exists())
        (self.bundle / 'pending').unlink()
        self.capture()
        preload = Path(self.tmp.name) / 'wrong-uid.cjs'
        preload.write_text('process.getuid = () => 4294967294;')
        self.assertNotEqual(self.run_helper(extra={'NODE_OPTIONS': '--require=' + str(preload)}).returncode, 0)

    def test_private_sgid_directories_and_io_failures(self):
        self.capture()
        current = self.bundle / 'current'
        current.chmod(0o2700)
        self.assertEqual(self.run_helper('verify').returncode, 0)
        self.capture()
        preload = Path(self.tmp.name) / 'io-error.cjs'
        preload.write_text("const fs=require('node:fs');let n=0;const old=fs.fsyncSync;"
                           "fs.fsyncSync=(...a)=>{if(++n===3)throw Object.assign(Error(),"
                           "{code:'EIO'});return old(...a)};")
        result = self.run_helper(extra={'NODE_OPTIONS': '--require=' + str(preload)})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bundle / 'COMPLETE').exists())
        self.assertNotIn('fixture-admin', result.stdout + result.stderr)
        self.capture()

    def test_process_crash_publication_boundaries_and_recovery(self):
        preload = Path(self.tmp.name) / 'crash.cjs'
        # Process-death fault injection, not a claim of storage power-loss testing.
        preload.write_text('''const fs=require('node:fs');let n=0;
for(const name of ['fsyncSync','renameSync']) {
 const original=fs[name]; fs[name]=function(...args) {
  const value=original.apply(this,args);
  if(++n===Number(process.env.CRASH_AT)) process.exit(88);
  return value;
 };
}
''')
        for boundary in range(1, 13):
            with self.subTest(boundary=boundary):
                self.capture()
                result = self.run_helper(extra={'NODE_OPTIONS': '--require=' + str(preload),
                                                'CRASH_AT': str(boundary)})
                self.assertIn(result.returncode, (0, 88), result.stderr)
                if (self.bundle / 'COMPLETE').exists():
                    self.assertEqual(self.run_helper('verify').returncode, 0)
                else:
                    self.assertNotEqual(self.run_helper('verify').returncode, 0)
                self.capture()

    def test_podtemplate_and_projected_delivery_contract(self):
        # Base 08c8cf468. Hash fixtures work in shallow CI checkouts too.
        baseline = {
            'helmrelease.yaml': '1b6cfd717687a2c45b6738994a3c420189d21562af7220d450d4c15a8bdc56d6',
            'kopiur-recurring-runtime-patch.yaml': '5df85b5fcef6217c4830ee3a17066ae97a0459d70b9e46d7d88bf69818cd3120',
        }
        for name, digest in baseline.items():
            self.assertEqual(hashlib.sha256((APP / name).read_bytes()).hexdigest(), digest)
        generator = (SCRIPT.parent / 'kustomization.yaml').read_text()
        self.assertIn('disableNameSuffixHash: true', generator)
        self.assertIn('reloader.stakater.com/ignore: "true"', generator)
        self.assertNotIn('subPath:', (APP / 'kopiur-recurring-runtime-patch.yaml').read_text())
        policy = (APP / 'kopiur-policy.yaml').read_text()
        self.assertIn('copyMethod: Snapshot', policy)
        self.assertIn('continueOnFailure: false', policy)
        self.assertIn('flock -n', policy)
        self.assertNotIn('ignoreErrors', policy)
        self.assertIn('  suspend: true', policy)
        schedule = (APP / 'kopiur-original.yaml').read_text().split('kind: SnapshotSchedule', 1)[1].split('---', 1)[0]
        self.assertIn('    suspend: true', schedule)
        self.assertIn('runOnCreate: false', schedule)


if __name__ == '__main__':
    unittest.main()
