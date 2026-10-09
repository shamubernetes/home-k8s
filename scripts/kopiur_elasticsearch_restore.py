"""Prepare a source-bound restore without granting production execution authority.

Every runtime variable needs an explicit replay or omission decision. Preparation
performs no I/O and never changes captured bytes, keys or security identities.
The caller still owns target isolation and live admission around every operation.
"""
import copy
import hashlib
import json
import re

from kopiur_elasticsearch_capture import credentials_from_bytes, validate_snapshot_selection
from kopiur_elasticsearch_escrow import EscrowError, _binding, _encoded, _validate_parts


def consumer_inventory(ledger, binding, backend, records, indices):
    """Bind every declared search consumer. This does not verify its queries."""
    binding = _binding(binding)
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('physical_stores'), list)
            or not isinstance(ledger.get('applications'), list)
            or not isinstance(backend, str) or not backend
            or not isinstance(records, list) or not records
            or not isinstance(indices, list) or not indices
            or any(not isinstance(i, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*', i)
                      or i.startswith('.') for i in indices)
            or len(indices) != len(set(indices))
            or any(not isinstance(s, dict) for s in ledger['physical_stores'])
            or any(not isinstance(a, dict) for a in ledger['applications'])):
        raise EscrowError('explicit ledger and consumer inventory required')
    stores = [s for s in ledger['physical_stores']
              if s.get('kind') == 'external_elasticsearch_indices' and s.get('backend_contract') == backend]
    # Dependencies use IDs, so even a duplicate outside the selected backend
    # makes the supplied ledger ambiguous.
    for entries in (ledger['physical_stores'], ledger['applications']):
        ids = [entry.get('id') for entry in entries]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise EscrowError('globally unique ledger identities required')
    required = set()
    store_ids = set()
    for store in stores:
        name, apps = store.get('id'), store.get('consumer_contracts')
        if (not isinstance(name, str) or not name or name in store_ids
                or not isinstance(apps, list) or not apps
                or any(not isinstance(a, str) or not a for a in apps)
                or len(apps) != len(set(apps))):
            raise EscrowError('complete unique search-store consumer contracts required')
        store_ids.add(name)
        for app in apps:
            matches = [a for a in ledger['applications'] if a.get('id') == app]
            if (len(matches) != 1 or not isinstance(matches[0].get('state_dependencies'), list)
                    or name not in matches[0]['state_dependencies']):
                raise EscrowError('reciprocal original consumer dependency required')
            required.add((app, name))
    if not required:
        raise EscrowError('declared search-store consumers required')
    for app in ledger['applications']:
        dependencies = app.get('state_dependencies')
        if (not isinstance(app.get('id'), str) or not isinstance(dependencies, list)
                or any(not isinstance(d, str) for d in dependencies)):
            raise EscrowError('complete application dependencies required')
        if any((app['id'], name) not in required for name in set(dependencies) & store_ids):
            raise EscrowError('undeclared reverse consumer dependency refused')
    actual, covered = set(), set()
    for record in records:
        if (not isinstance(record, dict) or set(record) != {'application', 'store', 'indices'}
                or any(not isinstance(record[k], str) or not record[k] for k in ('application', 'store'))
                or not isinstance(record['indices'], list) or not record['indices']
                or any(not isinstance(i, str) for i in record['indices'])
                or len(record['indices']) != len(set(record['indices']))
                or not set(record['indices']) <= set(indices)):
            raise EscrowError('exact per-consumer native index coverage required')
        key = (record['application'], record['store'])
        if key in actual:
            raise EscrowError('duplicate consumer coverage refused')
        actual.add(key)
        covered.update(record['indices'])
    if actual != required or covered != set(indices):
        raise EscrowError('native restore differs from complete declared consumer coverage')
    ordered = sorted(copy.deepcopy(records), key=lambda r: (r['application'], r['store']))
    try:
        digest = hashlib.sha256(_encoded({'binding': binding, 'backend': backend, 'records': ordered,
                         'ledger_sha256': hashlib.sha256(_encoded(ledger)).hexdigest()})).hexdigest()
    except (TypeError, ValueError, OverflowError):
        raise EscrowError('serializable original ledger required') from None
    return {'records': ordered, 'sha256': digest}


class RestorePlan:
    def __init__(self, manifest, parts, *, replay, omit, repository, snapshot, location, indices,
                 ledger, backend, consumers):
        if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'binding', 'entries'}
                or manifest['schema'] != 'k8s92-elasticsearch-escrow/v1'):
            raise EscrowError('complete restore manifest required')
        binding = _binding(manifest['binding'])
        if _validate_parts(binding, parts) != manifest['entries']:
            raise EscrowError('restored component bytes or metadata differ')
        credentials_from_bytes(parts['credentials']['data'])
        try:
            raw = parts['runtime']['data']
            runtime = json.loads(raw)
            if (not isinstance(runtime, dict) or set(runtime) != {'image', 'version', 'variables'}
                    or _encoded(runtime) != raw
                    or runtime['image'] != binding['engine_image']
                    or runtime['version'] != binding['runtime_version']
                    or not isinstance(runtime['variables'], dict)
                    or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', k)
                           or not isinstance(v, str) or any(ord(c) < 32 or ord(c) == 127 for c in v)
                           for k, v in runtime['variables'].items())):
                raise ValueError('runtime binding invalid')
        except (ValueError, TypeError, UnicodeError):
            raise EscrowError('source-bound canonical restore runtime required') from None
        variables = runtime['variables']
        if (not isinstance(replay, list) or any(not isinstance(k, str) for k in replay)
                or len(replay) != len(set(replay)) or not isinstance(omit, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                       for k, v in omit.items())
                or set(replay) & set(omit) or set(replay) | set(omit) != set(variables)):
            raise EscrowError('explicit exhaustive runtime dispositions required')
        # Bootstrap password regeneration or an unreviewed alternate config root
        # would bypass the captured keystore/config. Neither may be replayed.
        if set(replay) & {'ELASTIC_PASSWORD', 'ELASTIC_PASSWORD_FILE', 'ES_PATH_CONF'}:
            raise EscrowError('captured keystore and configuration must not be replaced')
        validate_snapshot_selection(repository, snapshot, location, indices)
        if any(i.startswith('.') for i in indices):
            raise EscrowError('explicit application indices required, security uses feature state')
        if variables.get('path.repo') != location or 'path.repo' not in replay:
            raise EscrowError('captured repository path must match isolated native selection')
        self._consumers = consumer_inventory(ledger, binding, backend, consumers, indices)
        self._manifest = copy.deepcopy(manifest)
        self._variables = {k: variables[k] for k in replay}
        self._omit = copy.deepcopy(omit)
        self._selection = {'repository': repository, 'snapshot': snapshot,
                           'location': location, 'indices': list(indices)}

    @property
    def runtime(self):
        return {'image': self._manifest['binding']['engine_image'],
                'version': self._manifest['binding']['runtime_version'],
                'variables': copy.deepcopy(self._variables)}

    @property
    def selection(self):
        return copy.deepcopy(self._selection)

    def check(self, manifest, parts):
        if (manifest != self._manifest
                or _validate_parts(_binding(self._manifest['binding']), parts) != self._manifest['entries']):
            raise EscrowError('prepared restore generation changed')

    def native_request(self):
        # The application index inventory is explicit. Security comes only from
        # the captured native feature state, never a regenerated credential map.
        return {'indices': ','.join(self._selection['indices']), 'include_global_state': True,
                'feature_states': ['security']}

    def receipt(self):
        # Do not disclose runtime values, credentials, paths or consumer data.
        return {'generation': self._manifest['binding']['generation'],
                'runtime_replayed_count': len(self._variables),
                'runtime_omitted_count': len(self._omit),
                'native_index_count': len(self._selection['indices']),
                'consumer_contract_count': len(self._consumers['records']),
                'consumer_inventory_sha256': self._consumers['sha256'],
                'consumer_queries_verified': False,
                'production_acceptance': False, 'source_admission_released': False}
