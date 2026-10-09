"""Native transactional fencing for disposable SQLite consumer fixtures only.

This adapter is not wired into production or the legacy consumer journal.
The complete plan must be persisted by its controller before dispatch.
"""
from contextlib import closing
import json
import sqlite3

from kopiur_fixture_fence import FixtureFence
from kopiur_shared import InvalidEvidence


class SQLiteFencing:
    def __init__(self, path, source):
        FixtureFence(path, source)  # Reuse the exact UUID-owned source restriction.
        self.path = str(path)

    @staticmethod
    def plan_identity(plan):
        required = {'identity', 'epoch', 'generation', 'expected_revision',
                    'expected_operation', 'expected_stage',
                    'hold_revision', 'terminal_revision', 'hold_operation',
                    'terminal_operation', 'prior_admission'}
        if (not isinstance(plan, dict) or set(plan) != required or type(plan['prior_admission']) is not bool
                or any(type(plan[key]) is not int or plan[key] < 0
                       for key in ('expected_revision', 'hold_revision', 'terminal_revision'))
                or plan['hold_revision'] != plan['expected_revision'] + 1
                or plan['terminal_revision'] != plan['hold_revision'] + 1
                or any(not isinstance(plan[key], str) or not plan[key]
                       for key in ('identity', 'epoch', 'generation',
                                   'hold_operation', 'terminal_operation'))
                or plan['hold_operation'] == plan['terminal_operation']
                or plan['expected_stage'] not in ('idle', 'released')
                or (plan['expected_stage'] == 'idle' and plan['expected_operation'] is not None)
                or (plan['expected_stage'] == 'released' and
                    (not isinstance(plan['expected_operation'], str) or not plan['expected_operation']))):
            raise InvalidEvidence('invalid persisted native fencing plan')
        return json.dumps(plan, sort_keys=True, separators=(',', ':'))

    def apply(self, plan, command):
        if command not in ('hold', 'fence', 'resume'):
            raise ValueError('unknown native fencing command')
        plan_identity = self.plan_identity(plan)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                rows = connection.execute(
                    'SELECT identity, admission, epoch, generation, revision, operation, stage, '
                    'plan_identity, prepared '
                    'FROM fencing').fetchall()
                if len(rows) != 1:
                    raise InvalidEvidence('native consumer boundary is not singular')
                (identity, admission, epoch, generation, revision, operation, stage,
                 persisted_plan, prepared) = rows[0]
                if identity != plan['identity'] or admission not in (0, 1):
                    raise InvalidEvidence('native identity or admission changed')
                expected = (revision == plan['expected_revision']
                            and stage == plan['expected_stage']
                            and operation == plan['expected_operation']
                            and epoch is None and generation is None
                            and ((stage == 'idle' and persisted_plan is None and prepared == 0)
                                 or (stage == 'released' and prepared == 1))
                            and admission == int(plan['prior_admission']))
                held = (revision == plan['hold_revision'] and stage == 'held'
                        and (epoch, generation, operation) ==
                        (plan['epoch'], plan['generation'], plan['hold_operation'])
                        and admission == 0 and persisted_plan == plan_identity and prepared == 0)
                terminal = (revision == plan['terminal_revision']
                            and operation == plan['terminal_operation']
                            and persisted_plan == plan_identity
                            and ((prepared == 0 and (epoch, generation) ==
                                  (plan['epoch'], plan['generation']))
                                 or (prepared == 1 and stage == 'released'
                                     and epoch is None and generation is None)))
                fenced = terminal and stage == 'fenced' and admission == 0
                released = (terminal and stage == 'released'
                            and admission == int(plan['prior_admission']))
                if command == 'hold':
                    if held:
                        return
                    if not expected:
                        raise InvalidEvidence('stale or foreign native hold')
                    values = (0, plan['hold_revision'], plan['hold_operation'], 'held')
                elif command == 'fence':
                    if fenced or released:
                        return  # Lost acknowledgement must never reclose release.
                    if not (expected or held):
                        raise InvalidEvidence('stale or foreign terminal fence')
                    values = (0, plan['terminal_revision'], plan['terminal_operation'], 'fenced')
                else:
                    if released:
                        return
                    if not fenced:
                        raise InvalidEvidence('resume requires exact terminal barrier')
                    values = (int(plan['prior_admission']), plan['terminal_revision'],
                              plan['terminal_operation'], 'released')
                connection.execute(
                    'UPDATE fencing SET admission=?, revision=?, operation=?, stage=?, '
                    'epoch=?, generation=?, plan_identity=?, prepared=0',
                    values + (plan['epoch'], plan['generation'], plan_identity))

    def prepare_next(self, plan):
        """Clear completed tokens only under exact terminal ownership.

        Native revision and operation tombstones survive. The journal must also
        prove complete cohort release and cessation before beginning anew.
        """
        plan_identity = self.plan_identity(plan)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if connection.execute('SELECT count(*) FROM fencing').fetchone()[0] != 1:
                    raise InvalidEvidence('native consumer boundary is not singular')
                cursor = connection.execute(
                    'UPDATE fencing SET epoch=NULL, generation=NULL, prepared=1 '
                    'WHERE identity=? AND revision=? AND operation=? AND stage=? '
                    'AND admission=? AND plan_identity=? '
                    'AND ((prepared=0 AND epoch=? AND generation=?) '
                    'OR (prepared=1 AND epoch IS NULL AND generation IS NULL))',
                    (plan['identity'], plan['terminal_revision'], plan['terminal_operation'],
                     'released', int(plan['prior_admission']), plan_identity,
                     plan['epoch'], plan['generation']))
                if cursor.rowcount != 1:
                    raise InvalidEvidence('native release not acknowledged for next generation')
