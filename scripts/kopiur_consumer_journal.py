"""Locked consumer generation protocol for isolated UUID-owned fixtures only.

Adapters must observe and restore native admission, not caller receipt flags.
No production boundary or application release is admitted by this module.
"""
import copy
import json
import math
import os
import re
import tempfile
import time
import uuid


from kopiur_consumer_generation import reconcile
from kopiur_fixture_fence import FixtureFence
from kopiur_shared import InvalidEvidence


class ConsumerJournal:
    def __init__(self, path, source):
        self.owner = FixtureFence(path, source)
        self.path = self.owner.path
        self.source = source

    def __enter__(self):
        self.owner.__enter__()
        return self

    def __exit__(self, *args):
        self.owner.__exit__(*args)

    def read(self):
        if self.owner.lock is None:
            raise RuntimeError('consumer journal requires exclusive ownership')
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text())
        return self.validate(state)

    def validate(self, state):
        if (not isinstance(state, dict) or state.get('source') != self.source
                or state.get('schema') != 'k8s92-isolated-consumer-journal/v2'
                or type(state.get('revision')) is not int or state['revision'] < 1
                or not re.fullmatch(r'[0-9a-f]{32}', state.get('operation', ''))
                or state.get('phase') not in ('intent', 'held', 'closed', 'revoked', 'released')
                or not re.fullmatch(r'[0-9a-f]{32}', state.get('epoch', ''))
                or not re.fullmatch(r'[0-9a-f]{32}', state.get('generation', ''))
                or type(state.get('revoked')) is not bool
                or type(state.get('deadline')) not in (int, float)
                or not math.isfinite(state['deadline'])
                or not isinstance(state.get('consumers'), dict) or not state['consumers']):
            raise InvalidEvidence('invalid isolated consumer journal')
        for app, consumer in state['consumers'].items():
            if (not isinstance(app, str) or not app or not isinstance(consumer, dict)
                    or not isinstance(consumer.get('identity'), str) or not consumer['identity']
                    or type(consumer.get('prior_admission')) is not bool):
                raise InvalidEvidence('invalid journal consumer boundary')
        return state

    def save(self, state, expected):
        # Full snapshot CAS. Keep the legacy lock until supervised cessation and
        # fenced native dispatch are integrated, rather than shorten it here.
        current = self.read()
        if current != expected:
            raise InvalidEvidence('consumer journal snapshot changed')
        candidate = copy.deepcopy(state)
        candidate['revision'] = 1 if current is None else current['revision'] + 1
        candidate['operation'] = uuid.uuid4().hex
        self.validate(candidate)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + '.')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(candidate, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return candidate

    def begin(self, adapters, timeout_seconds):
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError('positive finite fixture timeout required')
        previous = self.read()
        if previous is not None and previous['phase'] != 'released':
            raise InvalidEvidence('recover the previous generation first')
        if previous is not None:
            self.retain(previous)
        if not adapters:
            raise InvalidEvidence('explicit isolated consumer adapters required')
        consumers = {}
        for app, adapter in adapters.items():
            observed = adapter.observe()
            if (not isinstance(observed.get('identity'), str) or not observed['identity']
                    or type(observed.get('admission')) is not bool):
                raise InvalidEvidence('native consumer identity and prior admission required')
            consumers[app] = {'identity': observed['identity'],
                              'prior_admission': observed['admission']}
        state = {'schema': 'k8s92-isolated-consumer-journal/v2', 'source': self.source,
                 'epoch': uuid.uuid4().hex, 'generation': uuid.uuid4().hex,
                 'phase': 'intent', 'revoked': False,
                 'deadline': time.time() + timeout_seconds, 'consumers': consumers}
        self.save(state, previous)
        return state['epoch'], state['generation']

    def retain(self, state):
        """Retain exact terminal bytes before replacing a fixture generation.

        A linked, fsynced temporary file makes creation exclusive and atomic.
        An interrupted acknowledgement is retryable only with identical bytes.
        This is terminal fixture history, not a production recovery receipt.
        """
        if state is None or state != self.read() or state['phase'] != 'released':
            raise InvalidEvidence('retention requires the current released snapshot')
        payload = json.dumps(state, sort_keys=True, allow_nan=False).encode()
        history = self.path.with_name(self.path.name + '.history')
        history.mkdir(mode=0o700, exist_ok=True)
        target = history / (state['epoch'] + '-' + state['generation'] + '.json')
        fd, name = tempfile.mkstemp(dir=history, prefix='.pending-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, target)
            except FileExistsError:
                pass
            if target.read_bytes() != payload:
                raise InvalidEvidence('terminal history differs from the released snapshot')
            for path in (history, history.parent):
                directory = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return target

    def current(self, epoch):
        state = self.read()
        if (state is None or state['epoch'] != epoch or state['revoked']
                or state['phase'] not in ('intent', 'held', 'closed')
                or time.time() >= state['deadline']):
            raise InvalidEvidence('expired, replaced or revoked generation')
        return state

    @staticmethod
    def observe(state, adapters, held):
        if set(adapters) != set(state['consumers']):
            raise InvalidEvidence('adapter cohort differs from authoritative journal')
        for app, adapter in adapters.items():
            expected = state['consumers'][app]
            actual = adapter.observe()
            if actual.get('identity') != expected['identity']:
                raise InvalidEvidence('consumer resource identity changed')
            if held and (actual.get('admission') is not False
                         or type(actual.get('active')) is not int or actual['active'] != 0
                         or actual.get('epoch') != state['epoch']
                         or actual.get('generation') != state['generation']):
                raise InvalidEvidence('native consumer is not drained on this generation')

    def hold(self, epoch, adapters):
        state = self.current(epoch)
        expected = copy.deepcopy(state)
        if state['phase'] != 'intent':
            raise InvalidEvidence('hold requires persisted intent')
        self.observe(state, adapters, held=False)
        for app, adapter in adapters.items():
            adapter.hold(state['consumers'][app]['identity'], epoch, state['generation'])
        self.observe(state, adapters, held=True)
        self.current(epoch)
        state['phase'] = 'held'
        self.save(state, expected)

    def publish(self, epoch, ledger, receipt, adapters):
        state = self.current(epoch)
        expected = copy.deepcopy(state)
        if state['phase'] != 'held' or receipt.get('generation') != state['generation']:
            raise InvalidEvidence('publication requires the held journal generation')
        result = reconcile(ledger, receipt)
        if {item['application'] for item in result['consumers']} != set(state['consumers']):
            raise InvalidEvidence('receipt cohort differs from journal')
        self.observe(state, adapters, held=True)
        self.current(epoch)
        state['receipt'] = copy.deepcopy(receipt)
        state['phase'] = 'closed'
        self.save(state, expected)
        return result

    def admit(self, epoch, ledger, receipt, adapters):
        state = self.current(epoch)
        if state['phase'] != 'closed' or state.get('receipt') != receipt:
            raise InvalidEvidence('receipt differs from the authoritative closed journal')
        self.observe(state, adapters, held=True)
        result = reconcile(ledger, receipt)
        self.current(epoch)
        return result

    def preflight_recovery(self, state, adapters):
        if set(adapters) != set(state['consumers']):
            raise InvalidEvidence('adapter cohort differs from authoritative journal')
        # Validate the whole cohort before reopening its first consumer. Cleared
        # tokens require original admission, covering never-applied intent and
        # acknowledged prior resume. Native mutations must still check ownership.
        for app, adapter in adapters.items():
            actual = adapter.observe()
            expected = state['consumers'][app]
            if (actual.get('identity') != expected['identity']
                    or type(actual.get('admission')) is not bool
                    or 'epoch' not in actual or 'generation' not in actual):
                raise InvalidEvidence('incomplete native recovery ownership observation')
            token = actual['epoch'], actual['generation']
            if token == (state['epoch'], state['generation']):
                if actual['admission'] is not False:
                    raise InvalidEvidence('owned hold admission changed before recovery')
            elif token != (None, None) or actual['admission'] is not expected['prior_admission']:
                raise InvalidEvidence('consumer recovery ownership changed')

    def watchdog_recover(self, adapters, force=False):
        state = self.read()
        if state is None or state['phase'] == 'released':
            return False
        if not force and not state['revoked'] and time.time() < state['deadline']:
            return False
        # Persist revocation before any external resume. A failed resume remains
        # retryable by a fresh watchdog process, and late publication is denied.
        expected = copy.deepcopy(state)
        state['revoked'] = True
        state['phase'] = 'revoked'
        state = self.save(state, expected)
        snapshot = copy.deepcopy(state)
        self.preflight_recovery(state, adapters)
        for app, adapter in adapters.items():
            expected = state['consumers'][app]
            adapter.resume(expected['identity'], state['epoch'],
                           state['generation'], expected['prior_admission'])
            actual = adapter.observe()
            if (actual.get('identity') != expected['identity']
                    or actual.get('admission') is not expected['prior_admission']):
                raise InvalidEvidence('native consumer resume was not acknowledged')
        state['phase'] = 'released'
        self.save(state, snapshot)
        return True
