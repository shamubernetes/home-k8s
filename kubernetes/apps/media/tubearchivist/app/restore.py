#!/usr/bin/env python3
"""Restore one native TA bundle into an EMPTY isolated ES and cache only.

Run inside a disposable recovery container sharing the new ES network namespace.
This is not a production capture or an original NAS/R2 qualification receipt.
"""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import urllib.request

REQUIRED = {'db.sqlite3', 'runtime.json', 'cache-state.tar', 'metadata.json',
            'elasticsearch.ndjson', 'elasticsearch-mappings.json',
            'elasticsearch-aliases.json', 'elasticsearch-settings.json'}


def verify(bundle):
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError('invalid bundle')
    checksums = json.loads((bundle / 'checksums.json').read_text())
    if set(checksums) != REQUIRED:
        raise ValueError('incomplete bundle manifest')
    for name, digest in checksums.items():
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('invalid payload')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('bundle checksum mismatch')
    with contextlib.closing(sqlite3.connect((bundle / 'db.sqlite3').absolute().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('database integrity failed')
    escrow = json.loads((bundle / 'runtime.json').read_text())
    if set(escrow) != {'TA_USERNAME', 'TA_PASSWORD'} or not all(isinstance(v, str) and v for v in escrow.values()):
        raise ValueError('invalid runtime escrow')
    return escrow


def restore_es(bundle, request):
    mappings = json.loads((bundle / 'elasticsearch-mappings.json').read_text())
    aliases = json.loads((bundle / 'elasticsearch-aliases.json').read_text())
    settings = json.loads((bundle / 'elasticsearch-settings.json').read_text())
    if not mappings or set(mappings) != set(aliases) or set(mappings) != set(settings):
        raise ValueError('incomplete index catalog')
    if request('GET', '/_cat/indices?format=json&expand_wildcards=all'):
        raise ValueError('restore requires an empty isolated Elasticsearch')
    immutable = {'uuid', 'version', 'creation_date', 'provided_name', 'history_uuid'}
    for index, mapping in mappings.items():
        if not index.startswith('ta_') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in index):
            raise ValueError('invalid application index')
        original = settings[index]['settings']['index']
        writable = {k: v for k, v in original.items() if k not in immutable}
        request('PUT', '/' + index, {'mappings': mapping['mappings'], 'settings': {'index': writable}})
    documents = []
    with (bundle / 'elasticsearch.ndjson').open() as stream:
        while line := stream.readline():
            action = json.loads(line)
            source = stream.readline()
            if not source or set(action) != {'index'} or set(action['index']) != {'_index', '_id'}:
                raise ValueError('malformed native bulk stream')
            target = action['index']
            if target['_index'] not in mappings:
                raise ValueError('foreign bulk index')
            documents.append((target['_index'], target['_id'], json.loads(source)))
    if len({(i, key) for i, key, _ in documents}) != len(documents):
        raise ValueError('duplicate native documents')
    expected = json.loads((bundle / 'metadata.json').read_text())['documents']
    if type(expected) is not int or len(documents) != expected:
        raise ValueError('native document total mismatch')
    for offset in range(0, len(documents), 500):
        batch = documents[offset:offset + 500]
        payload = ''.join(json.dumps({'index': {'_index': i, '_id': key}}) + '\n' + json.dumps(doc) + '\n' for i, key, doc in batch)
        response = request('POST', '/_bulk?refresh=true', payload)
        items = response.get('items', [])
        if response.get('errors') is not False or len(items) != len(batch):
            raise ValueError('native bulk restore failed')
        for item, (index, key, _) in zip(items, batch):
            result = item.get('index', {})
            if result.get('status') not in (200, 201) or result.get('error') or result.get('_index') != index or result.get('_id') != key:
                raise ValueError('native bulk item failed')
    # Verify every restored source, not merely total counts or HTTP 200.
    for index, key, source in documents:
        from urllib.parse import quote
        hit = request('GET', '/' + index + '/_doc/' + quote(key, safe=''))
        if hit.get('found') is not True or hit.get('_source') != source:
            raise ValueError('native document equality failed')
    actions = [{'add': {'index': index, 'alias': alias, **definition}}
               for index in mappings for alias, definition in aliases[index].get('aliases', {}).items()]
    if actions:
        request('POST', '/_aliases', {'actions': actions})
    for index in mappings:
        restored = request('GET', '/' + index + '/_alias')
        if restored.get(index, {}).get('aliases') != aliases[index].get('aliases', {}):
            raise ValueError('native alias equality failed')
        restored = request('GET', '/' + index + '/_mapping')
        if restored.get(index, {}).get('mappings') != mappings[index]['mappings']:
            raise ValueError('native mapping equality failed')
    return {'indices': len(mappings), 'documents': len(documents), 'document_equality': True}


def restore_cache(bundle, target):
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise ValueError('restore requires an empty isolated cache')
    with tarfile.open(bundle / 'cache-state.tar') as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in {'ytdlp', 'download', 'import'}:
                raise ValueError('unsafe archive path')
            if not (member.isfile() or member.isdir()):
                raise ValueError('unsafe archive object')
        archive.extractall(target, filter='data')
    shutil.copyfile(bundle / 'db.sqlite3', target / 'db.sqlite3')
    shutil.copyfile(bundle / 'runtime.json', target / 'runtime.json')
    (target / 'runtime.json').chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated', action='store_true', required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    verify(args.bundle.absolute())
    # Fixed loopback endpoint only. No production URL, credentials or proxy path.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def request(method, path, body=None):
        raw = body.encode() if isinstance(body, str) else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request('http://127.0.0.1:9200' + path, data=raw, method=method,
                                     headers={'Content-Type': 'application/x-ndjson' if isinstance(body, str) else 'application/json'})
        with opener.open(req, timeout=30) as response:
            result = json.load(response)
        if isinstance(result, dict) and result.get('error'):
            raise ValueError('native Elasticsearch restore failed')
        return result
    result = restore_es(args.bundle, request)
    restore_cache(args.bundle, args.cache)
    print(json.dumps(result))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == '__main__':
    main()
