#!/usr/bin/env python3
"""Prepare complete original Chaptarr trees for isolated PostgreSQL replay."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

from original_sqlite import inventory, require


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    originals = {b: Path('/original') / b for b in ('nas', 'r2')}
    before = {b: inventory(p) for b, p in originals.items()}
    require(before['nas'] and before['nas'] == before['r2'])
    for backend, source in originals.items():
        target = Path('/work') / backend
        require(not target.exists())
        shutil.copytree(source, target)
        # This is the retained application config, not XML from a network peer.
        config_bytes = (target / 'config.xml').read_bytes()
        require(len(config_bytes) < 1024 * 1024 and b'<!DOCTYPE' not in config_bytes)
        config = ET.fromstring(config_bytes)
        require(config.tag == 'Config' and config.findtext('ApiKey'))
        base = target / '.kopiur-chaptarr'
        generation = (base / 'current').read_text().strip()
        require(re.fullmatch('[0-9a-f-]{36}', generation))
        folder = base / generation
        expected_names = {name + '.dump' for name in ('chaptarr_main', 'chaptarr_log', 'chaptarr_cache')}
        checksums = {}
        for line in (folder / 'SHA256SUMS').read_text().splitlines():
            checksum, filename = line.split(maxsplit=1)
            filename = filename.removeprefix('./')
            require(filename in expected_names and re.fullmatch('[0-9a-f]{64}', checksum))
            require(filename not in checksums)
            checksums[filename] = checksum
        require(set(checksums) == expected_names)
        for filename, checksum in checksums.items():
            artifact = folder / filename
            require(artifact.is_file() and not artifact.is_symlink())
            with artifact.open('rb') as stream:
                require(hashlib.file_digest(stream, 'sha256').hexdigest() == checksum)
        (Path('/work') / (backend + '-generation')).write_text(generation)
        require(inventory(source) == before[backend])
    receipt = {'app': 'chaptarr', 'original_points': json.loads(os.environ['ORIGINAL_POINTS']),
               'full_original_tree_equal': True, 'original_config_and_key_preserved': True,
               'all_three_native_archives_validated': True, 'originals_unchanged': True,
               'original_tree_sha256': hashlib.sha256(json.dumps(before['nas'], sort_keys=True).encode()).hexdigest()}
    Path('/work/source-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
    for base, dirs, files in os.walk('/work'):
        for name in dirs + files:
            os.chown(Path(base) / name, 999, 999)
    os.chown('/work', 999, 999)
    os.chmod('/work', 0o700)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except Exception as error:
        import traceback
        lines = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename == __file__]
        print(json.dumps({'failed': True, 'class': type(error).__name__, 'source_lines': lines}), flush=True)
        sys.exit(1)
