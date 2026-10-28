"""Exercise the deployed Kopiur stale selector with promtool, not a Python model.

Run: python3 scripts/tests/test-kopiur-prometheusrule.py
Requires existing yq and promtool binaries, or PROMTOOL=/path/to/promtool.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'kubernetes/apps/kopiur-system/kopiur/app/helmrelease.yaml'
NAMESPACE = 'home-assistant'
LEGACY = 'home-assistant-native'
REPLACEMENTS = [f'{LEGACY}-{part}' for part in ('bundle', 'config', 'git')]
TIMESTAMP = 'kopiur_policy_last_backup_success_timestamp_seconds'
SUCCESS = 'kopiur_snapshotpolicy_last_backup_success'
FAILURES = 'kopiur_snapshot_consecutive_failures'
EVAL_SECONDS = 200 * 3600
THRESHOLD = 604800


def series(metric, policy, value, namespace=NAMESPACE, **labels):
    labels = {'namespace': namespace, 'name' if metric == FAILURES else 'policy': policy,
              **labels}
    selector = ','.join(f'{key}={json.dumps(value)}' for key, value in labels.items())
    return {'series': f'{metric}{{{selector}}}', 'values': f'{value}+0x200'}


def timestamp(policy, age=1000, namespace=NAMESPACE, **labels):
    return series(TIMESTAMP, policy, EVAL_SECONDS - age, namespace, **labels)


def sample(policy, value, namespace=NAMESPACE, failed=False, **labels):
    labels = {'namespace': namespace, 'policy': policy, **labels}
    if failed:
        labels.update({'__name__': FAILURES, 'name': policy})
    selector = ','.join(f'{key}={json.dumps(value)}' for key, value in labels.items())
    return {'labels': '{' + selector + '}', 'value': value}


class KopiurStaleSelectorTests(unittest.TestCase):
    def test_retained_original_requires_complete_fresh_replacement_coverage(self):
        # Read the actual postRenderer expression so selector regressions fail.
        expression = json.loads(subprocess.check_output([
            'yq', '-o=json',
            '.spec.postRenderers[].kustomize.patches[].patch | from_yaml | .[] | '
            'select(.op == "replace" and .path == "/spec/groups/0/rules/1/expr") | .value',
            str(RELEASE),
        ], text=True))
        legacy = [timestamp(LEGACY, THRESHOLD + 1), series(SUCCESS, LEGACY, 1)]
        stale_legacy = sample(LEGACY, THRESHOLD + 1)
        fresh = [timestamp(policy) for policy in REPLACEMENTS]
        cases = []

        def case(name, inputs, expected):
            cases.append({'name': name, 'interval': '1h', 'input_series': inputs,
                          'promql_expr_test': [{'expr': expression, 'eval_time': '200h',
                                                'exp_samples': expected}]})

        case('successful retained original with all three fresh replacements', legacy + fresh, [])
        case('replacement freshness boundary is inclusive', legacy + [
            timestamp(policy, THRESHOLD) for policy in REPLACEMENTS], [])
        case('legacy success zero is not exempt', [legacy[0], series(SUCCESS, LEGACY, 0)] + fresh,
             [stale_legacy])
        case('missing legacy success is not exempt', [legacy[0]] + fresh, [stale_legacy])
        case('no replacement metrics fails closed', legacy, [stale_legacy])
        case('legacy without a success timestamp still alerts on failures', [
            series(SUCCESS, LEGACY, 0), series(FAILURES, LEGACY, 2)] + fresh,
             [sample(LEGACY, 2, failed=True)])

        for policy in REPLACEMENTS:
            others = [timestamp(other) for other in REPLACEMENTS if other != policy]
            case(f'missing {policy} disables exemption', legacy + others, [stale_legacy])
            case(f'stale {policy} disables exemption and alerts itself',
                 legacy + others + [timestamp(policy, THRESHOLD + 1)],
                 [stale_legacy, sample(policy, THRESHOLD + 1)])
            case(f'active {policy} staleness remains visible',
                 [timestamp(policy, THRESHOLD + 1), series(SUCCESS, policy, 1)],
                 [sample(policy, THRESHOLD + 1)])
            case(f'active {policy} without success still alerts on failures',
                 [series(FAILURES, policy, 2), series(SUCCESS, policy, 0)],
                 [sample(policy, 2, failed=True)])
            case(f'wrong namespace {policy} cannot complete coverage',
                 legacy + others + [timestamp(policy, namespace='other')], [stale_legacy])
            case(f'lookalike {policy} cannot complete coverage',
                 legacy + others + [timestamp(policy + '-extra')], [stale_legacy])

        case('unrelated legacy policy in same namespace remains covered', legacy + fresh + [
            timestamp('other-native', THRESHOLD + 1), series(SUCCESS, 'other-native', 1)],
             [sample('other-native', THRESHOLD + 1)])
        case('same legacy policy in other namespace remains covered', legacy + fresh + [
            timestamp(LEGACY, THRESHOLD + 1, namespace='other'),
            series(SUCCESS, LEGACY, 1, namespace='other')],
             [sample(LEGACY, THRESHOLD + 1, namespace='other')])
        for success in (0, 1):
            case(f'preserve Radarr original success={success} behavior', [
                timestamp('radarr-k8s92-original', THRESHOLD + 1, namespace='arrs'),
                series(SUCCESS, 'radarr-k8s92-original', success, namespace='arrs')],
                 [] if success else [sample('radarr-k8s92-original', THRESHOLD + 1, namespace='arrs')])
        case('active Radarr daily policy remains covered', [
            timestamp('radarr', THRESHOLD + 1, namespace='arrs'),
            series(SUCCESS, 'radarr', 1, namespace='arrs')],
             [sample('radarr', THRESHOLD + 1, namespace='arrs')])
        case('success in repository A does not hide no-success failure in repository B', [
            timestamp('other-native', repository='a'),
            series(FAILURES, 'other-native', 2, repository='b')],
             [sample('other-native', 2, failed=True, repository='b')])

        with tempfile.TemporaryDirectory(prefix='kopiur-promql-') as temporary:
            fixture = Path(temporary) / 'tests.json'
            fixture.write_text(json.dumps({'evaluation_interval': '1h', 'tests': cases}))
            result = subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules',
                                     str(fixture)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(f'Validated {len(cases)} Kopiur PromQL regression cases')


if __name__ == '__main__':
    unittest.main()
