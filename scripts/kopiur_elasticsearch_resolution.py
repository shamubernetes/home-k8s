"""Resolve caller-qualified consumer indices under independent live authority.

Release/runtime evidence is supplied, never inferred from defaults or a ledger
hash. The guard must revalidate that evidence and the engine lifetime on every
read. This read-only preparation grants no source export or writer admission.
"""
import copy
import re

from kopiur_elasticsearch_catalog import qualified_indices
from kopiur_elasticsearch_escrow import EscrowError, _binding, _digest, _encoded
from kopiur_elasticsearch_restore import consumer_inventory
from kopiur_elasticsearch_source import LoopbackSnapshotIO


def validate_contracts(contracts):
    if not isinstance(contracts, list) or not contracts:
        raise EscrowError('explicit source-backed consumer selections required')
    pairs = set()
    for item in contracts:
        if (not isinstance(item, dict) or set(item) != {
                'application', 'store', 'selectors', 'required_indices', 'release', 'runtime'}
                or any(not isinstance(item[k], str) or not item[k] for k in ('application', 'store'))):
            raise EscrowError('source-backed consumer selection invalid')
        pair = item['application'], item['store']
        if pair in pairs:
            raise EscrowError('duplicate source-backed consumer selection')
        pairs.add(pair)
        selectors = item['selectors']
        if (not isinstance(selectors, list) or not selectors
                or any(not isinstance(s, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*\*?', s)
                       for s in selectors)
                or len(selectors) != len(set(selectors))):
            raise EscrowError('exact indices or terminal-prefix selections required')
        required = item['required_indices']
        if not isinstance(required, list):
            raise EscrowError('explicit required index coverage required')
        if required:
            qualified_indices(required, 'required consumer indices')
        # Exact selectors cannot silently omit a generation/state index. Prefix
        # selectors expand every native match, never just caller-known names.
        if (any(s not in required for s in selectors if not s.endswith('*'))
                or any(not any(matches(name, s) for s in selectors) for name in required)):
            raise EscrowError('required generation/state index coverage differs')
        release, runtime = item['release'], item['runtime']
        if (not isinstance(release, dict) or set(release) != {'image', 'source_revision'}
                or not isinstance(release['image'], str)
                or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', release['image'])
                or not isinstance(release['source_revision'], str)
                or not re.fullmatch(r'[0-9a-f]{40}', release['source_revision'])
                or not isinstance(runtime, dict) or set(runtime) != {
                    'pod_uid', 'container_id', 'started_at', 'restart_count', 'selection_sha256'}
                or any(not isinstance(runtime[k], str) or not runtime[k]
                       for k in ('pod_uid', 'container_id', 'started_at'))
                or type(runtime['restart_count']) is not int or runtime['restart_count'] < 0
                or not isinstance(runtime['selection_sha256'], str)
                or runtime['selection_sha256'] != _digest(_encoded({
                    'selectors': selectors, 'required_indices': required}))):
            raise EscrowError('bound release and effective runtime selection evidence required')
    return sorted(copy.deepcopy(contracts), key=lambda r: (r['application'], r['store']))


def matches(name, selector):
    return name.startswith(selector[:-1]) if selector.endswith('*') else name == selector


def resolution_path(selectors):
    return '/_resolve/index/' + ','.join(selectors) + '?expand_wildcards=all'


class LoopbackResolutionIO(LoopbackSnapshotIO):
    """Reuse escaped stdin credentials, without permitting URLs or archive reads."""
    def __init__(self, *, exec_read, contracts):
        self.exec_read = exec_read
        self.paths = frozenset(resolution_path(c['selectors']) for c in validate_contracts(contracts))

    def read_archive(self, location):
        raise EscrowError('index resolution capability cannot read archives')


class IndexResolution:
    def __init__(self, binding, *, ledger, backend, contracts, guard, request):
        self.binding = _binding(binding)
        self.contracts = validate_contracts(contracts)
        self.ledger, self.backend = copy.deepcopy(ledger), backend
        self.guard, self.request = guard, request
        # Check the entire reciprocal roster before even the first native read.
        records = [dict(application=c['application'], store=c['store'], indices=['preflight'])
                   for c in self.contracts]
        consumer_inventory(self.ledger, self.binding, backend, records, ['preflight'])

    def check(self):
        # A callback receiving the contracts cannot accidentally qualify only
        # the Elasticsearch Pod while ignoring consumer release/runtime drift.
        try:
            authorized = self.guard(copy.deepcopy(self.contracts))
        except Exception:
            raise EscrowError('engine and consumer selection authority check failed') from None
        if authorized is not True:
            raise EscrowError('engine and consumer selection authority required')
        return True

    def _read(self, contract, credentials):
        self.check()
        try:
            response = self.request(resolution_path(contract['selectors']), copy.deepcopy(credentials))
        except Exception:
            raise EscrowError('authenticated native index resolution failed') from None
        self.check()
        if (not isinstance(response, dict) or set(response) != {'indices', 'aliases', 'data_streams'}
                or response['aliases'] != [] or response['data_streams'] != []
                or not isinstance(response['indices'], list) or not response['indices']):
            raise EscrowError('physical native index expansion required')
        names = []
        for item in response['indices']:
            if (not isinstance(item, dict) or not {'name', 'attributes'} <= set(item)
                    or not set(item) <= {'name', 'attributes', 'aliases'}
                    or not isinstance(item['name'], str)
                    or item['attributes'] != ['open']
                    or not isinstance(item.get('aliases', []), list)
                    or any(not isinstance(a, str) or not a for a in item.get('aliases', []))
                    or len(item.get('aliases', [])) != len(set(item.get('aliases', [])))
                    or not any(matches(item['name'], s) for s in contract['selectors'])):
                raise EscrowError('open exact native application indices required')
            names.append(item['name'])
        qualified_indices(names, 'native expanded indices')
        if not set(contract['required_indices']) <= set(names):
            raise EscrowError('required generation/state index absent from native catalog')
        return {'application': contract['application'], 'store': contract['store'],
                'indices': sorted(names)}

    def observe(self, credentials):
        records = [self._read(c, credentials) for c in self.contracts]
        indices = sorted({name for record in records for name in record['indices']})
        inventory = consumer_inventory(self.ledger, self.binding, self.backend, records, indices)
        self.check()
        return {'indices': indices, 'consumers': inventory['records'],
                'consumer_inventory_sha256': inventory['sha256'],
                'selection_contracts_sha256': _digest(_encoded(self.contracts)),
                'production_acceptance': False}

    def capture(self, credentials):
        first = self.observe(credentials)
        if _encoded(self.observe(credentials)) != _encoded(first):
            raise EscrowError('native consumer index expansion changed during capture')
        self.check()
        return first

    def revalidate(self, expected, read_credentials):
        """Check both authorities before and after credential/authentication I/O."""
        self.check()
        credentials = read_credentials()
        self.check()
        return self.verify(expected, credentials)

    def verify(self, expected, credentials):
        if _encoded(self.capture(credentials)) != _encoded(expected):
            raise EscrowError('prepared native consumer index expansion changed')
        return True
