"""Focused F1/F2 regressions; run only on the approved remote runner.

python3 -m unittest discover -s scripts/tests -p test_k8s92_tubearchivist.py -v
Then run scripts/k8s92-mixed-db-test.py --app tubearchivist for real pinned
Django/ES startup, native capture, source removal, restore and normal startup.

Pinned schema evidence:
https://github.com/tubearchivist/tubearchivist/blob/v0.5.12/backend/config/settings.py
https://github.com/tubearchivist/tubearchivist/blob/v0.5.12/backend/user/migrations/0001_initial.py
https://github.com/tubearchivist/tubearchivist/blob/v0.5.12/backend/config/management/commands/ta_startup.py
"""
import ast
import base64
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'ta_backup', ROOT / 'kubernetes/apps/media/tubearchivist/app/backup.py')
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)
RECOVERY_SPEC = importlib.util.spec_from_file_location(
    'ta_recovery', ROOT / 'kubernetes/apps/media/tubearchivist/app/recovery.py')
assert RECOVERY_SPEC is not None and RECOVERY_SPEC.loader is not None
RECOVERY = importlib.util.module_from_spec(RECOVERY_SPEC)
with patch.dict(sys.modules, {'backup': BACKUP}):
    RECOVERY_SPEC.loader.exec_module(RECOVERY)

# Load only pure helpers. Importing the Docker runner would create scratch and
# change umask, neither of which is needed by these focused regressions.
RUNNER = ROOT / 'scripts/k8s92-mixed-db-test.py'
HELPERS = {'ta_fixture_documents', 'ta_fixture_script', 'ta_failure_markers'}
TREE = ast.parse(RUNNER.read_text())
NAMESPACE = {'json': json}
exec(compile(ast.Module(body=[node for node in TREE.body
                             if isinstance(node, ast.FunctionDef) and node.name in HELPERS],
                        type_ignores=[]), str(RUNNER), 'exec'), NAMESPACE)


class SQLiteSchemaTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('TMPDIR', Path.home() / '.hermes/cache/scratch'))
        scratch.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='ta-schema-', dir=scratch)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / 'db.sqlite3'
        with sqlite3.connect(self.path) as db:
            # Account's scalar columns match the pinned initial migration. The
            # runtime fixture independently checks Django's actual model/table.
            db.executescript("""
                CREATE TABLE user_account (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, password VARCHAR(128) NOT NULL,
                    last_login DATETIME NULL, is_superuser BOOL NOT NULL,
                    name VARCHAR(150) NOT NULL UNIQUE, is_staff BOOL NOT NULL);
                INSERT INTO user_account VALUES (1, '!synthetic-unusable', NULL, 1, 'fixture', 1);
                CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT, name TEXT, applied DATETIME);
                INSERT INTO django_migrations VALUES (1, 'user', '0001_initial', '2025-06-15');
                CREATE TABLE task_customperiodictask (periodictask_ptr_id INTEGER PRIMARY KEY, task_config TEXT);
                INSERT INTO task_customperiodictask VALUES (1, '{}');
            """)

    def test_custom_user_schema_without_auth_user_is_accepted_read_only(self):
        before = self.path.read_bytes()
        counts = BACKUP.sqlite_inventory(self.path)
        self.assertEqual(counts['user_account'], 1)
        self.assertEqual(counts['django_migrations'], 1)
        self.assertEqual(counts['task_customperiodictask'], 1)
        self.assertNotIn('auth_user', counts)
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_auth_user_cannot_replace_the_real_account_table(self):
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE user_account RENAME TO auth_user')
        with self.assertRaisesRegex(RuntimeError, 'missing Django state tables'):
            BACKUP.sqlite_inventory(self.path)

    def test_required_migration_and_task_tables_are_still_enforced(self):
        for table in ('django_migrations', 'task_customperiodictask'):
            with self.subTest(table=table):
                with sqlite3.connect(self.path) as db:
                    db.execute('ALTER TABLE ' + table + ' RENAME TO missing_table')
                try:
                    with self.assertRaisesRegex(RuntimeError, 'missing Django state tables'):
                        BACKUP.sqlite_inventory(self.path)
                finally:
                    with sqlite3.connect(self.path) as db:
                        db.execute('ALTER TABLE missing_table RENAME TO ' + table)

    def test_corrupt_database_is_rejected(self):
        self.path.write_bytes(b'not a SQLite database')
        with self.assertRaises(sqlite3.DatabaseError):
            BACKUP.sqlite_inventory(self.path)

    def test_missing_database_is_not_created(self):
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            BACKUP.sqlite_inventory(self.path)
        self.assertFalse(self.path.exists())


class AppFixtureTests(unittest.TestCase):
    def setUp(self):
        self.records = NAMESPACE['ta_fixture_documents']()
        self.docs = {index: doc for index, _, doc in self.records}

    def test_all_six_content_indices_have_distinct_native_documents(self):
        self.assertEqual(set(self.docs), set(BACKUP.INDICES) - {'ta_config'})
        self.assertEqual(len(self.records), len(self.docs))
        id_fields = {'ta_channel': 'channel_id', 'ta_video': 'youtube_id',
                     'ta_download': 'youtube_id', 'ta_playlist': 'playlist_id',
                     'ta_subtitle': 'subtitle_fragment_id', 'ta_comment': 'youtube_id'}
        for index, doc_id, document in self.records:
            with self.subTest(index=index):
                self.assertEqual(doc_id, document[id_fields[index]])
                self.assertNotIn('lane_fixture', document)
                self.assertNotIn('lane_id', document)
                self.assertIn('雪', json.dumps(document, ensure_ascii=False))

    def test_startup_video_migrations_have_nested_channel_and_complete_stats(self):
        video = self.docs['ta_video']
        self.assertEqual(video['channel'], self.docs['ta_channel'])
        self.assertEqual(video['channel']['channel_tabs'], ['videos', 'streams', 'shorts'])
        self.assertEqual(set(video['stats']), {'like_count', 'average_rating', 'view_count', 'dislike_count'})
        for value in video['stats'].values():
            self.assertIn(type(value), (int, float))
        self.assertTrue(video['description'])
        self.assertTrue(video['channel']['channel_description'])
        for field in ('channel_banner_url', 'channel_thumb_url', 'channel_tvart_url'):
            self.assertIsInstance(video['channel'][field], str)
        self.assertEqual(self.docs['ta_playlist']['playlist_sort_order'], 'top')
        self.assertIsInstance(self.docs['ta_playlist']['playlist_description'], str)

    def test_relations_and_inactive_download_are_consistent(self):
        video = self.docs['ta_video']
        channel = self.docs['ta_channel']
        playlist = self.docs['ta_playlist']
        download = self.docs['ta_download']
        self.assertEqual(video['playlist'], [playlist['playlist_id']])
        self.assertEqual(playlist['playlist_entries'][0]['youtube_id'], video['youtube_id'])
        self.assertEqual(playlist['playlist_channel_id'], channel['channel_id'])
        for index, field in (('ta_download', 'channel_id'), ('ta_subtitle', 'subtitle_channel_id'),
                             ('ta_comment', 'comment_channel_id')):
            self.assertEqual(self.docs[index][field], channel['channel_id'])
        for index in ('ta_subtitle', 'ta_comment'):
            self.assertEqual(self.docs[index]['youtube_id'], video['youtube_id'])
        self.assertEqual(video['comment_count'], len(self.docs['ta_comment']['comment_comments']))
        self.assertNotEqual(download['youtube_id'], video['youtube_id'])
        self.assertEqual(download['status'], 'ignore')
        self.assertFalse(download['auto_start'])
        self.assertFalse(channel['channel_subscribed'])
        self.assertFalse(playlist['playlist_subscribed'])

    def test_seed_and_readback_scripts_compile_without_startup_bypass(self):
        for seed in (False, True):
            script = NAMESPACE['ta_fixture_script'](seed=seed)
            compile(script, '<remote-ta-fixture>', 'exec')
            self.assertIn("account._meta.db_table == 'user_account'", script)
            self.assertIn("contract.request(path)['_source'] == document", script)
            self.assertNotIn('TA_MIG_SKIP', script)


class RedisRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.key = base64.b64encode(b'\x00queue').decode()
        self.record = {'dump': base64.b64encode(b'\x00\xffnative').decode(),
                       'logical': base64.b64encode(b'6:string3:raw').decode(), 'expires_at_ms': -1}
        self.records = {self.key: self.record}

    def test_binary_inventory_and_persistent_expiry(self):
        self.assertEqual(BACKUP.check_redis_inventory(self.records), self.records)

    def test_invalid_encoding_and_expiry_rejected(self):
        for record in ({'dump': '?', 'expires_at_ms': -1},
                       {'dump': self.record['dump'], 'expires_at_ms': -2},
                       {'dump': self.record['dump'], 'expires_at_ms': True}):
            with self.subTest(record=record), self.assertRaises(Exception):
                BACKUP.check_redis_inventory({self.key: record})

    def test_value_key_and_expiry_mutations_rejected(self):
        for after in ({}, {self.key: dict(self.record, logical=base64.b64encode(b'changed').decode())},
                      {self.key: dict(self.record, expires_at_ms=42)}):
            with self.subTest(after=after), self.assertRaises(RuntimeError):
                BACKUP.compare_redis_inventory(self.records, after)

    def test_expiry_clock_resolution_tolerance_does_not_allow_persistence_change(self):
        before = {self.key: dict(self.record, expires_at_ms=100000)}
        BACKUP.compare_redis_inventory(before, {self.key: dict(self.record, expires_at_ms=100001)})
        with self.assertRaises(RuntimeError):
            BACKUP.compare_redis_inventory(before, self.records)

    def test_native_serialization_order_can_change_without_value_change(self):
        after = {self.key: dict(self.record, dump=base64.b64encode(b'different-order').decode())}
        BACKUP.compare_redis_inventory(self.records, after)

    def test_source_and_nonempty_targets_rejected_without_writes(self):
        for run_id, dbsize in (('source', 0), ('fresh', 1)):
            client = Mock()
            client.info.return_value = {'run_id': run_id}
            client.connection_pool.connection_kwargs = {'host': 'fresh', 'port': 6379, 'db': 15}
            client.eval.return_value = 0
            with patch.object(BACKUP, 'verify', return_value={'redis': {'source_instance_id': 'source', 'source_endpoint_sha256': 'old-source'}}), \
                 patch.object(Path, 'read_text', return_value=json.dumps(self.records)), \
                 patch.object(BACKUP, 'digest', return_value='synthetic-digest'), \
                 patch.object(BACKUP, 'redis_client', return_value=client), \
                 patch.dict(os.environ, {'K8S92_ISOLATED_RESTORE': 'YES'}):
                with self.assertRaises(RuntimeError):
                    BACKUP.restore_redis(Path('/synthetic-bundle'))
            client.restore.assert_not_called()
            client.flushdb.assert_not_called()
            client.flushall.assert_not_called()
            if run_id == 'source':
                client.eval.assert_not_called()
            else:
                self.assertEqual(client.eval.call_count, 1)
                self.assertIn("redis.call('DBSIZE')", client.eval.call_args.args[0])
                self.assertEqual(client.eval.call_args.args[1:3], (1, BACKUP.RECOVERY_KEY))

    def test_explicit_isolation_required(self):
        with patch.object(BACKUP, 'verify', return_value={'redis': {'source_instance_id': 'source'}}), \
             patch.dict(os.environ, {'K8S92_ISOLATED_RESTORE': 'NO'}), \
             patch.object(BACKUP, 'redis_client') as client:
            with self.assertRaises(RuntimeError):
                BACKUP.restore_redis(Path('/synthetic-bundle'))
            client.assert_not_called()


    def test_dragonfly_instance_identity_uses_supported_replication_field(self):
        client = Mock()
        client.info.side_effect = [{}, {'master_replid': 'fixture-lineage'}]
        self.assertEqual(BACKUP.redis_instance_id(client), 'fixture-lineage')
        self.assertEqual(client.info.call_args_list, [call('server'), call('replication')])

    def test_expiry_eligibility_uses_redis_clock_not_restore_host_clock(self):
        client = Mock()
        client.time.return_value = (100, 500000)
        records = {'expired': dict(self.record, expires_at_ms=100500),
                   'live': dict(self.record, expires_at_ms=100501),
                   'persistent': self.record}
        self.assertEqual(set(BACKUP.unexpired_records(client, records)), {'live', 'persistent'})

    def test_restore_scan_permits_only_proven_natural_expiration(self):
        client = Mock()
        client.scan_iter.return_value = [b'\x00queue']
        client.eval.return_value = []
        client.time.return_value = (100, 0)
        expired = {self.key: dict(self.record, expires_at_ms=100000)}
        self.assertEqual(BACKUP.redis_inventory(client, expiry_records=expired), {})
        for records in (None, self.records, {}):
            with self.assertRaises(RuntimeError):
                BACKUP.redis_inventory(client, expiry_records=records)

    def test_restarted_empty_source_endpoint_is_rejected_before_claim(self):
        client = Mock()
        client.info.return_value = {'run_id': 'restarted-source'}
        client.connection_pool.connection_kwargs = {'host': 'source', 'port': 6379, 'db': 15}
        manifest = {'redis': {'source_instance_id': 'old-source',
                             'source_endpoint_sha256': BACKUP.redis_endpoint_id(client)}}
        with patch.object(BACKUP, 'verify', return_value=manifest), \
             patch.object(BACKUP, 'redis_client', return_value=client), \
             patch.dict(os.environ, {'K8S92_ISOLATED_RESTORE': 'YES'}):
            with self.assertRaisesRegex(RuntimeError, 'refuse source Redis endpoint'):
                BACKUP.restore_redis(Path('/synthetic'))
        client.eval.assert_not_called()

    def test_wrongtype_task_metadata_is_held_without_get_or_rewrite(self):
        client = Mock()
        client.type.return_value = b'hash'
        self.assertEqual(RECOVERY.disposition(b'celery-task-meta-id', client),
                         'invalid-or-unknown-no-replay')
        client.get.assert_not_called()

    def test_native_held_startup_refuses_production_es_before_native_mutation(self):
        client = Mock()
        client.info.return_value = {'run_id': 'target'}
        manifest = {'redis': {'source_instance_id': 'source'}, 'es_version': '8.19.22'}
        with patch.object(BACKUP, 'verify', return_value=manifest), \
             patch.object(BACKUP, 'redis_client', return_value=client), \
             patch.object(BACKUP, 'recovery_owner', return_value=b'owner'), \
             patch.object(Path, 'read_text', return_value='{}'), \
             patch.object(BACKUP, 'verify_restored_redis'), \
             patch.object(BACKUP, 'request', return_value={'cluster_name': 'production'}), \
             patch.object(BACKUP, 'digest') as digest, \
             patch.dict(os.environ, {'K8S92_ISOLATED_RESTORE': 'YES'}):
            with self.assertRaisesRegex(RuntimeError, 'refuse non-disposable Elasticsearch'):
                RECOVERY.hold_startup(Path('/bundle'), Path('/ledger.json'))
        digest.assert_not_called()

    def test_recovery_marker_only_is_excluded_from_content_comparison(self):
        marker = base64.b64encode(BACKUP.RECOVERY_KEY).decode()
        with patch.object(BACKUP, 'redis_inventory', return_value=dict(self.records, **{marker: self.record})):
            self.assertEqual(BACKUP.restored_inventory(Mock()), self.records)

    def test_terminal_ambiguous_and_invalid_native_records_are_not_replayed(self):
        client = Mock()
        client.type.return_value = b'string'
        for status in ('SUCCESS', 'FAILURE', 'FAILED', 'REVOKED'):
            client.get.return_value = json.dumps({'status': status}).encode()
            self.assertEqual(RECOVERY.disposition(b'celery-task-meta-id', client), 'terminal-no-replay')
        for status in ('PENDING', 'STARTED', 'RETRY'):
            client.get.return_value = json.dumps({'status': status, 'command': 'STOP'}).encode()
            self.assertEqual(RECOVERY.disposition(b'celery-task-meta-id', client), 'interrupted-requires-reconciliation')
        for data in (b'not-json', b'[]', b'null', b'{}'):
            client.get.return_value = data
            self.assertEqual(RECOVERY.disposition(b'celery-task-meta-id', client), 'invalid-or-unknown-no-replay')
        client.set.assert_not_called()
        client.delete.assert_not_called()

    def test_progress_native_queues_and_unknown_broker_keys_are_preserved(self):
        client = Mock()
        self.assertEqual(RECOVERY.disposition(b'ta:message:download:id', client), 'progress-preserved')
        self.assertEqual(RECOVERY.disposition(b'ta:reindex:ta_video', client), 'application-intent-preserved')
        self.assertEqual(RECOVERY.disposition(b'unacked', client), 'opaque-or-broker-no-replay')
        self.assertEqual(RECOVERY.disposition(b'\x00unknown', client), 'opaque-or-broker-no-replay')
        client.assert_not_called()


class SyntheticDiagnosticTests(unittest.TestCase):
    def test_only_fixed_labels_escape_private_logs(self):
        private = ('RuntimeError: missing Django state tables\n'
                   'SECRET=do-not-publish https://user:password@example.invalid/private\n'
                   'in sqlite_inventory\nscript_exception null_pointer_exception')
        labels = NAMESPACE['ta_failure_markers'](private)
        self.assertEqual(labels, ['missing-django-state-tables', 'es-script-error',
                                  'es-null-pointer', 'sqlite-inventory-frame'])
        public = json.dumps(labels)
        self.assertNotIn('SECRET', public)
        self.assertNotIn('password', public)
        self.assertNotIn('example.invalid', public)

    def test_unknown_errors_do_not_leak_messages(self):
        self.assertEqual(NAMESPACE['ta_failure_markers']('a private error message'), ['unclassified'])


if __name__ == '__main__':
    unittest.main()
