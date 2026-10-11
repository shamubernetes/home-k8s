import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_grimmory_original as original


class OriginalTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'original': {'snapshots': {'nas': '1'*32, 'r2': '2'*32},
            'generation': '8dbce9ee-1a02-4860-8fd0-eb2b26fbf461',
            'sql_sha256': 'a'*64, 'runtime_sha256': 'b'*64, 'data_sha256': 'c'*64}}

    def test_exact_contract(self):
        self.assertEqual(original.validate_original(self.payload), self.payload['original'])

    def test_rejects_reused_manifest(self):
        self.payload['original']['snapshots']['r2'] = '1'*32
        with self.assertRaises(ValueError):original.validate_original(self.payload)

    def test_rejects_missing_backend(self):
        del self.payload['original']['snapshots']['r2']
        with self.assertRaises(ValueError):original.validate_original(self.payload)

    def test_rejects_mutable_or_short_manifest(self):
        for value in ('latest', '1'*31, 'A'*32, '../original'):
            self.payload['original']['snapshots']['nas'] = value
            with self.assertRaises(ValueError):original.validate_original(self.payload)

    def test_rejects_digest_or_generation_omission(self):
        for key in ('generation', 'sql_sha256', 'runtime_sha256', 'data_sha256'):
            candidate = copy.deepcopy(self.payload)
            del candidate['original'][key]
            with self.assertRaises(ValueError):original.validate_original(candidate)

    def test_rejects_unrequested_contract_fields(self):
        self.payload['original']['production_mutation'] = True
        with self.assertRaises(ValueError):original.validate_original(self.payload)

    def test_embedded_programs_compile(self):
        for script in (original.CHECK, original.BOOT):compile(script, '<isolated-original>', 'exec')

    def execute_check(self, file_mode=0o640, root_mode=0o2750):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)/'restored'
            runtime = root/'mariadb-config/kopiur/runtime'
            runtime.mkdir(parents=True)
            source = root/'grimmory-data'
            source.mkdir()
            (source/'state.bin').write_bytes(b'original-state')
            (source/'state.bin').chmod(file_mode)
            source.chmod(root_mode)
            with tarfile.open(runtime/'app-data.tar', 'w') as archive:
                archive.add(source, arcname='.')
            for name in ('application.env0', 'application.argv0', 'process-start.txt', 'runtime.txt'):
                (runtime/name).write_bytes(b'native-runtime')
            def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
            roster = sorted(runtime.iterdir())
            (runtime/'runtime.sha256').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in roster))
            base = runtime.parent
            (base/'grimmory.sql').write_bytes(b'native-sql')
            expected = copy.deepcopy(self.payload['original'])
            expected['sql_sha256'] = digest(base/'grimmory.sql')
            expected['runtime_sha256'] = digest(runtime/'runtime.sha256')
            (base/'COMPLETE').write_text(expected['generation']+'\n')
            (base/'manifest.txt').write_text('format=mariadb-logical-runtime-v2\nbase_tables=73\n'
                +'runtime_generation='+expected['generation']+'\nsql_sha256='+expected['sql_sha256']
                +'\nruntime_manifest_sha256='+expected['runtime_sha256']+'\n')
            script = original.CHECK.replace("'/data/restored'", repr(str(root))).replace(
                "'/appdata'", repr(str(Path(temporary)/'appdata')))
            return subprocess.run([sys.executable, '-c', script], input=json.dumps(expected),
                text=True, capture_output=True, timeout=10)

    def test_original_check_preserves_setgid_directories(self):
        for mode in (0o2750, 0o2775):
            result = self.execute_check(root_mode=mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['app_entries'], 2)

    def test_original_check_rejects_privileged_files(self):
        for mode in (0o2640, 0o4640):
            result = self.execute_check(file_mode=mode)
            self.assertNotEqual(result.returncode, 0)

    def test_reader_and_boot_boundaries(self):
        source = Path(original.__file__).read_text()
        self.assertIn('--readonly', source)
        self.assertIn("native.docker('rm','-f',reader)", source)
        self.assertIn("'--network=none'", source)
        self.assertIn("network='container:'+database", source)
        self.assertNotIn('k snapshot delete', source)
        self.assertNotIn('k snapshot create', source)
        self.assertNotIn('rclone purge', source)
        self.assertIn("'shared_books_bookdrop_qualified':False", source)


if __name__ == '__main__':unittest.main()
