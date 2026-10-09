"""Guarded Elasticsearch source catalogs and isolated native-restore comparison.

Read only explicitly selected indices through the owned engine's loopback. These
catalogs bind supplied ledger coverage and fresh metadata/counts, not production
writer cessation, document equality or application credential reconciliation.
"""
import copy
import re

from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_restore import consumer_inventory
from kopiur_elasticsearch_capture import validate_snapshot_selection
from kopiur_elasticsearch_source import LoopbackSnapshotIO


def qualified_indices(indices, label):
    validate_snapshot_selection('catalog', 'generation',
                                '/usr/share/elasticsearch/data/snapshot', indices)
    if any(index.startswith('.') for index in indices):
        raise EscrowError(label + ' must name exact application indices')
    return copy.deepcopy(indices)


SCHEMA = 'k8s92-elasticsearch-source-catalog/v1'
IDENTITY = frozenset({'generation', 'source_uid', 'source_pod_uid',
                      'engine_image', 'runtime_version'})


def _identity(binding):
    return {key: binding[key] for key in IDENTITY}


def validate_catalog(catalog, binding):
    if (not isinstance(catalog, dict)
            or set(catalog) != {'schema', 'source', 'ledger_sha256', 'consumers', 'indices'}
            or catalog['schema'] != SCHEMA or catalog['source'] != _identity(binding)
            or not isinstance(catalog['ledger_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', catalog['ledger_sha256'])):
        raise EscrowError('source catalog binding invalid')
    indices = catalog['indices']
    if not isinstance(indices, dict) or not indices:
        raise EscrowError('source catalog index coverage absent')
    qualified_indices(list(indices), 'source catalog indices')
    consumers = catalog['consumers']
    if not isinstance(consumers, list) or not consumers:
        raise EscrowError('source catalog consumer coverage absent')
    pairs, covered = set(), set()
    for item in consumers:
        if (not isinstance(item, dict) or set(item) != {'application', 'store', 'indices'}
                or any(not isinstance(item[k], str) or not item[k] for k in ('application', 'store'))):
            raise EscrowError('source catalog consumer invalid')
        selected = qualified_indices(item['indices'], 'source catalog consumer indices')
        pair = (item['application'], item['store'])
        if pair in pairs or not set(selected) <= set(indices):
            raise EscrowError('source catalog consumer coverage differs')
        pairs.add(pair)
        covered.update(selected)
    if covered != set(indices):
        raise EscrowError('source catalog has unowned index coverage')
    for name, item in indices.items():
        if (not isinstance(item, dict) or set(item) != {'settings', 'mappings', 'aliases', 'count'}
                or not isinstance(item['settings'], dict)
                or not isinstance(item['settings'].get('index.uuid'), str)
                or not item['settings']['index.uuid']
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in item['settings'].items())
                or any(not isinstance(item[k], dict) for k in ('mappings', 'aliases'))
                or type(item['count']) is not int or item['count'] < 0):
            raise EscrowError('source catalog index metadata invalid')
    # Reject non-JSON metadata and NaN, never include returned document values in errors.
    try:
        _encoded(catalog)
    except (ValueError, TypeError):
        raise EscrowError('source catalog encoding invalid') from None
    return copy.deepcopy(catalog)


class LoopbackCatalogIO(LoopbackSnapshotIO):
    """Use the existing credential-safe transport with exact read paths only."""
    def __init__(self, *, exec_read, indices):
        selected = qualified_indices(indices, 'catalog loopback indices')
        # No repository/archive operation is added to this read capability.
        self.exec_read = exec_read
        self.paths = frozenset('/' + index + suffix for index in selected
            for suffix in ('/_settings?flat_settings=true', '/_mapping', '/_alias', '/_count'))

    def read_archive(self, location):
        raise EscrowError('catalog capability cannot read archives')


class SourceCatalog:
    def __init__(self, binding, *, ledger, backend, consumer_indices, indices, guard, request):
        self.binding = copy.deepcopy(binding)
        self.guard, self.request = guard, request
        self.consumers = consumer_inventory(ledger, binding, backend,
                                            consumer_indices, indices)['records']
        self.indices = qualified_indices(indices, 'source catalog indices')
        self.ledger_sha256 = _digest(_encoded(ledger))

    def _read(self, path, credentials):
        if self.guard() is not True:
            raise EscrowError('affirmative catalog authority required')
        try:
            result = self.request(path, copy.deepcopy(credentials))
        except EscrowError:
            raise
        except Exception:
            raise EscrowError('authenticated source catalog read failed') from None
        if self.guard() is not True:
            raise EscrowError('affirmative catalog authority required')
        return result

    def observe(self, credentials):
        records = {}
        for index in self.indices:
            record = {}
            for key, suffix in (('settings', '/_settings?flat_settings=true'),
                                ('mappings', '/_mapping'), ('aliases', '/_alias')):
                response = self._read('/' + index + suffix, credentials)
                if (not isinstance(response, dict) or set(response) != {index}
                        or not isinstance(response[index], dict)
                        or set(response[index]) != {key}
                        or not isinstance(response[index][key], dict)):
                    raise EscrowError('exact source catalog index response required')
                record[key] = copy.deepcopy(response[index][key])
            count = self._read('/' + index + '/_count', credentials)
            shards = count.get('_shards') if isinstance(count, dict) else None
            if (not isinstance(shards, dict) or shards.get('failed') != 0
                    or type(shards.get('failed')) is not int
                    or type(shards.get('total')) is not int or shards['total'] <= 0
                    or type(shards.get('successful')) is not int
                    or shards['successful'] != shards['total']
                    or type(count.get('count')) is not int or count['count'] < 0):
                raise EscrowError('complete source catalog count required')
            record['count'] = count['count']
            records[index] = record
        return validate_catalog({'schema': SCHEMA, 'source': _identity(self.binding),
            'ledger_sha256': self.ledger_sha256, 'consumers': self.consumers,
            'indices': records}, self.binding)

    def capture(self, credentials):
        first = self.observe(credentials)
        if _encoded(self.observe(credentials)) != _encoded(first):
            raise EscrowError('source catalog changed during capture')
        return first

    def verify_restore(self, catalog, credentials):
        expected = validate_catalog(catalog, self.binding)
        if (expected['ledger_sha256'] != self.ledger_sha256
                or expected['consumers'] != self.consumers
                or set(expected['indices']) != set(self.indices)):
            raise EscrowError('restore catalog ledger coverage differs')
        actual = self.capture(credentials)
        # Elasticsearch assigns a new internal UUID on native restore. Preserve
        # every other setting, including creation timestamps and admission blocks.
        for inventory in (expected, actual):
            for item in inventory['indices'].values():
                item['settings'].pop('index.uuid')
        if _encoded(actual) != _encoded(expected):
            raise EscrowError('native restore source catalog differs')
        return {'source_catalog_sha256': _digest(_encoded(catalog)),
                'index_count': len(self.indices), 'consumer_store_count': len(self.consumers),
                'fresh_counts_metadata_verified': True,
                'production_recovery_accepted': False}
