"""Remove the retained, ownerless legacy bootstrap object only."""
import json
from pathlib import Path
import ssl
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
token = (root / 'token').read_text().strip()
context = ssl.create_default_context(cafile=str(root / 'ca.crt'))
url = ('https://kubernetes.default.svc/apis/volsync.backube/v1alpha1/'
       'namespaces/arrs/replicationdestinations/overseerr-dst')


def call(method, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=body, method=method,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, context=context, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError('bootstrap API operation failed: HTTP ' + str(error.code)) from None


original = call('GET')
if original is not None:
    metadata = original['metadata']
    assert metadata['name'] == 'overseerr-dst' and metadata['namespace'] == 'arrs'
    assert not metadata.get('ownerReferences') and not metadata.get('finalizers')
    assert metadata.get('labels', {}).get('app.kubernetes.io/name') == 'overseerr'
    call('DELETE', {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                    'preconditions': {'uid': metadata['uid']}, 'propagationPolicy': 'Background'})
assert call('GET') is None
print(json.dumps({'orphan_bootstrap_absent': True, 'production_pvc_access': False}), flush=True)
