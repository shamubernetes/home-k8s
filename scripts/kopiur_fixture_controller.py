"""Short snapshot-CAS controller for the isolated ARC supervisor only.

Every native operation has durable intent before dispatch. Native execution and
waiting never hold the controller lock; independent revocation uses the same
supervisor's permanent state lock. This module never admits production consumers
or replaces ConsumerJournal's legacy lock. A committed fixture plan is not an
application recovery acceptance receipt.
"""
import copy
import json
import math
import time
import uuid

from kopiur_fixture_supervisor import BoundaryError, _persist


class FixtureController:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.path = supervisor.directory / 'controller.json'

    def _read(self, native):
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text())
        if (state.get('schema') != 'k8s92-arc-controller/v1'
                or state.get('generation') != native['generation']
                or state.get('boundary') != native['boundary']
                or type(state.get('revision')) is not int or state['revision'] < 1
                or state.get('phase') not in ('open', 'committed', 'ceased')):
            raise BoundaryError('controller generation identity changed')
        return state

    def snapshot(self):
        with self.supervisor._locked():
            native, _ = self.supervisor._read()
            return copy.deepcopy(self._read(native))

    def _current(self, expected):
        native, boundary = self.supervisor._read()
        current = self._read(native)
        if current != expected:
            raise BoundaryError('controller snapshot changed')
        if (current is None or current['phase'] != 'open' or native['revoked']
                or time.monotonic() >= current['deadline_monotonic']):
            raise BoundaryError('controller expired, terminal or revoked')
        return current, native, boundary

    def _save(self, state):
        state = copy.deepcopy(state)
        state['revision'] += 1
        _persist(self.path, state)
        return copy.deepcopy(state)

    def begin(self, consumers, timeout):
        if (not isinstance(consumers, dict) or not consumers
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                       for k, v in consumers.items())
                or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise BoundaryError('explicit fixture cohort and finite timeout required')
        with self.supervisor._locked():
            native, _ = self.supervisor._read()
            if self._read(native) is not None or native['revoked'] or native['commands']:
                raise BoundaryError('fresh unused supervisor generation required')
            state = {'schema': 'k8s92-arc-controller/v1', 'revision': 0,
                     'generation': native['generation'], 'boundary': native['boundary'],
                     'phase': 'open', 'consumers': copy.deepcopy(consumers), 'plans': [],
                     'deadline_monotonic': time.monotonic() + timeout}
            return self._save(state)

    def stage(self, expected, consumer, argv):
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv):
            raise BoundaryError('explicit native command required')
        with self.supervisor._locked():
            state, _, _ = self._current(expected)
            if consumer not in state['consumers']:
                raise BoundaryError('plan consumer outside authoritative cohort')
            state['plans'].append({'id': uuid.uuid4().hex, 'consumer': consumer,
                                   'argv': copy.deepcopy(argv), 'stage': 'intent'})
            return self._save(state)

    @staticmethod
    def _plan(state, plan_id, stage):
        plans = [p for p in state['plans'] if p['id'] == plan_id]
        if len(plans) != 1 or plans[0]['stage'] != stage:
            raise BoundaryError('exact native plan stage required')
        return plans[0]

    def dispatch(self, expected, plan_id):
        with self.supervisor._locked():
            state, _, _ = self._current(expected)
            plan = self._plan(state, plan_id, 'intent')
            plan['stage'] = 'dispatching'
            staged = self._save(state)
            argv = copy.deepcopy(plan['argv'])
        try:
            child, registration = self.supervisor.dispatch(argv)
            with self.supervisor._locked():
                state, native, _ = self._current(staged)
                plan = self._plan(state, plan_id, 'dispatching')
                if registration not in native['commands']:
                    raise BoundaryError('supervised registration missing')
                plan['stage'] = 'running'
                plan['registration'] = registration
                return child, self._save(state)
        except BaseException:
            # Includes process-loss aftermath and failed CAS acknowledgement.
            # Unknown outcomes remain held; cessation never means app admission.
            self.supervisor.cease()
            raise

    def complete(self, expected, plan_id, timeout=5):
        with self.supervisor._locked():
            state, _, _ = self._current(expected)
            plan = copy.deepcopy(self._plan(state, plan_id, 'running'))
        proof = self.supervisor.complete(plan['registration']['operation'], timeout)
        with self.supervisor._locked():
            state, native, boundary = self._current(expected)
            plan = self._plan(state, plan_id, 'running')
            operations = [c['operation'] for c in native['commands']]
            if (proof['generation'] != native['generation']
                    or proof['boundary'] != native['boundary']
                    or proof['registered_operations'] != operations
                    or 'populated 0' not in (boundary / 'cgroup.events').read_text().splitlines()):
                raise BoundaryError('native completion changed before controller CAS')
            plan['stage'] = 'completed'
            plan['completion'] = proof
            return self._save(state)

    def commit(self, expected):
        with self.supervisor._locked():
            state, native, boundary = self._current(expected)
            plans = state['plans']
            commands = native['commands']
            if (not plans or any(p['stage'] != 'completed' for p in plans)
                    or {p['consumer'] for p in plans} != set(state['consumers'])
                    or {p['registration']['operation'] for p in plans} !=
                    {c['operation'] for c in commands}
                    or any(c['stage'] != 'completed' for c in commands)
                    or 'populated 0' not in (boundary / 'cgroup.events').read_text().splitlines()):
                raise BoundaryError('entire authoritative fixture cohort must complete')
            state['phase'] = 'committed'
            # Seal native dispatch before terminal publication. A lost journal
            # acknowledgement is recoverable, but cannot reopen dispatch.
            native['sealed'] = True
            native['revision'] += 1
            _persist(self.supervisor.path, native)
            state['terminal'] = {'generation': native['generation'],
                                 'boundary': native['boundary'], 'populated': 0,
                                 'operations': [c['operation'] for c in commands],
                                 'production_recovery_accepted': False}
            return self._save(state)

    def verify_terminal(self, expected):
        with self.supervisor._locked():
            native, boundary = self.supervisor._read()
            state = self._read(native)
            if (state != expected or state is None or state['phase'] != 'committed'
                    or native['revoked'] or native.get('sealed') is not True
                    or state['terminal']['operations'] !=
                    [c['operation'] for c in native['commands']]
                    or any(c['stage'] != 'completed' for c in native['commands'])
                    or 'populated 0' not in (boundary / 'cgroup.events').read_text().splitlines()):
                raise BoundaryError('no current committed fixture terminal')
            _persist(self.supervisor.path, native)
            _persist(self.path, state)
            return copy.deepcopy(state['terminal'])

    def recover(self):
        proof = self.supervisor.cease()
        with self.supervisor._locked():
            native, boundary = self.supervisor._read()
            state = self._read(native)
            if (not native['revoked'] or proof['generation'] != native['generation']
                    or proof['boundary'] != native['boundary']
                    or 'populated 0' not in (boundary / 'cgroup.events').read_text().splitlines()):
                raise BoundaryError('complete-boundary cessation not proved')
            if state is None:
                return proof
            state['phase'] = 'ceased'
            state['cessation'] = proof
            return self._save(state)
