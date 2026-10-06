"""Native SQLite admission protocol for disposable ARC cohorts only.

Every operation runs in a registered cgroup command, never inside a controller
state lock. The database is the actual writer admission gate. Commit stays held;
release and rollback restore each member's observed prior admission. Recovery
must cease every original/release command boundary before installing its fence.
This module does not qualify production recovery or replace ConsumerJournal.
"""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from kopiur_fixture_supervisor import BoundaryError, FixtureSupervisor, _persist
from kopiur_fixture_controller import FixtureController


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BoundaryError('existing regular fixture database required')
    st = path.stat()
    return {'device': st.st_dev, 'inode': st.st_ino}


def create_database(path, consumers):
    """Root fixture setup only, not an application database initializer."""
    path = Path(path)
    if (sys.platform != 'linux' or os.geteuid() != 0
            or os.environ.get('K8S92_CGROUP_FIXTURE') != 'isolated-arc-only'
            or not _uuid(path.stem) or path.suffix != '.sqlite'
            or not consumers or any(not isinstance(k, str) or not k
                or not isinstance(v, dict) or not isinstance(v.get('identity'), str)
                or not v['identity'] or type(v.get('admission')) is not bool
                for k, v in consumers.items())):
        raise BoundaryError('explicit isolated ARC fixture database required')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    os.close(fd)
    os.chmod(path, 0o666)
    connection = sqlite3.connect(path, timeout=0)
    try:
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        connection.execute('CREATE TABLE records (consumer TEXT NOT NULL, value TEXT NOT NULL)')
        initial = {'schema': 'k8s92-native-admission/v1', 'fixture': path.stem,
                   'revision': 0, 'phase': 'idle', 'plan': None,
                   'consumers': copy.deepcopy(consumers)}
        connection.execute('INSERT INTO state VALUES (1, ?)', (json.dumps(initial, sort_keys=True),))
        connection.commit()
    finally:
        connection.close()
    return _identity(path)


def operation(path, identity, plan, action, consumer=None, value=None):
    """One non-waiting SQLite transaction, executed by a supervised worker."""
    path = Path(path)
    if (sys.platform != 'linux' or not _uuid(path.stem) or path.suffix != '.sqlite'
            or _identity(path) != identity):
        raise BoundaryError('native fixture database identity changed')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=0)
    try:
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        if _identity(path) != identity:
            raise BoundaryError('native fixture database replaced during open')
        state = json.loads(connection.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        if state['schema'] != 'k8s92-native-admission/v1' or state['fixture'] != path.stem:
            raise BoundaryError('native cohort identity changed')
        if action == 'observe':
            connection.commit()
            return state
        if action == 'write':
            if consumer not in state['consumers'] or state['consumers'][consumer]['admission'] is not True:
                raise BoundaryError('native writer admission closed')
            connection.execute('INSERT INTO records VALUES (?, ?)', (consumer, value))
            connection.commit()
            return state
        if (not isinstance(plan, dict) or not _uuid(plan.get('generation'))
                or not isinstance(plan.get('consumers'), dict)
                or set(plan['consumers']) != set(state['consumers'])
                or any(not isinstance(v, dict) or not isinstance(v.get('identity'), str)
                       or not v['identity'] or type(v.get('prior_admission')) is not bool
                       for v in plan['consumers'].values())):
            raise BoundaryError('complete native plan required')
        if action in ('hold', 'fence') and state['phase'] == 'idle':
            for name, expected in plan['consumers'].items():
                actual = state['consumers'][name]
                if (actual['identity'] != expected['identity']
                        or actual['admission'] is not expected['prior_admission']):
                    raise BoundaryError('observed prior admission changed')
            state['plan'] = copy.deepcopy(plan)
            state['phase'] = 'held' if action == 'hold' else 'rolled_back'
            for actual in state['consumers'].values():
                actual.update(admission=False if action == 'hold' else actual['admission'],
                              released=False, restored=action == 'fence')
        else:
            if state['plan'] != plan:
                raise BoundaryError('native ownership or immutable prior plan changed')
            # Whole-cohort validation precedes the first member mutation, even
            # on rollback retries after a lost release acknowledgement.
            for name, expected in plan['consumers'].items():
                actual = state['consumers'][name]
                admission = expected['prior_admission'] if actual['released'] or actual['restored'] else False
                if actual['identity'] != expected['identity'] or actual['admission'] is not admission:
                    raise BoundaryError('native cohort identity or admission changed')
            if action == 'hold':
                if state['phase'] != 'held':
                    raise BoundaryError('late hold denied')
            elif action == 'prepare':
                if state['phase'] not in ('held', 'prepared'):
                    raise BoundaryError('preparation requires held cohort')
                state['phase'] = 'prepared'
            elif action == 'commit':
                if state['phase'] not in ('prepared', 'committed'):
                    raise BoundaryError('commit requires prepared cohort')
                state['phase'] = 'committed'
            elif action == 'release':
                if state['phase'] not in ('committed', 'released') or consumer not in state['consumers']:
                    raise BoundaryError('release denied outside current committed cohort')
                actual = state['consumers'][consumer]
                actual['admission'] = plan['consumers'][consumer]['prior_admission']
                actual['released'] = True
                if all(v['released'] for v in state['consumers'].values()):
                    state['phase'] = 'released'
            elif action == 'fence':
                if state['phase'] not in ('held', 'prepared', 'committed', 'released', 'recovering', 'rolled_back'):
                    raise BoundaryError('native recovery fence denied')
                if state['phase'] != 'rolled_back':
                    state['phase'] = 'recovering'
            elif action == 'rollback':
                if state['phase'] not in ('recovering', 'rolled_back') or consumer not in state['consumers']:
                    raise BoundaryError('rollback requires whole-cohort recovery fence')
                actual = state['consumers'][consumer]
                actual['admission'] = plan['consumers'][consumer]['prior_admission']
                actual['restored'] = True
                if all(v['restored'] for v in state['consumers'].values()):
                    state['phase'] = 'rolled_back'
            else:
                raise BoundaryError('unknown native admission operation')
        state['revision'] += 1
        connection.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state, sort_keys=True),))
        connection.commit()
        return state
    finally:
        connection.close()


class NativeAdmission:
    """Root coordinator for one native fixture plan and owned command boundaries.

    Recovery permanently revokes and ceases *all* recorded command generations
    before a fresh recovery boundary can mutate admission. Restart never infers
    command success. Recovery progress remains independently retryable.
    """
    def __init__(self, supervisor, database, identity, results, capture_consumer=None):
        self.supervisor = supervisor
        self.path = supervisor.directory / 'admission.json'
        self.database = Path(database)
        self.identity = copy.deepcopy(identity)
        self.results = Path(results)
        self.capture_consumer = capture_consumer

    def _read(self):
        state = json.loads(self.path.read_text())
        native, _ = self.supervisor._read()
        if (state['generation'] != native['generation'] or state['boundary'] != native['boundary']
                or state['database'] != str(self.database) or state['database_identity'] != self.identity
                or state['results'] != str(self.results) or state['capture_consumer'] != self.capture_consumer):
            raise BoundaryError('native admission coordinator identity changed')
        return state

    def _save(self, state):
        state = copy.deepcopy(state)
        state['revision'] += 1
        _persist(self.path, state)
        return state

    def snapshot(self):
        with self.supervisor._locked():
            return copy.deepcopy(self._read())

    def _worker(self, command_supervisor, plan, action, consumer=None, value=None):
        output = self.results / (uuid.uuid4().hex + '.json')
        payload = {'path': str(self.database), 'identity': self.identity, 'plan': plan,
                   'action': action, 'consumer': consumer, 'value': value}
        argv = [sys.executable, str(Path(__file__).resolve()), json.dumps(payload, sort_keys=True), str(output)]
        publisher = FixtureController(self.supervisor) if self.capture_consumer else None
        running = plan_id = committed = None
        if publisher and command_supervisor is self.supervisor:
            state = publisher.stage(publisher.snapshot(), self.capture_consumer, argv)
            plan_id = state['plans'][-1]['id']
            child, running = publisher.dispatch(state, plan_id)
            registration = running['plans'][-1]['registration']
        elif publisher and action == 'release':
            committed = publisher.snapshot()
            publisher.verify_terminal(committed)
            # Only the short parent-before-child dispatch gate holds the lock.
            # Parent revocation kills the nested release worker too; neither
            # SQL nor completion waits prevent independent parent revocation.
            with self.supervisor._locked():
                publisher._current(committed, ('committed',))
                admission = self._read()
                if admission['phase'] != 'open' or admission['intent'] != {'action': 'release', 'consumer': consumer}:
                    raise BoundaryError('native release intent retired')
                child, registration = command_supervisor.dispatch(argv)
        else:
            child, registration = command_supervisor.dispatch(argv)
        try:
            if publisher and running is not None and plan_id is not None:
                publisher.complete(running, plan_id)
            else:
                command_supervisor.complete(registration['operation'])
            if publisher and committed is not None:
                publisher.verify_terminal(committed)
                with self.supervisor._locked():
                    publisher._current(committed, ('committed',))
            observed = json.loads(output.read_text())
            return observed
        except BaseException:
            command_supervisor.cease()
            raise
        finally:
            child.wait(timeout=5)
            output.unlink(missing_ok=True)

    def begin(self):
        # The observed state is obtained by an owned successful native command,
        # not a caller-supplied prior-admission flag.
        if self.capture_consumer:
            capture = FixtureController(self.supervisor).snapshot()
            if not capture or capture['consumers'].get(self.capture_consumer) != self.database.stem:
                raise BoundaryError('capture consumer must bind the exact native database UUID')
        observed = self._worker(self.supervisor, None, 'observe')
        with self.supervisor._locked():
            native, _ = self.supervisor._read()
            if self.path.exists() or native['revoked'] or native.get('sealed') or observed['phase'] != 'idle':
                raise BoundaryError('fresh native admission cohort required')
            plan = {'generation': native['generation'], 'consumers': {
                name: {'identity': v['identity'], 'prior_admission': v['admission']}
                for name, v in observed['consumers'].items()}}
            return self._save({'revision': 0, 'generation': native['generation'], 'boundary': native['boundary'],
                               'database': str(self.database), 'database_identity': self.identity,
                               'results': str(self.results), 'plan': plan, 'phase': 'open',
                               'capture_consumer': self.capture_consumer,
                               'boundaries': [], 'release_boundary': None, 'intent': None})

    def _release_supervisor(self, expected):
        publisher = FixtureController(self.supervisor)
        committed = publisher.snapshot()
        publisher.verify_terminal(committed)
        with self.supervisor._locked():
            publisher._current(committed, ('committed',))
            state = self._read()
            if state != expected or state['phase'] != 'open' or state['intent'] is not None:
                raise BoundaryError('native release snapshot retired')
            if state['release_boundary'] is None:
                _, parent = self.supervisor._read()
                directory = self.supervisor.directory / ('release-' + str(uuid.uuid4()))
                directory.mkdir(mode=0o700)
                release = FixtureSupervisor(directory, parent)
                release.begin()
                native, _ = release._read()
                state['release_boundary'] = {'directory': str(directory), 'root': str(parent),
                                             'generation': native['generation'], 'boundary': native['boundary']}
                state = self._save(state)
            release = self._release_reference(state['release_boundary'])
            return state, release

    def _release_reference(self, reference):
        _, parent = self.supervisor._read()
        directory = Path(reference['directory'])
        if (reference['root'] != str(parent) or directory.parent != self.supervisor.directory
                or not directory.name.startswith('release-') or directory.is_symlink()
                or not _uuid(directory.name.removeprefix('release-'))):
            raise BoundaryError('native release reference outside original owned boundary')
        release = FixtureSupervisor(directory, parent)
        native, _ = release._read()
        if native['generation'] != reference['generation'] or native['boundary'] != reference['boundary']:
            raise BoundaryError('native release boundary identity changed')
        return release

    def apply(self, expected, action, consumer=None):
        if action not in ('hold', 'prepare', 'commit', 'release'):
            raise BoundaryError('explicit forward admission operation required')
        command_supervisor = self.supervisor
        if action == 'release' and self.capture_consumer:
            expected, command_supervisor = self._release_supervisor(expected)
        with self.supervisor._locked():
            state = self._read()
            native, _ = self.supervisor._read()
            if (state != expected or state['phase'] != 'open' or state['intent'] is not None
                    or native['revoked']):
                raise BoundaryError('stale or revoked admission coordinator')
            state['intent'] = {'action': action, 'consumer': consumer}
            staged = self._save(state)
        # Never retain the coordinator lock across native SQL or wait.
        observed = self._worker(command_supervisor, staged['plan'], action, consumer)
        with self.supervisor._locked():
            state = self._read()
            native, _ = self.supervisor._read()
            if state != staged or native['revoked']:
                raise BoundaryError('native admission acknowledgement became stale')
            state['observed'] = observed
            state['intent'] = None
            if observed['phase'] == 'released':
                state['phase'] = 'released'
            return self._save(state)

    def recover(self):
        # Mark recovery intent before cessation. Forward acknowledgements and
        # queued forward operations cannot win after this snapshot transition.
        with self.supervisor._locked():
            state = self._read()
            state['phase'] = 'recovering'
            staged = self._save(state)
            boundaries = copy.deepcopy(state['boundaries'])
        proofs = [self.supervisor.cease()]
        if staged['release_boundary'] is not None:
            release = self._release_reference(staged['release_boundary'])
            proofs.append(release.cease())
        if self.capture_consumer:
            FixtureController(self.supervisor).recover()
        for directory in boundaries:
            prior = FixtureSupervisor(directory, self.supervisor.root)
            proofs.append(prior.cease())
        # An independently restarted recovery attempt ceases its predecessor
        # too. Register its private boundary before its first native dispatch.
        with self.supervisor._locked():
            state = self._read()
            if state != staged:
                raise BoundaryError('another recovery owner changed admission snapshot')
            directory = self.supervisor.directory / ('recovery-' + str(uuid.uuid4()))
            directory.mkdir(mode=0o700)
            recovery = FixtureSupervisor(directory, self.supervisor.root)
            recovery.begin()
            state['boundaries'].append(str(directory))
            state['cessation'] = proofs
            registered = self._save(state)
        try:
            observed = self._worker(recovery, registered['plan'], 'fence')
            for name in registered['plan']['consumers']:
                observed = self._worker(recovery, registered['plan'], 'rollback', name)
            cessation = recovery.cease()
            with self.supervisor._locked():
                state = self._read()
                if state != registered or observed['phase'] != 'rolled_back':
                    raise BoundaryError('native recovery terminal changed')
                state['phase'] = 'rolled_back'
                state['observed'] = observed
                state['recovery_cessation'] = cessation
                state['consumer_admission_restored'] = True
                state['production_recovery_accepted'] = False
                return self._save(state)
        except BaseException:
            recovery.cease()
            raise


if __name__ == '__main__':
    payload = json.loads(sys.argv[1])
    observed = operation(**payload)
    Path(sys.argv[2]).write_text(json.dumps(observed, sort_keys=True))
