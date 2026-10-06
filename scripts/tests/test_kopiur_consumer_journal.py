"""Real isolated SQLite writer processes, not production app acceptance."""
import copy
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_consumer_journal import ConsumerJournal
from kopiur_shared import InvalidEvidence
from test_kopiur_consumer_generation import fixture


@contextmanager
def database(path):
    # sqlite's transaction context commits/rolls back but does not close its FD.
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            yield connection


class SQLiteConsumer:
    """Test-only native transaction admission. No production resource adapter."""
    def __init__(self, path):
        self.path = str(path)

    def observe(self):
        with database(self.path) as connection:
            # Serializes with every admitted write. Once admission is closed,
            # acquiring this native lock proves all older transactions drained.
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT identity, admission, epoch, generation, writes FROM state').fetchone()
        return {'identity': row[0], 'admission': bool(row[1]), 'epoch': row[2],
                'generation': row[3], 'writes': row[4], 'active': 0}

    def hold(self, identity, epoch, generation):
        with database(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT identity FROM state').fetchone()
            if row[0] != identity:
                raise InvalidEvidence('changed fixture identity')
            connection.execute('UPDATE state SET admission=0, epoch=?, generation=?', (epoch, generation))

    def resume(self, identity, epoch, generation, prior):
        with database(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT identity, epoch, generation FROM state').fetchone()
            if row[0] != identity or (row[1] is not None and row[1:] != (epoch, generation)):
                raise InvalidEvidence('fixture hold ownership changed')
            # Clearing the completed hold permits next-generation intent
            # recovery before this consumer receives its next hold.
            connection.execute('UPDATE state SET admission=?, epoch=NULL, generation=NULL', (int(prior),))


def writer(path):
    with database(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('UPDATE state SET writes=writes+1 WHERE admission=1')
    print('ready', flush=True)
    while True:
        with database(path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('UPDATE state SET writes=writes+1 WHERE admission=1')
        time.sleep(0.01)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = 'k8s92-nonrel-' + uuid.uuid4().hex + '-source'
        self.path = self.root / 'journal.json'
        self.adapters = {}
        self.processes = []
        ledger, _ = fixture()
        for index, item in enumerate(ledger['applications']):
            path = self.root / ('consumer' + str(index) + '.db')
            with database(path) as connection:
                connection.execute('CREATE TABLE state(identity TEXT, admission INTEGER, epoch TEXT, generation TEXT, writes INTEGER)')
                connection.execute('INSERT INTO state VALUES(?,1,NULL,NULL,0)', (uuid.uuid4().hex,))
            process = subprocess.Popen([sys.executable, __file__, 'writer', str(path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.append(process)
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.adapters[item['id']] = SQLiteConsumer(path)

    def tearDown(self):
        for process in self.processes:
            process.terminate()
            process.communicate(timeout=10)
        self.directory.cleanup()

    def begin(self, timeout=60):
        with ConsumerJournal(self.path, self.source) as journal:
            epoch, generation = journal.begin(self.adapters, timeout)
            journal.hold(epoch, self.adapters)
        ledger, receipt = fixture()
        receipt['generation'] = generation
        for item in receipt['stores'] + receipt['consumers']:
            item['generation'] = generation
        for consumer in receipt['consumers']:
            for point in consumer['points']:
                point['generation'] = generation
        return epoch, ledger, receipt

    def watchdog(self):
        paths = json.dumps({app: adapter.path for app, adapter in self.adapters.items()})
        return subprocess.run([sys.executable, __file__, 'watchdog', str(self.path), self.source, paths],
                              capture_output=True, text=True, timeout=15)

    def expire(self):
        with ConsumerJournal(self.path, self.source) as journal:
            state = journal.read()
            assert state is not None
            state['deadline'] = time.time() - 1
            journal.save(state)

    def test_real_writers_drained_and_locked_closed_receipt_admitted(self):
        epoch, ledger, receipt = self.begin()
        before = [adapter.observe()['writes'] for adapter in self.adapters.values()]
        self.assertTrue(all(value > 0 for value in before))
        time.sleep(0.05)
        self.assertEqual(before, [adapter.observe()['writes'] for adapter in self.adapters.values()])
        with ConsumerJournal(self.path, self.source) as journal:
            journal.publish(epoch, ledger, receipt, self.adapters)
            result = journal.admit(epoch, ledger, receipt, self.adapters)
            self.assertFalse(result['release_authorized'])
            self.assertFalse(result['production_recovery_accepted'])

    def test_receipt_assertion_cannot_override_native_admission(self):
        epoch, ledger, receipt = self.begin()
        adapter = next(iter(self.adapters.values()))
        with database(adapter.path) as connection:
            connection.execute('UPDATE state SET admission=1')
        with ConsumerJournal(self.path, self.source) as journal:
            with self.assertRaises(InvalidEvidence):
                journal.publish(epoch, ledger, receipt, self.adapters)

    def test_closed_receipt_cannot_override_journal_or_native_state(self):
        epoch, ledger, receipt = self.begin()
        with ConsumerJournal(self.path, self.source) as journal:
            journal.publish(epoch, ledger, receipt, self.adapters)
            changed = copy.deepcopy(receipt)
            changed['stores'][0]['nas']['snapshot_id'] = 'e' * 32
            with self.assertRaises(InvalidEvidence):
                journal.admit(epoch, ledger, changed, self.adapters)
            adapter = next(iter(self.adapters.values()))
            with database(adapter.path) as connection:
                connection.execute('UPDATE state SET epoch=?', ('f' * 32,))
            with self.assertRaises(InvalidEvidence):
                journal.admit(epoch, ledger, receipt, self.adapters)

    def test_independent_timeout_watchdog_revokes_and_restores(self):
        epoch, ledger, receipt = self.begin()
        self.expire()
        result = self.watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'recovered': True})
        self.assertTrue(all(adapter.observe()['admission'] for adapter in self.adapters.values()))
        with ConsumerJournal(self.path, self.source) as journal:
            state = journal.read()
            assert state is not None
            self.assertTrue(state['revoked'])
            self.assertEqual(state['phase'], 'released')
            with self.assertRaises(InvalidEvidence):
                journal.publish(epoch, ledger, receipt, self.adapters)
        self.assertEqual(json.loads(self.watchdog().stdout), {'recovered': False})

    def test_actual_elapsed_timeout_and_unexpired_noop(self):
        self.begin(timeout=1)
        self.assertEqual(json.loads(self.watchdog().stdout), {'recovered': False})
        time.sleep(1.1)
        self.assertEqual(json.loads(self.watchdog().stdout), {'recovered': True})

    def test_resume_without_ack_remains_revoked_for_retry(self):
        self.begin()
        self.expire()
        adapter = next(iter(self.adapters.values()))
        original = adapter.resume
        def no_ack(identity, epoch, generation, prior):
            pass
        adapter.resume = no_ack
        with ConsumerJournal(self.path, self.source) as journal:
            with self.assertRaises(InvalidEvidence):
                journal.watchdog_recover(self.adapters)
            state = journal.read()
            assert state is not None
            self.assertEqual(state['phase'], 'revoked')
        adapter.resume = original
        self.assertEqual(self.watchdog().returncode, 0)

    def test_partial_hold_intent_recovers_after_collector_process_loss(self):
        with ConsumerJournal(self.path, self.source) as journal:
            journal.begin(self.adapters, 60)
        command = [sys.executable, __file__, 'crash-hold', str(self.path), self.source,
                   json.dumps({app: adapter.path for app, adapter in self.adapters.items()})]
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23)
        with ConsumerJournal(self.path, self.source) as journal:
            state = journal.read()
            assert state is not None
            self.assertEqual(state['phase'], 'intent')
        self.assertEqual(sum(item.observe()['admission'] for item in self.adapters.values()), 1)
        self.expire()
        self.assertEqual(self.watchdog().returncode, 0)
        self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))

    def test_failed_or_timed_out_drain_cannot_publish_and_recovers(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                with ConsumerJournal(self.path, self.source) as journal:
                    epoch, _ = journal.begin(self.adapters, 1 if timeout else 60)
                    adapter = list(self.adapters.values())[-1]
                    original = adapter.hold
                    def incomplete(identity, epoch, generation):
                        if timeout:
                            time.sleep(1.1)
                            original(identity, epoch, generation)
                        else:
                            raise InvalidEvidence('injected native drain failure')
                    adapter.hold = incomplete
                    try:
                        with self.assertRaises(InvalidEvidence):
                            journal.hold(epoch, self.adapters)
                        _, receipt = fixture()
                        with self.assertRaises(InvalidEvidence):
                            journal.publish(epoch, fixture()[0], receipt, self.adapters)
                    finally:
                        adapter.hold = original
                self.expire()
                result = self.watchdog()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))

    def test_all_resumed_but_released_journal_ack_lost(self):
        self.begin()
        self.expire()
        command = [sys.executable, __file__, 'crash-released', str(self.path), self.source,
                   json.dumps({app: adapter.path for app, adapter in self.adapters.items()})]
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23)
        self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))
        with ConsumerJournal(self.path, self.source) as journal:
            state = journal.read()
            assert state is not None
            self.assertEqual(state['phase'], 'revoked')
        self.assertEqual(self.watchdog().returncode, 0)

    def test_watchdog_restart_after_failed_resume_retains_revocation(self):
        self.begin()
        self.expire()
        adapter = next(iter(self.adapters.values()))
        original = adapter.observe()['identity']
        with database(adapter.path) as connection:
            connection.execute('UPDATE state SET identity=?', ('changed',))
        result = self.watchdog()
        self.assertNotEqual(result.returncode, 0)
        with ConsumerJournal(self.path, self.source) as journal:
            state = journal.read()
            assert state is not None
            self.assertEqual(state['phase'], 'revoked')
        self.assertTrue(all(not item.observe()['admission'] for item in self.adapters.values()))
        with database(adapter.path) as connection:
            connection.execute('UPDATE state SET identity=?', (original,))
        result = self.watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))

    def test_later_foreign_hold_prevents_any_resume(self):
        self.begin()
        self.expire()
        adapter = list(self.adapters.values())[-1]
        original = adapter.observe()
        for field in ('epoch', 'generation'):
            with self.subTest(field=field):
                with database(adapter.path) as connection:
                    connection.execute('UPDATE state SET ' + field + '=?', ('f' * 32,))
                result = self.watchdog()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(all(not item.observe()['admission'] for item in self.adapters.values()))
                with ConsumerJournal(self.path, self.source) as journal:
                    state = journal.read()
                    assert state is not None
                    self.assertEqual(state['phase'], 'revoked')
                with database(adapter.path) as connection:
                    connection.execute('UPDATE state SET ' + field + '=?', (original[field],))
        self.assertEqual(self.watchdog().returncode, 0)
        self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))

    def test_cleared_token_with_changed_admission_prevents_any_resume(self):
        self.begin()
        self.expire()
        adapter = list(self.adapters.values())[-1]
        original = adapter.observe()
        with database(adapter.path) as connection:
            connection.execute('UPDATE state SET epoch=NULL, generation=NULL')
        result = self.watchdog()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(all(not item.observe()['admission'] for item in self.adapters.values()))
        with database(adapter.path) as connection:
            connection.execute('UPDATE state SET epoch=?, generation=?',
                               (original['epoch'], original['generation']))
        self.assertEqual(self.watchdog().returncode, 0)

    def test_lost_resume_ack_is_idempotent_after_process_exit(self):
        self.begin()
        self.expire()
        command = [sys.executable, __file__, 'crash-resume', str(self.path), self.source,
                   json.dumps({app: adapter.path for app, adapter in self.adapters.items()})]
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23)
        with ConsumerJournal(self.path, self.source) as journal:
            state = journal.read()
            assert state is not None
            self.assertEqual(state['phase'], 'revoked')
        self.assertEqual(self.watchdog().returncode, 0)
        self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))

    def test_new_epoch_rejects_late_publication(self):
        epoch, ledger, receipt = self.begin()
        self.expire()
        self.assertEqual(self.watchdog().returncode, 0)
        with ConsumerJournal(self.path, self.source) as journal:
            newer, _ = journal.begin(self.adapters, 60)
            self.assertNotEqual(newer, epoch)
            with self.assertRaises(InvalidEvidence):
                journal.publish(epoch, ledger, receipt, self.adapters)

    def test_second_generation_unheld_and_partial_hold_recovery(self):
        self.begin()
        self.expire()
        self.assertEqual(self.watchdog().returncode, 0)
        for partial in (False, True):
            with self.subTest(partial=partial):
                with ConsumerJournal(self.path, self.source) as journal:
                    epoch, generation = journal.begin(self.adapters, 60)
                if partial:
                    adapter = next(iter(self.adapters.values()))
                    adapter.hold(adapter.observe()['identity'], epoch, generation)
                self.expire()
                result = self.watchdog()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(all(item.observe()['admission'] for item in self.adapters.values()))

    def test_prior_closed_admission_is_not_opened(self):
        adapter = next(iter(self.adapters.values()))
        with database(adapter.path) as connection:
            connection.execute('UPDATE state SET admission=0')
        self.begin()
        self.expire()
        self.assertEqual(self.watchdog().returncode, 0)
        self.assertFalse(adapter.observe()['admission'])

    def test_concurrent_owner_and_production_source_refused(self):
        with ConsumerJournal(self.path, self.source):
            with self.assertRaises(RuntimeError):
                with ConsumerJournal(self.path, self.source):
                    pass
        with self.assertRaises(ValueError):
            ConsumerJournal(self.path, 'database/elasticsearch')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'writer':
        writer(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] in ('watchdog', 'crash-resume', 'crash-hold', 'crash-released'):
        adapters = {app: SQLiteConsumer(path) for app, path in json.loads(sys.argv[4]).items()}
        if sys.argv[1] in ('crash-resume', 'crash-hold'):
            import os
            adapter = next(iter(adapters.values()))
            resume, hold = adapter.resume, adapter.hold
            def crash_resume(identity, epoch, generation, prior):
                resume(identity, epoch, generation, prior)
                os._exit(23)
            def crash_hold(identity, epoch, generation):
                hold(identity, epoch, generation)
                os._exit(23)
            if sys.argv[1] == 'crash-resume':
                adapter.resume = crash_resume
            else:
                adapter.hold = crash_hold
        with ConsumerJournal(sys.argv[2], sys.argv[3]) as journal:
            if sys.argv[1] == 'crash-released':
                import os
                save = journal.save
                def crash_save(state):
                    if state['phase'] == 'released':
                        os._exit(23)
                    save(state)
                journal.save = crash_save
            if sys.argv[1] == 'crash-hold':
                state = journal.read()
                assert state is not None
                journal.hold(state['epoch'], adapters)
            else:
                print(json.dumps({'recovered': journal.watchdog_recover(adapters)}))
    else:
        unittest.main()
