#!/usr/bin/env python3
"""Publish one immutable closed generation, never a later raw filesystem copy."""
import json
from pathlib import Path
import sys
import uuid

import backup
import coordination as gate


if __name__ == '__main__':
    root = Path('/cache/kopiur')
    destination = root / 'generations' / uuid.uuid4().hex
    state = gate.coordinate(destination, [sys.executable, str(Path(__file__).with_name('backup.py')),
                                          'collect-coherent'])
    # If pointer publication fails, writers are already resumed. No failed hook
    # silently leaves a previous pointer looking like this attempt succeeded.
    manifest = backup.verify(destination)
    gate.atomic(root / 'LATEST.json', {
        'format': 'coherent-generation-pointer-v1', 'generation': state['generation'],
        'relative_path': str(destination.relative_to(root)),
        'manifest_sha256': backup.digest(destination / 'manifest.json'),
    })
    print(json.dumps({'generation': manifest['generation'], 'phase': state['phase']}))
