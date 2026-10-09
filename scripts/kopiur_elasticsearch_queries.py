"""Explicit read-only consumer queries bound to a guarded source catalog.

This compares complete bounded query results, not arbitrary Elasticsearch DSL or
production application authentication. Query bodies stay in authorized memory and
stdin; escrow bindings and receipts contain only query/result hashes and counts.
Restore callers must supply the same explicitly reviewed query bodies.
"""
import copy
import re

from kopiur_elasticsearch_catalog import SourceCatalog, qualified_indices, validate_catalog
from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_source import LoopbackSnapshotIO

SCHEMA = 'k8s92-elasticsearch-consumer-queries/v1'
FIELD = r'[A-Za-z][A-Za-z0-9_.]*'


def validate_body(body):
    # This limit bounds the verifier's memory/I/O, not a production recovery or
    # retention policy. Larger contracts need an independently reviewed pager.
    if (not isinstance(body, dict)
            or set(body) != {'query', 'sort', 'size', 'track_total_hits', '_source'}
            or type(body['size']) is not int or not 1 <= body['size'] <= 1000
            or body['track_total_hits'] is not True or body['_source'] is not True
            or not isinstance(body['sort'], list) or not body['sort']):
        raise EscrowError('explicit bounded complete query required')
    fields = set()
    for item in body['sort']:
        if not isinstance(item, dict) or len(item) != 1:
            raise EscrowError('explicit deterministic query sort required')
        field, direction = next(iter(item.items()))
        if (not isinstance(field, str) or not re.fullmatch(FIELD, field)
                or direction not in ('asc', 'desc') or field in fields):
            raise EscrowError('explicit deterministic query sort required')
        fields.add(field)
    query = body['query']
    if not isinstance(query, dict) or len(query) != 1:
        raise EscrowError('reviewed read-only query required')
    kind, spec = next(iter(query.items()))
    if kind == 'match_all' and spec == {}:
        pass
    elif kind == 'term' and isinstance(spec, dict) and len(spec) == 1:
        field, value = next(iter(spec.items()))
        if (not isinstance(field, str) or not re.fullmatch(FIELD, field)
                or type(value) not in (str, int, bool) or isinstance(value, str) and not value):
            raise EscrowError('explicit scalar term query required')
    else:
        raise EscrowError('reviewed read-only query required')
    try:
        _encoded(body)
    except (TypeError, ValueError):
        raise EscrowError('query encoding invalid') from None
    return copy.deepcopy(body)


def query_records(records, catalog, *, hashed=False):
    if not isinstance(records, list) or not records:
        raise EscrowError('complete per-consumer query roster required')
    expected = {(r['application'], r['store']): set(r['indices']) for r in catalog['consumers']}
    observed, ordered = {}, []
    for record in records:
        if (not isinstance(record, dict) or set(record) != {'application', 'store', 'queries'}
                or any(not isinstance(record[k], str) for k in ('application', 'store'))
                or not isinstance(record['queries'], list) or not record['queries']):
            raise EscrowError('explicit consumer query record required')
        pair = record['application'], record['store']
        if pair not in expected or pair in observed:
            raise EscrowError('consumer query roster differs from catalog')
        ids, indices = set(), set()
        for query in record['queries']:
            field = 'body_sha256' if hashed else 'body'
            if (not isinstance(query, dict) or set(query) != {'id', 'index', field}
                    or not isinstance(query['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', query['id'])
                    or query['id'] in ids or not isinstance(query['index'], str)
                    or query['index'] not in expected[pair]):
                raise EscrowError('exact per-consumer query coverage required')
            if hashed:
                if not isinstance(query[field], str) or not re.fullmatch('[0-9a-f]{64}', query[field]):
                    raise EscrowError('explicit query body digest required')
            else:
                validate_body(query[field])
            ids.add(query['id'])
            indices.add(query['index'])
        if indices != expected[pair]:
            raise EscrowError('exact per-consumer query coverage required')
        observed[pair] = indices
        value = copy.deepcopy(record)
        value['queries'].sort(key=lambda q: q['id'])
        ordered.append(value)
    if set(observed) != set(expected):
        raise EscrowError('consumer query roster differs from catalog')
    return sorted(ordered, key=lambda r: (r['application'], r['store']))


def contract_digests(records):
    return [{k: r[k] for k in ('application', 'store')} |
        {'queries': [{k: q[k] for k in ('id', 'index')} | {'body_sha256': _digest(_encoded(q['body']))}
                     for q in r['queries']]} for r in records]


def _ordered(left, right, directions):
    for a, b, direction in zip(left, right, directions):
        if type(a) is not type(b) and not (type(a) in (int, float) and type(b) in (int, float)):
            raise ValueError('incompatible sort types')
        if a != b:
            return a < b if direction == 'asc' else a > b
    return False


def result_digest(response, index, body):
    try:
        shards, hits = response['_shards'], response['hits']
        total, values = hits['total'], hits['hits']
        if (response['timed_out'] is not False or response.get('terminated_early', False) is not False
                or type(shards['total']) is not int or shards['total'] <= 0
                or type(shards['failed']) is not int or shards['failed'] != 0
                or type(shards['successful']) is not int or shards['successful'] != shards['total']
                or not isinstance(total, dict) or set(total) != {'value', 'relation'}
                or total['relation'] != 'eq' or type(total['value']) is not int
                or not 0 <= total['value'] <= body['size']
                or not isinstance(values, list) or len(values) != total['value']):
            raise ValueError('complete result required')
        docs, ids, sorts = [], set(), set()
        previous = None
        directions = [next(iter(item.values())) for item in body['sort']]
        for hit in values:
            if (not isinstance(hit, dict) or hit['_index'] != index
                    or not isinstance(hit['_id'], str) or not hit['_id'] or hit['_id'] in ids
                    or not isinstance(hit['_source'], dict)
                    or not isinstance(hit['sort'], list) or len(hit['sort']) != len(body['sort'])
                    or any(type(v) not in (str, int, float, bool) for v in hit['sort'])):
                raise ValueError('exact result required')
            order = _encoded(hit['sort'])
            if order in sorts:
                raise ValueError('nonunique query order')
            if previous is not None and not _ordered(previous, hit['sort'], directions):
                raise ValueError('incorrect query order')
            previous = hit['sort']
            ids.add(hit['_id'])
            sorts.add(order)
            docs.append({'id': hit['_id'], 'source': hit['_source'], 'sort': hit['sort']})
        return {'sha256': _digest(_encoded(docs)), 'count': total['value']}
    except (KeyError, TypeError, ValueError):
        raise EscrowError('complete exact-index uniquely ordered query results required') from None


def validate_queries(value, binding):
    catalog = validate_catalog(binding.get('source_catalog'), binding)
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'catalog_sha256', 'contracts', 'results'}
            or value['schema'] != SCHEMA or value['catalog_sha256'] != _digest(_encoded(catalog))):
        raise EscrowError('consumer query catalog binding invalid')
    contracts = query_records(value['contracts'], catalog, hashed=True)
    keys = {(r['application'], r['store'], q['id']) for r in contracts for q in r['queries']}
    if not isinstance(value['results'], list):
        raise EscrowError('complete consumer query results required')
    actual = set()
    for result in value['results']:
        if (not isinstance(result, dict)
                or set(result) != {'application', 'store', 'id', 'sha256', 'count'}
                or any(not isinstance(result[k], str) for k in ('application', 'store', 'id', 'sha256'))
                or not re.fullmatch('[0-9a-f]{64}', result['sha256'])
                or type(result['count']) is not int or result['count'] < 0):
            raise EscrowError('consumer query result digest invalid')
        key = result['application'], result['store'], result['id']
        if key not in keys or key in actual:
            raise EscrowError('consumer query result coverage differs')
        actual.add(key)
    if actual != keys or _encoded(contracts) != _encoded(value['contracts']):
        raise EscrowError('consumer query result coverage differs')
    return copy.deepcopy(value)


class LoopbackQueryIO(LoopbackSnapshotIO):
    def __init__(self, *, exec_read, indices):
        self.exec_read = exec_read
        self.indices = frozenset(qualified_indices(indices, 'query loopback indices'))
        self.paths = frozenset()

    def query(self, index, body, credentials):
        if not isinstance(index, str) or index not in self.indices:
            raise EscrowError('exact prepared query index required')
        body = validate_body(body)
        return self._request('/' + index + '/_search?allow_partial_search_results=false',
                             credentials, body=body)

    def read_archive(self, location):
        raise EscrowError('query capability cannot read archives')


class ConsumerQueries:
    def __init__(self, catalog, *, contracts, query):
        if not isinstance(catalog, SourceCatalog):
            raise EscrowError('guarded source catalog required')
        self.catalog, self.query = catalog, query
        self.binding = copy.deepcopy(catalog.binding)
        self.expected = validate_catalog(self.binding.get('source_catalog'), self.binding)
        if (self.expected['ledger_sha256'] != catalog.ledger_sha256
                or _encoded(self.expected['consumers']) != _encoded(catalog.consumers)
                or set(self.expected['indices']) != set(catalog.indices)):
            raise EscrowError('query catalog differs from supplied ledger')
        self.contracts = query_records(contracts, self.expected)

    def _results(self, credentials):
        results = []
        for record in self.contracts:
            for query in record['queries']:
                if self.catalog.guard() is not True:
                    raise EscrowError('affirmative query authority required')
                try:
                    response = self.query(query['index'], copy.deepcopy(query['body']), copy.deepcopy(credentials))
                except Exception:
                    raise EscrowError('bound consumer query failed') from None
                if self.catalog.guard() is not True:
                    raise EscrowError('affirmative query authority required')
                results.append({k: record[k] for k in ('application', 'store')} |
                    {'id': query['id']} | result_digest(response, query['index'], query['body']))
        return results

    def _capture(self, credentials, *, restored):
        def verify_catalog():
            if restored:
                self.catalog.verify_restore(self.expected, credentials)
            elif _encoded(self.catalog.observe(credentials)) != _encoded(self.expected):
                raise EscrowError('source catalog changed around consumer queries')
        verify_catalog()
        results = self._results(credentials)
        if _encoded(self._results(credentials)) != _encoded(results):
            raise EscrowError('consumer query results changed during capture')
        verify_catalog()
        return validate_queries({'schema': SCHEMA, 'catalog_sha256': _digest(_encoded(self.expected)),
            'contracts': contract_digests(self.contracts), 'results': results}, self.binding)

    def capture(self, credentials):
        return self._capture(credentials, restored=False)

    def verify_restore(self, expected, credentials):
        expected = validate_queries(expected, self.binding)
        if _encoded(expected['contracts']) != _encoded(contract_digests(self.contracts)):
            raise EscrowError('prepared consumer query contracts differ')
        actual = self._capture(credentials, restored=True)
        if _encoded(actual) != _encoded(expected):
            raise EscrowError('native restore consumer query results differ')
        return {'source_queries_sha256': _digest(_encoded(expected)),
                'consumer_store_count': len(self.contracts), 'query_count': len(expected['results']),
                'complete_query_results_verified': True, 'production_application_accepted': False}
