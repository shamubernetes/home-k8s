"""Original authority regressions and real ARC-only /proc reader qualification."""
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kopiur_elasticsearch_consumer_authority import (
    ConsumerAuthority, PROFILES, process_projection, projection_command,
)
from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded


IMAGE = 'ghcr.io/thezoo-house/cowbell@sha256:' + 'f51c7a8c7dffc41f4fb1b8dc277f2c0a6fd7fb9fa3c8bb6ce8caff35c30dce3b'
PYTHON_IMAGE = 'docker.io/bbilly1/tubearchivist@sha256:ba1c846ddd0c6fdd0f040727129d2466e03b7a0c26499223839d450c7586ac09'
# Exact public runtime base from the original Cowbell release Dockerfile.
# ARC has no private GHCR pull grant. This qualifies the Bun reader, not the
# application's loaded configuration or original Cowbell image contents.
BUN_IMAGE = 'docker.io/oven/bun:1.3.14-alpine@sha256:5acc90a93e91ff07bf72aa90a7c9f0fa189765aec90b47bdbf2152d2196383c0'
SOURCE_FILES = ('web/src/config/runtime.ts', 'web/src/catalog/model.ts', 'web/src/catalog/elasticsearch.ts')


def plan(app='services/zoo-cowbell'):
    return {'application': app, 'store': 'elasticsearch-indices:' + app, 'image': IMAGE, 'declared_image': IMAGE,
        'source_url': 'https://github.com/example/consumer', 'source_revision': 'a' * 40,
        'source_checkout': '/source', 'source_files': {p: _digest(p.encode()) for p in SOURCE_FILES},
        'selectors': ['fixture', 'fixture-state'], 'required_indices': ['fixture', 'fixture-state'],
        'expected_selection': None, 'default_selection': 'fixture'}


def projection():
    return {'boot_id': '11111111-1111-1111-1111-111111111111',
            'processes': [{'pid': 1, 'start_ticks': '100', 'selection': None},
                          {'pid': 3, 'start_ticks': '110', 'selection': None}]}


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.grant = Mock(return_value=True)
        self.calls = []
        self.deployments, self.rs, self.pods = [], [], []
        for n, container in PROFILES[self.plan['application']]['deployments'].items():
            labels = {'role': n}
            count = 2 if n == 'zoo-cowbell' else 1
            self.deployments.append({'metadata': {'name': n, 'uid': n+'-uid', 'generation': 7},
                'spec': {'replicas': count, 'selector': {'matchLabels': labels}, 'template': {'spec': {}}},
                'status': {'observedGeneration': 7, 'replicas': count, 'updatedReplicas': count,
                           'readyReplicas': count, 'availableReplicas': count}})
            self.rs.append({'metadata': {'name': n+'-rs', 'uid': n+'-rs-uid',
                'ownerReferences': [{'controller': True, 'kind': 'Deployment', 'uid': n+'-uid'}]}})
            for i in range(count):
                name = n+'-pod-'+str(i)
                self.pods.append({'metadata': {'name': name, 'uid': name+'-uid', 'labels': labels,
                    'ownerReferences': [{'controller': True, 'kind': 'ReplicaSet', 'uid': n+'-rs-uid'}]},
                    'spec': {'containers': [{'name': container, 'image': IMAGE}]},
                    'status': {'phase': 'Running', 'containerStatuses': [{'name': container,
                        'ready': True, 'imageID': IMAGE, 'containerID': 'containerd://' + _digest(name.encode()),
                        'restartCount': 0, 'state': {'running': {'startedAt': '2026-10-01T00:00:00Z'}}}]}})
        self.config = {'config': {'Labels': {'org.opencontainers.image.source': self.plan['source_url'],
                          'org.opencontainers.image.revision': self.plan['source_revision']}}}
        self.processes = projection()
        self.authority = self.make()

    def run_read(self, argv, data=None):
        self.calls.append((argv, data))
        if argv[0] == 'crane': return _encoded(self.config)
        if argv[0] == 'git': return argv[-1].split(':', 1)[1].encode()
        if argv[1] == 'get':
            return _encoded({'items': {'deployments': self.deployments,
                                      'replicasets': self.rs, 'pods': self.pods}[argv[2]]})
        if argv[1] == 'exec': return _encoded(self.processes)
        raise AssertionError('unqualified operation')

    def make(self):
        return ConsumerAuthority([self.plan], require_authority=self.grant, run=self.run_read)

    def exec_calls(self):
        return [c for c in self.calls if c[0][:2] == ['kubectl', 'exec']]

    def test_complete_query_worker_dispatcher_roster_and_safe_receipt(self):
        contracts = self.authority.prepare()
        self.assertTrue(self.authority.require(contracts))
        receipt = self.authority.receipt()
        self.assertEqual(receipt['container_count'], 4)
        self.assertEqual(receipt['application_count'], 1)
        self.assertTrue(receipt['startup_selection_verified'])
        self.assertFalse(receipt['all_writer_fence_verified'])
        self.assertFalse(receipt['application_loaded_selection_verified'])
        self.assertFalse(receipt['production_recovery_accepted'])
        self.assertNotIn('processes', receipt)
        self.assertEqual({c[0][4] for c in self.exec_calls()}, {p['metadata']['name'] for p in self.pods})
        for _, data in self.exec_calls(): self.assertEqual(json.loads(data), {'variable': 'CATALOG_INDEX_PREFIX'})

    def test_no_authority_means_no_metadata_or_process_io(self):
        self.grant.return_value = False
        with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.calls, [])

    def test_revocation_during_pre_exec_pod_get_prevents_every_exec(self):
        original = self.run_read
        def revoke(argv, data=None):
            result = original(argv, data)
            if argv[:3] == ['kubectl', 'get', 'pods']: self.grant.return_value = False
            return result
        self.authority.run = revoke
        with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_composed_source_checkpoint_denies_mid_observation_reads(self):
        source_live = [True]
        bound = self.authority.with_checkpoint(lambda: source_live[0])
        original = self.run_read
        def revoke(argv, data=None):
            result = original(argv, data)
            if argv[:3] == ['kubectl', 'get', 'pods']: source_live[0] = False
            return result
        bound.run = revoke
        with self.assertRaises(EscrowError): bound.prepare()
        self.assertEqual(self.exec_calls(), [])
        self.assertTrue(self.grant.return_value)

    def test_extra_consumer_deployment_with_different_selector_denied(self):
        d = copy.deepcopy(self.deployments[0]); d['metadata']['name'] = 'zoo-cowbell-extra'
        d['spec']['selector'] = {'matchLabels': {'role': 'extra'}}
        self.deployments.append(d)
        with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_orphan_consumer_without_expected_labels_denied(self):
        p = copy.deepcopy(self.pods[0]); p['metadata']['name'] = 'rogue-reader'
        p['metadata']['uid'] = 'rogue-uid'; p['metadata']['labels'] = {}
        self.pods.append(p)
        with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_unrelated_service_does_not_block_roster(self):
        d = copy.deepcopy(self.deployments[0]); d['metadata']['name'] = 'another-app'
        d['spec']['template']['spec'] = {'containers': [{'name': 'app', 'image': 'example/other@sha256:'+'f'*64}]}
        self.deployments.append(d)
        p = copy.deepcopy(self.pods[0]); p['metadata']['name'] = 'another-app-pod'
        p['metadata']['uid'] = 'other-uid'; p['metadata']['labels'] = {}
        p['spec']['containers'][0]['image'] = 'example/other@sha256:'+'f'*64
        self.pods.append(p)
        self.authority.prepare()
        self.assertEqual(self.authority.receipt()['container_count'], 4)

    def test_terminated_migration_pod_not_counted_as_active_consumer(self):
        p = copy.deepcopy(self.pods[0]); p['metadata']['name'] = 'zoo-cowbell-migrate'
        p['metadata']['uid'] = 'migration-uid'; p['metadata']['labels'] = {}
        p['status']['phase'] = 'Succeeded'
        p['status']['containerStatuses'][0]['state'] = {'terminated': {'exitCode': 0}}
        self.pods.append(p)
        self.authority.prepare()
        self.assertEqual(self.authority.receipt()['container_count'], 4)

    def test_missing_or_wrong_provenance_denies_every_process_read(self):
        for labels in ({}, {'org.opencontainers.image.source': self.plan['source_url'],
                           'org.opencontainers.image.revision': 'b'*40}):
            self.config['config']['Labels'] = labels
            with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_source_bytes_must_match_reviewed_release(self):
        self.plan['source_files'][SOURCE_FILES[0]] = 'b'*64
        with self.assertRaises(EscrowError): self.make().prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_missing_dispatcher_denies_process_reads(self):
        self.deployments.pop()
        with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_extra_or_missing_replica_denies_process_reads(self):
        for delta in (-1, 1):
            pods = copy.deepcopy(self.pods)
            if delta == -1: self.pods.pop()
            else: self.pods.append(copy.deepcopy(self.pods[-1]))
            with self.assertRaises(EscrowError): self.authority.prepare()
            self.pods = pods
        self.assertEqual(self.exec_calls(), [])

    def test_foreign_replicaset_owner_denied(self):
        self.rs[0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        with self.assertRaises(EscrowError): self.authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_runtime_digest_or_restart_drift_denied(self):
        contracts = self.authority.prepare()
        for key, value in [('imageID', IMAGE[:-1]+'0'), ('restartCount', 1), ('restartCount', True)]:
            status = self.pods[0]['status']['containerStatuses'][0]
            original = status[key]; status[key] = value
            with self.assertRaises(EscrowError): self.authority.require(contracts)
            status[key] = original

    def test_same_pod_container_lifetime_start_change_denied(self):
        contracts = self.authority.prepare()
        self.pods[0]['status']['containerStatuses'][0]['state']['running']['startedAt'] = 'new-start'
        with self.assertRaises(EscrowError): self.authority.require(contracts)

    def test_tagged_declaration_and_exact_qualified_runtime_identity_accepted(self):
        self.plan['declared_image'] = IMAGE.replace('@sha256:', ':v0.5.11@sha256:')
        for pod in self.pods:
            pod['spec']['containers'][0]['image'] = self.plan['declared_image']
        authority = self.make()
        contracts = authority.prepare()
        self.assertTrue(authority.require(contracts))
        self.pods[0]['status']['containerStatuses'][0]['imageID'] = IMAGE[:-1]+'0'
        with self.assertRaises(EscrowError): authority.require(contracts)

    def test_unqualified_runtime_digest_cannot_replace_declared_digest(self):
        self.plan['declared_image'] = IMAGE[:-1]+'0'
        with self.assertRaises(EscrowError): self.make()

    def test_same_pid_process_start_tick_change_denied(self):
        contracts = self.authority.prepare()
        self.processes['processes'][1]['start_ticks'] = '111'
        with self.assertRaises(EscrowError): self.authority.require(contracts)

    def test_new_process_denied(self):
        contracts = self.authority.prepare()
        self.processes['processes'].append({'pid': 7, 'start_ticks': '120', 'selection': None})
        with self.assertRaises(EscrowError): self.authority.require(contracts)

    def test_environment_override_in_one_child_denied(self):
        self.processes['processes'][1]['selection'] = 'foreign'
        with self.assertRaises(EscrowError): self.authority.prepare()

    def test_template_change_detected_even_without_container_restart(self):
        contracts = self.authority.prepare()
        self.deployments[0]['spec']['template']['spec']['new'] = True
        with self.assertRaises(EscrowError): self.authority.require(contracts)

    def test_projection_read_lifetime_change_rejects_returned_bytes(self):
        original = self.run_read
        def drift(argv, data=None):
            result = original(argv, data)
            if argv[:2] == ['kubectl','exec']:
                self.pods[0]['status']['containerStatuses'][0]['restartCount'] += 1
            return result
        self.authority.run = drift
        with self.assertRaisesRegex(EscrowError, 'during process read'): self.authority.prepare()

    def test_projection_read_revocation_discards_returned_bytes(self):
        original = self.run_read
        def revoke(argv, data=None):
            result = original(argv, data)
            if argv[:2] == ['kubectl','exec']: self.grant.return_value = False
            return result
        self.authority.run = revoke
        with self.assertRaises(EscrowError): self.authority.prepare()

    def test_different_selection_contract_cannot_reuse_authority(self):
        contracts = self.authority.prepare()
        contracts[0]['release']['source_revision'] = 'b'*40
        with self.assertRaises(EscrowError): self.authority.require(contracts)

    def test_state_index_cannot_be_removed_from_cowbell_plan(self):
        self.plan['selectors'] = self.plan['required_indices'] = ['fixture']
        with self.assertRaises(EscrowError): self.make()

    def test_source_contract_must_cover_runtime_generation_and_queries(self):
        self.plan['source_files'].pop(SOURCE_FILES[1])
        with self.assertRaises(EscrowError): self.make()

    def test_duplicate_application_cannot_replace_roster(self):
        with self.assertRaises(EscrowError):
            ConsumerAuthority([self.plan, self.plan], require_authority=self.grant)

    def test_tubearchivist_missing_labels_cannot_be_inferred_from_tag(self):
        p = plan('media/tubearchivist'); p['default_selection'] = None
        self.config['config']['Labels'] = {}
        authority = ConsumerAuthority([p], require_authority=self.grant, run=self.run_read)
        with self.assertRaisesRegex(EscrowError, 'provenance absent'): authority.prepare()
        self.assertEqual(self.exec_calls(), [])

    def test_external_error_message_and_chain_redacted(self):
        private = secrets.token_hex(16)
        self.authority.run = Mock(side_effect=EscrowError(private))
        with self.assertRaises(EscrowError) as caught: self.authority.prepare()
        self.assertNotIn(private, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_unprepared_receipt_or_contracts_denied(self):
        for op in (self.authority.receipt, self.authority.contracts):
            with self.assertRaises(EscrowError): op()

    def test_process_boolean_pid_or_duplicate_or_no_init_denied(self):
        for change in ('bool', 'duplicate', 'init', 'ticks'):
            p = projection()
            if change == 'bool': p['processes'][0]['pid'] = True
            if change == 'duplicate': p['processes'].append(copy.deepcopy(p['processes'][0]))
            if change == 'init': p['processes'].pop(0)
            if change == 'ticks': p['processes'][0]['start_ticks'] = '001'
            with self.assertRaises(EscrowError): process_projection(p, expected_selection=None)


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-'),
                     'real process readers qualify only on existing ARC')
class NativeProjectionTests(unittest.TestCase):
    """Fresh unprivileged containers, no production endpoints or host mounts."""
    def exercise(self, interpreter, image):
        name = 'k8s92-projection-' + secrets.token_hex(8)
        private = secrets.token_hex(24)
        sleeper = (['python3', '-c', 'import subprocess,time;subprocess.Popen(["python3","-c","import time;time.sleep(180)"]);time.sleep(180)']
                   if interpreter == 'python3' else ['bun', '--no-install', '--no-env-file', '--eval',
                   'Bun.spawn([process.execPath,"--no-env-file","--eval","setTimeout(()=>{},180000)"]);setTimeout(()=>{},180000)'])
        def docker(*args, data=None):
            r = subprocess.run(['docker', *args], input=data, capture_output=True, timeout=180)
            if r.returncode: raise AssertionError('owned projection fixture operation failed')
            return r.stdout
        try:
            docker('run', '-d', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--user', '1000:1000',
                   '-e', 'CATALOG_INDEX_PREFIX=fixture', '-e', 'SYNTHETIC_PRIVATE=' + private,
                   '--entrypoint', sleeper[0], image, *sleeper[1:])
            command = projection_command(interpreter)
            deadline = time.monotonic() + 30
            while True:
                raw = docker('exec', '-i', name, *command, data=_encoded({'variable': 'CATALOG_INDEX_PREFIX'}))
                if len(json.loads(raw)['processes']) == 2:
                    break
                if time.monotonic() >= deadline:
                    self.fail('owned multiprocess projection never became ready')
                time.sleep(0.2)
            self.assertNotIn(private.encode(), raw)
            value = process_projection(json.loads(raw), expected_selection='fixture')
            self.assertEqual(len(value['processes']), 2)
            second = docker('exec', '-i', name, *command, data=_encoded({'variable': 'CATALOG_INDEX_PREFIX'}))
            self.assertEqual(_encoded(json.loads(second)), _encoded(json.loads(raw)))
            denied = subprocess.run(['docker', 'exec', '-i', name, *command],
                                   input=_encoded({'variable': 'SYNTHETIC_PRIVATE'}),
                                   capture_output=True, timeout=30)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(denied.stdout, b'')
            self.assertNotIn(private.encode(), denied.stderr)
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
            remaining = subprocess.run(['docker', 'inspect', name], capture_output=True, timeout=30)
            self.assertNotEqual(remaining.returncode, 0)

    def test_real_python_proc_reader_with_private_environment(self):
        self.exercise('python3', PYTHON_IMAGE)

    def test_real_bun_proc_reader_with_private_environment(self):
        self.exercise('bun', BUN_IMAGE)


if __name__ == '__main__':
    unittest.main()
