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
        if (not isinstance(state, dict) or state.get('source') != self.source
                or state.get('schema') != 'k8s92-isolated-consumer-journal/v1'
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

    def save(self, state):
        # Re-read while holding the same stable lock inode before atomic replace.
        self.read()
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + '.')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, sort_keys=True, allow_nan=False)
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

    def begin(self, adapters, timeout_seconds):
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError('positive finite fixture timeout required')
        previous = self.read()
        if previous is not None and previous['phase'] != 'released':
            raise InvalidEvidence('recover the previous generation first')
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
        state = {'schema': 'k8s92-isolated-consumer-journal/v1', 'source': self.source,
                 'epoch': uuid.uuid4().hex, 'generation': uuid.uuid4().hex,
                 'phase': 'intent', 'revoked': False,
                 'deadline': time.time() + timeout_seconds, 'consumers': consumers}
        self.save(state)
        return state['epoch'], state['generation']

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
        if state['phase'] != 'intent':
            raise InvalidEvidence('hold requires persisted intent')
        self.observe(state, adapters, held=False)
        for app, adapter in adapters.items():
            adapter.hold(state['consumers'][app]['identity'], epoch, state['generation'])
        self.observe(state, adapters, held=True)
        self.current(epoch)
        state['phase'] = 'held'
        self.save(state)

    def publish(self, epoch, ledger, receipt, adapters):
        state = self.current(epoch)
        if state['phase'] != 'held' or receipt.get('generation') != state['generation']:
            raise InvalidEvidence('publication requires the held journal generation')
        result = reconcile(ledger, receipt)
        if {item['application'] for item in result['consumers']} != set(state['consumers']):
            raise InvalidEvidence('receipt cohort differs from journal')
        self.observe(state, adapters, held=True)
        self.current(epoch)
        state['receipt'] = copy.deepcopy(receipt)
        state['phase'] = 'closed'
        self.save(state)
        return result

    def admit(self, epoch, ledger, receipt, adapters):
        state = self.current(epoch)
        if state['phase'] != 'closed' or state.get('receipt') != receipt:
            raise InvalidEvidence('receipt differs from the authoritative closed journal')
        self.observe(state, adapters, held=True)
        result = reconcile(ledger, receipt)
        self.current(epoch)
        return result

    def watchdog_recover(self, adapters, force=False):
        state = self.read()
        if state is None or state['phase'] == 'released':
            return False
        if not force and not state['revoked'] and time.time() < state['deadline']:
            return False
        # Persist revocation before any external resume. A failed resume remains
        # retryable by a fresh watchdog process, and late publication is denied.
        state['revoked'] = True
        state['phase'] = 'revoked'
        self.save(state)
        self.observe(state, adapters, held=False)
        for app, adapter in adapters.items():
            expected = state['consumers'][app]
            adapter.resume(expected['identity'], state['epoch'],
                           state['generation'], expected['prior_admission'])
            actual = adapter.observe()
            if (actual.get('identity') != expected['identity']
                    or actual.get('admission') is not expected['prior_admission']):
                raise InvalidEvidence('native consumer resume was not acknowledged')
        state['phase'] = 'released'
        self.save(state)
        return True
