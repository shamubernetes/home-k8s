"""Retire the ownerless bootstrap and unused VolSync API after runtime removal."""
import json
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
token = (root / 'token').read_text().strip()
context = ssl.create_default_context(cafile=str(root / 'ca.crt'))
base = 'https://kubernetes.default.svc'
orphan = '/apis/volsync.backube/v1alpha1/namespaces/arrs/replicationdestinations/overseerr-dst'
definitions = (
    'replicationsources.volsync.backube',
    'replicationdestinations.volsync.backube',
    'volsyncmonitors.homelab.rafaribe.com',
)


def call(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(base + path, data=body, method=method,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, context=context, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError('retirement API operation failed: HTTP ' + str(error.code)) from None


def delete_exact(path, resource):
    call('DELETE', path, {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                         'preconditions': {'uid': resource['metadata']['uid']},
                         'propagationPolicy': 'Background'})


original = call('GET', orphan)
if original is not None:
    metadata = original['metadata']
    assert metadata['name'] == 'overseerr-dst' and metadata['namespace'] == 'arrs'
    assert not metadata.get('ownerReferences') and not metadata.get('finalizers')
    assert metadata.get('labels', {}).get('app.kubernetes.io/name') == 'overseerr'
    delete_exact(orphan, original)
assert call('GET', orphan) is None

# Never remove API definitions while any legacy runtime or custom object remains.
# The independent CSI snapshot-controller and its APIs are deliberately untouched.
deadline = time.monotonic() + 600
while True:
    releases = [call('GET', '/apis/helm.toolkit.fluxcd.io/v2/namespaces/volsync-system/'
                     'helmreleases/' + name)
                for name in ('volsync', 'homelab-assistant', 'homelab-assistant-crds')]
    resources = [call('GET', path) for path in (
        '/apis/volsync.backube/v1alpha1/replicationsources',
        '/apis/volsync.backube/v1alpha1/replicationdestinations',
        '/apis/homelab.rafaribe.com/v1alpha1/volsyncmonitors',
    )]
    if not any(releases) and all(not item or not item['items'] for item in resources):
        break
    if time.monotonic() >= deadline:
        raise TimeoutError('legacy runtime or custom resources remain; API definitions preserved')
    time.sleep(5)

for name in definitions:
    path = '/apis/apiextensions.k8s.io/v1/customresourcedefinitions/' + name
    resource = call('GET', path)
    if resource is not None:
        assert resource['metadata']['name'] == name
        assert resource['spec']['group'] in ('volsync.backube', 'homelab.rafaribe.com')
        delete_exact(path, resource)
    while call('GET', path) is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError('retired API definition deletion did not finish')
        time.sleep(2)
print(json.dumps({'orphan_bootstrap_absent': True, 'legacy_runtime_absent': True,
                  'legacy_custom_objects_absent': True, 'retired_api_definitions_absent': True,
                  'production_pvc_access': False, 'csi_snapshot_controller_untouched': True}), flush=True)
