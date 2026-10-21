"""Portable native ES PIT export, read-only and scoped to actual Tube indices."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path('/capture/.kopiur-native-es')
BASE = os.environ.get('ES_URL', 'http://elasticsearch.database.svc.cluster.local:9200').rstrip('/')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def request(method, path, body=None):
    auth = base64.b64encode((os.environ['ES_USERNAME'] + ':' + os.environ['ES_PASSWORD']).encode()).decode()
    data = None if body is None else canonical(body).encode()
    with urlopen(Request(BASE + path, data=data, method=method,
                         headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}), timeout=120) as response:
        assert response.status == 200
        value = json.load(response)
    if isinstance(value, dict):
        assert not value.get('error')
        if '_shards' in value:
            assert value['_shards']['failed'] == 0
        assert not value.get('timed_out', False)
    return value


def metadata():
    mapping = request('GET', '/ta_*/_mapping')
    names = sorted(mapping)
    assert names and all(name.startswith('ta_') for name in names)
    joined = ','.join(quote(name, safe='') for name in names)
    return {'indices': names, 'mapping': mapping,
            'settings': request('GET', '/' + joined + '/_settings'),
            'aliases': request('GET', '/' + joined + '/_alias')}


def main():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    pending = ROOT / 'pending'
    assert not pending.is_symlink()
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(mode=0o700)
    who = request('GET', '/_security/_authenticate')
    assert who['username'] == 'kopiur_tubearchivist' and who['roles'] == ['kopiur_tubearchivist']
    source = metadata()
    names = source['indices']
    joined = ','.join(quote(name, safe='') for name in names)
    pit = request('POST', '/' + joined + '/_pit?keep_alive=5m')['id']
    counts = dict.fromkeys(names, 0)
    total = None
    after = None
    digest = hashlib.sha256()
    try:
        with (pending / 'documents.jsonl').open('wb') as stream:
            while True:
                query = {'size': 1000, 'sort': ['_shard_doc'], 'track_total_hits': True,
                         'pit': {'id': pit, 'keep_alive': '5m'}, 'query': {'match_all': {}}}
                if after is not None:
                    query['search_after'] = after
                page = request('POST', '/_search', query)
                pit = page.get('pit_id', pit)
                hits = page['hits']
                assert hits['total']['relation'] == 'eq'
                if total is None:
                    total = hits['total']['value']
                assert hits['total']['value'] == total
                for hit in hits['hits']:
                    assert hit['_index'] in counts and '_source' in hit
                    row = {key: hit[key] for key in ['_index', '_id', '_source']}
                    if '_routing' in hit:
                        row['_routing'] = hit['_routing']
                    encoded = (canonical(row) + '\n').encode()
                    stream.write(encoded)
                    digest.update(encoded)
                    counts[hit['_index']] += 1
                if not hits['hits']:
                    break
                after = hits['hits'][-1]['sort']
            stream.flush()
            os.fsync(stream.fileno())
        assert sum(counts.values()) == total
        # Only actual schema/alias inputs must be stable, never a global document
        # writer quiet period. PIT preserves the original complete source rows.
        assert metadata() == source
        (pending / 'indices.json').write_text(canonical(source))
        receipt = {'format': 'tubearchivist-native-es-pit-v1', 'version': request('GET', '/')['version']['number'],
                   'indices': names, 'document_counts': counts, 'documents': total,
                   'documents_sha256': digest.hexdigest(), 'completed': time.time(),
                   'capture_user': who['username'], 'source_readonly': True,
                   'all_shard_reads_verified': True, 'production_writer_held': False}
        (pending / 'capture.json').write_text(canonical(receipt))
        previous = ROOT / 'previous'
        current = ROOT / 'current'
        if previous.exists():
            shutil.rmtree(previous)
        if current.exists():
            current.rename(previous)
        pending.rename(current)
        print(json.dumps({'native_pit_capture_complete': True, 'index_count': len(names),
                          'documents': total, 'source_readonly': True}), flush=True)
    finally:
        request('DELETE', '/_pit', {'id': pit})


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__) if frame.filename == __file__]
        print(json.dumps({'native_capture_failed': type(error).__name__, 'source_lines': lines,
                          'private_details_withheld': True}), flush=True)
        raise SystemExit(1) from None
