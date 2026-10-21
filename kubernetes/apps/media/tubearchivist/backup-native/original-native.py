"""Import both retained originals into isolated exact-version Elasticsearch."""
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def request(method, path, value=None, ndjson=None):
    body = ndjson if ndjson is not None else (canonical(value).encode() if value is not None else None)
    with urlopen(Request('http://127.0.0.1:9200' + path, data=body, method=method,
                         headers={'Content-Type': 'application/x-ndjson' if ndjson else 'application/json'}),
                 timeout=120) as response:
        result = json.load(response)
    assert not result.get('errors', False) and not result.get('error')
    assert result.get('_shards', {}).get('failed', 0) == 0
    return result


def verify(root, prefix):
    capture = root / '.kopiur-native-es/current'
    receipt = json.loads((capture / 'capture.json').read_text())
    data = (capture / 'documents.jsonl').read_bytes()
    assert hashlib.sha256(data).hexdigest() == receipt['documents_sha256']
    metadata = json.loads((capture / 'indices.json').read_text())
    rows = [json.loads(row) for row in data.splitlines()]
    assert len(rows) == receipt['documents']
    assert receipt['version'] == request('GET', '/')['version']['number']
    counts = {}
    for name in metadata['indices']:
        target = prefix + name
        settings = metadata['settings'][name]['settings']['index']
        # Identity, history and allocation settings belong to the original cluster.
        settings = {key: value for key, value in settings.items()
                    if key not in {'uuid', 'version', 'creation_date', 'provided_name', 'routing', 'lifecycle'}
                    and not key.startswith('resize')}
        settings['number_of_replicas'] = '0'
        request('PUT', '/' + target, {'settings': {'index': settings},
                                    'mappings': metadata['mapping'][name]['mappings']})
        selected = [row for row in rows if row['_index'] == name]
        counts[name] = len(selected)
        if selected:
            bulk = []
            for row in selected:
                action = {'_index': target, '_id': row['_id']}
                if '_routing' in row:
                    action['routing'] = row['_routing']
                bulk.extend([canonical({'index': action}), canonical(row['_source'])])
            request('POST', '/_bulk?refresh=true', ndjson=('\n'.join(bulk) + '\n').encode())
        observed = request('POST', '/' + target + '/_search',
                           {'size': max(1, len(selected)), 'track_total_hits': True, 'query': {'match_all': {}}})
        assert observed['hits']['total'] == {'value': len(selected), 'relation': 'eq'}
        expected = {row['_id']: canonical(row['_source']) for row in selected}
        actual = {row['_id']: canonical(row['_source']) for row in observed['hits']['hits']}
        assert actual == expected
    assert counts == receipt['document_counts']
    return {'indices': len(counts), 'documents': len(rows), 'digest': receipt['documents_sha256']}


def main():
    for attempt in range(120):
        try:
            request('GET', '/')
            break
        except OSError:
            time.sleep(2)
    else:
        raise TimeoutError('isolated Elasticsearch startup')
    results = [verify(Path('/original/' + side), side + '-') for side in ['nas', 'r2']]
    assert results[0] == results[1]
    print(json.dumps({'original_native_import_passed': True, 'stores': ['nas', 'r2'],
                      'index_count': results[0]['indices'], 'documents': results[0]['documents'],
                      'all_source_rows_equal': True, 'production_access': False}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        print(json.dumps({'native_original_failed': type(error).__name__,
                          'lines': [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                                    if frame.filename == __file__], 'private_details_withheld': True}), flush=True)
        raise SystemExit(1) from None
