"""Reject inherited hourly backup triggers without changing retention history.

Run: python3 scripts/tests/test_backup_cadence.py (requires repo tool yq).
"""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
VARIABLE = re.compile(r'^\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}$')


def resolve(value, variables):
    for _ in range(10):
        match = VARIABLE.fullmatch(value)
        if match is None:
            return value
        value = variables.get(match[1], match[2])
        if value is None:
            raise ValueError('Unresolved schedule variable: ' + match[1])
    raise ValueError('Schedule variable cycle')


def at_most_daily(cron):
    if cron in {'@daily', '@midnight', '@weekly', '@monthly', '@yearly', '@annually'}:
        return True
    fields = cron.split()
    if len(fields) != 5:
        return False
    return (fields[0].isdecimal() and 0 <= int(fields[0]) < 60
            and fields[1].isdecimal() and 0 <= int(fields[1]) < 24)


def yaml_json(expression, paths):
    result = subprocess.run(['yq', '-o=json', '-I=0', expression, *map(str, paths)],
                            check=True, capture_output=True, text=True)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


class CadenceTests(unittest.TestCase):
    def test_frequency_examples(self):
        for cron in ('0 1 * * *', '59 23 * * 0', '@daily', '@weekly'):
            with self.subTest(cron=cron):
                self.assertTrue(at_most_daily(cron))
        for cron in ('0 * * * *', '*/15 1 * * *', '0 */6 * * *',
                     '0 1,2 * * *', '@hourly', 'bad'):
            with self.subTest(cron=cron):
                self.assertFalse(at_most_daily(cron))

    def test_resolve_default_and_reference(self):
        variables = {'VOLSYNC_SCHEDULE': '${DAILY}', 'DAILY': '7 1 * * *'}
        self.assertEqual(resolve('${VOLSYNC_SCHEDULE}', variables), '7 1 * * *')
        self.assertEqual(resolve('${MISSING:-0 2 * * *}', variables), '0 2 * * *')
        with self.assertRaises(ValueError):
            resolve('${MISSING}', variables)
        with self.assertRaises(ValueError):
            resolve('${LOOP}', {'LOOP': '${LOOP}'})

    def test_tunarr_retained_backup_root_does_not_own_the_app(self):
        app = ROOT / 'kubernetes/apps/media/tunarr'
        legacy, = yaml_json('.', [app / 'ks.yaml'])
        self.assertTrue(legacy['spec']['suspend'])
        self.assertEqual(legacy['metadata']['annotations']['kustomize.toolkit.fluxcd.io/reconcile'], 'disabled')
        owner, = yaml_json('.', [app / 'backups-ks.yaml'])
        self.assertEqual(owner['spec']['path'], './kubernetes/apps/media/tunarr/backups')
        self.assertFalse(owner['spec']['prune'])
        self.assertEqual(owner['spec']['deletionPolicy'], 'Orphan')
        root, = yaml_json('.', [app / 'backups/kustomization.yaml'])
        self.assertEqual(root['resources'], ['../../../../templates/volsync/nfs-replicationsource.yaml'])
        resource, = yaml_json('.', [ROOT / 'kubernetes/templates/volsync/nfs-replicationsource.yaml'])
        self.assertEqual(resource['kind'], 'ReplicationSource')
        self.assertEqual(resource['spec']['sourcePVC'], '${APP}')
        self.assertEqual(resource['spec']['restic']['copyMethod'], '${VOLSYNC_COPYMETHOD:-Snapshot}')
        patch, = root['patches']
        self.assertEqual(patch['target']['kind'], 'ReplicationSource')
        self.assertIn('kustomize.toolkit.fluxcd.io/prune: disabled', patch['patch'])

    def test_all_declared_backup_cadences(self):
        common_path = ROOT / 'kubernetes/flux/vars/volsync-schedules.yaml'
        common, = yaml_json('.data', [common_path])
        self.assertTrue(common)
        for name, value in common.items():
            with self.subTest(variable=name):
                self.assertTrue(at_most_daily(resolve(value, common)), value)

        app_root = ROOT / 'kubernetes/apps'
        paths = list(app_root.rglob('ks.yaml')) + list(app_root.rglob('*-ks.yaml'))
        for tree in (app_root, ROOT / 'kubernetes/templates/volsync'):
            for path in tree.rglob('*.yaml'):
                text = path.read_text()
                if any('kind: ' + kind in text for kind in (
                        'SnapshotSchedule', 'SnapshotReplication', 'ReplicationSource')):
                    paths.append(path)
        expression = ('select(.kind == "Kustomization" or .kind == "SnapshotSchedule" '
                      'or .kind == "SnapshotReplication" or .kind == "ReplicationSource") | '
                      '{"path": filename, "kind": .kind, "name": .metadata.name, '
                      '"vars": (.spec.postBuild.substitute // {}), '
                      '"cron": (.spec.schedule.cron // .spec.trigger.schedule)}')
        records = yaml_json(expression, sorted(set(paths)))
        app_variables = {}
        for record in records:
            if record['kind'] == 'Kustomization':
                app_variables.setdefault(str(Path(record['path']).parent), {}).update(record['vars'])
        checked = 0
        for record in records:
            variables = dict(common)
            relative = Path(record['path']).relative_to(app_root) if str(record['path']).startswith(str(app_root) + '/') else None
            if relative:
                variables.update(app_variables.get(str(app_root / relative.parts[0] / relative.parts[1]), {}))
            if record['kind'] == 'Kustomization':
                crons = [value for name, value in record['vars'].items()
                         if name.startswith('VOLSYNC_') and 'SCHEDULE' in name]
                variables.update(record['vars'])
            else:
                crons = [record['cron']] if record['cron'] else []
            for value in crons:
                with self.subTest(path=record['path'], name=record['name']):
                    self.assertTrue(at_most_daily(resolve(value, variables)), value)
                    checked += 1
        self.assertGreater(checked, len(common))


if __name__ == '__main__':
    unittest.main()
