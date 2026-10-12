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

    def execute_check(self, file_mode=0o640, root_mode=0o2750, books=None):
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
            expected = copy.deepcopy(self.payload['original'])
            if books:
                physical = Path(temporary)/'source-books'
                physical.mkdir()
                (physical/'bookdrop').mkdir()
                (physical/'a book.bin').write_bytes(b'original-physical-book')
                (physical/'a book.bin').chmod(0o640)
                physical.chmod(0o2775)
                with tarfile.open(runtime/'books.tar', 'w') as archive:
                    archive.add(physical, arcname='.')
                    if books == 'traversal':
                        archive.addfile(tarfile.TarInfo('../outside'))
                    if books == 'symlink':
                        link = tarfile.TarInfo('link')
                        link.type = tarfile.SYMTYPE
                        link.linkname = '/outside'
                        archive.addfile(link)
                (runtime/'runtime.txt').write_text('format=grimmory-runtime-books-v2\n')
                (runtime/'books.txt').write_text('format=grimmory-books-v1\nsource=/books\n'
                    'bookdrop_subpath=bookdrop\nconsistency=repeated-byte-metadata-stability-not-writer-fenced\n')
                expected['books_sha256'] = digest(runtime/'books.tar')
                if books == 'digest':expected['books_sha256'] = 'f'*64
                if books == 'omitted':del expected['books_sha256']
            roster = sorted(runtime.iterdir())
            (runtime/'runtime.sha256').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in roster))
            base = runtime.parent
            (base/'grimmory.sql').write_bytes(b'native-sql')
            expected['sql_sha256'] = digest(base/'grimmory.sql')
            expected['runtime_sha256'] = digest(runtime/'runtime.sha256')
            (base/'COMPLETE').write_text(expected['generation']+'\n')
            (base/'manifest.txt').write_text('format=mariadb-logical-runtime-v2\nbase_tables=73\n'
                +'runtime_generation='+expected['generation']+'\nsql_sha256='+expected['sql_sha256']
                +'\nruntime_manifest_sha256='+expected['runtime_sha256']+'\n')
            script = original.CHECK.replace("'/data/restored'", repr(str(root))).replace(
                "'/appdata'", repr(str(Path(temporary)/'appdata'))).replace(
                "'/bookdata'", repr(str(Path(temporary)/'bookdata')))
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

    def test_books_original_contract(self):
        self.payload['original']['books_sha256'] = 'd'*64
        original.validate_original(self.payload)
        self.payload['original']['books_sha256'] = 'latest'
        with self.assertRaises(ValueError):original.validate_original(self.payload)

    def test_books_original_bytes_metadata_and_bookdrop(self):
        result = self.execute_check(books='valid')
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['books_files'], 1)
        self.assertEqual(receipt['books_entries'], 3)
        self.assertTrue(receipt['books_original_bytes_metadata_equal'])
        self.assertTrue(receipt['bookdrop_subtree_restored'])
        self.assertFalse(receipt['shared_writer_fencing_qualified'])

    def test_books_original_rejects_unsafe_or_unbound_payload(self):
        for case in ('traversal', 'symlink', 'digest', 'omitted'):
            self.assertNotEqual(self.execute_check(books=case).returncode, 0, case)

    def test_normalized_network_reference(self):
        identifier = 'd'*64
        host = {'PortBindings': {}, 'LogConfig': {'Type': 'none'},
                'NetworkMode': 'container:'+identifier}
        original.verify_network({'HostConfig': host}, identifier)
        host['NetworkMode'] = 'none'
        original.verify_network({'HostConfig': host}, identifier, database=True)
        for mode in ('bridge', 'host', 'container:'+('e'*64), 'container:friendly-name'):
            host['NetworkMode'] = mode
            with self.assertRaises(AssertionError):original.verify_network({'HostConfig': host}, identifier)
        host['NetworkMode'] = 'container:'+identifier
        host['PortBindings'] = {'6060/tcp': [{'HostPort': '6060'}]}
        with self.assertRaises(AssertionError):original.verify_network({'HostConfig': host}, identifier)

    def test_consumer_recreation_order_and_inputs(self):
        from unittest.mock import Mock, patch
        drill = Mock()
        drill.containers = ['old-app', 'database']
        drill.start_consumer.return_value = 'new-app'
        drill.http.return_value = {'status': 'up'}
        events = []
        def docker(*args):events.append(args)
        def start(*args, **kwargs):
            self.assertEqual(events, [('rm', '-f', 'old-app'), ('restart', 'database')])
            self.assertNotIn('old-app', drill.containers)
            self.assertEqual(args, ('nas','database','app-volume','tools-volume',None))
            return 'new-app'
        drill.start_consumer.side_effect = start
        def wait(check):self.assertTrue(check())
        with patch.object(original.native, 'docker', side_effect=docker), \
                patch.object(original, 'wait', side_effect=wait), \
                patch.object(original, 'data_digest', return_value='c'*64):
            self.assertEqual(original.recreate_consumer(drill,'old-app','database',
                'nas','app-volume','tools-volume','c'*64), 'new-app')

    def test_consumer_recreation_rejects_data_drift(self):
        from unittest.mock import Mock, patch
        drill = Mock()
        drill.containers = ['old-app', 'database']
        drill.start_consumer.return_value = 'new-app'
        drill.http.return_value = {}
        with patch.object(original.native, 'docker'), \
                patch.object(original, 'wait', side_effect=lambda check: check()), \
                patch.object(original, 'data_digest', side_effect=['c'*64, 'd'*64]):
            with self.assertRaisesRegex(ValueError, 'data differs'):
                original.recreate_consumer(drill,'old-app','database','nas','app','tools','c'*64)

    def test_books_consumer_exact_duplicate_volume_and_isolation(self):
        from unittest.mock import patch
        drill=original.Restore()
        with patch.object(original.native,'docker') as docker:
            app=drill.start_consumer('nas','db','app','tools','books')
        args=docker.call_args.args
        self.assertIn(app,drill.containers)
        for value in ('container:db','568:568','--log-driver=none','--read-only',
                      'no-new-privileges','books:/books:ro',
                      'type=volume,src=books,dst=/bookdrop,volume-nocopy,volume-subpath=bookdrop,readonly'):
            self.assertIn(value,args)
        self.assertNotIn('--privileged',args)
        self.assertEqual(args[-3:],('sh',original.GRIMM,'/tools/boot.sh'))

    def test_legacy_consumer_uses_existing_start(self):
        from unittest.mock import patch
        drill=original.Restore()
        with patch.object(drill,'start',return_value='legacy') as start:
            self.assertEqual(drill.start_consumer('nas','db','app','tools'),'legacy')
        self.assertEqual(start.call_args.args[2],{'app':'/app/data','tools':'/tools:ro'})

    def test_books_consumer_readback_rejects_writable_or_wrong_volume(self):
        from unittest.mock import patch
        mounts=[{'Destination':'/books','Name':'books','RW':False},
                {'Destination':'/bookdrop','Name':'books','RW':False}]
        for case in ('valid','writable','wrong','missing'):
            sample=copy.deepcopy(mounts)
            if case=='writable':sample[1]['RW']=True
            if case=='wrong':sample[1]['Name']='another'
            if case=='missing':sample.pop()
            with patch.object(original.native,'docker',return_value=json.dumps([{'Mounts':sample}])) as docker:
                if case=='valid':
                    original.verify_consumer_books('native-app','d'*64)
                    args=docker.call_args.args
                    self.assertIn('native-app:ro',args)
                    self.assertIn('--network=none',args)
                    compile(args[-2],'<books-mounted-verifier>','exec')
                else:
                    with self.assertRaises(AssertionError):original.verify_consumer_books('native-app','d'*64)

    def test_books_survive_database_consumer_recreation(self):
        from unittest.mock import Mock,patch
        drill=Mock();drill.containers=['old','db'];drill.start_consumer.return_value='new'
        with patch.object(original.native,'docker'),patch.object(original,'wait',side_effect=lambda check:check()), \
                patch.object(original,'data_digest',return_value='c'*64):
            original.recreate_consumer(drill,'old','db','nas','app','tools','c'*64,'books')
        drill.start_consumer.assert_called_once_with('nas','db','app','tools','books')

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
