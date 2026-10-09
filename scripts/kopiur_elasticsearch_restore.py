"""Prepare a source-bound restore without granting production execution authority.

Every runtime variable needs an explicit replay or omission decision. Preparation
performs no I/O and never changes captured bytes, keys or security identities.
The caller still owns target isolation and live admission around every operation.
"""
import copy
import json
import re

from kopiur_elasticsearch_capture import credentials_from_bytes, validate_snapshot_selection
from kopiur_elasticsearch_escrow import EscrowError, _binding, _encoded, _validate_parts


class RestorePlan:
    def __init__(self, manifest, parts, *, replay, omit, repository, snapshot, location, indices):
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
                'production_acceptance': False, 'source_admission_released': False}
