#!/usr/bin/env python3
"""Qualify the restored native consumer under Kubernetes, not a script restart."""
import json
import os
from pathlib import Path
import time

import importlib.util
spec = importlib.util.spec_from_file_location('native', '/contract/consumer-verify.py')
assert spec is not None and spec.loader is not None
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
CONTROL = native.CONTROL


def epoch(name):
    path = CONTROL / name
    return path.read_text().strip() if path.exists() else ''


def qualify():
    native.wait_for(lambda: epoch('database-ready') == native.EXPECTED_DATA)
    native.wait_for(native.health)
    native.wait_for(native.setup_status)
    native.check_database()
    first_database = epoch('database-epoch')
    first_application = epoch('application-epoch')
    assert first_database and first_application
    (CONTROL / 'replace-database').touch()
    native.wait_for(lambda: epoch('database-epoch') not in ('', first_database) and
                    epoch('database-ready') == native.EXPECTED_DATA)
    native.wait_for(native.health)
    native.wait_for(native.setup_status)
    native.check_database()
    # Freeze the native Java process once. The real HTTP probe, not this verifier
    # or the app wrapper, must cause Kubernetes to restart its container.
    (CONTROL / 'freeze-application').touch()
    native.wait_for(lambda: (CONTROL / 'application-frozen').exists())
    native.wait_for(lambda: not native.health())
    native.wait_for(lambda: epoch('application-epoch') not in ('', first_application))
    native.wait_for(native.health)
    native.wait_for(native.setup_status)
    native.check_database()
    backend = os.environ['BACKEND']
    receipt = {'backend': backend, 'snapshot_id': native.SNAPSHOTS[backend],
               'controller_restored_native_consumer': True, 'base_tables': 73,
               'original_data_sha256': native.EXPECTED_DATA,
               'database_container_replaced_equal': True,
               'native_probe_application_replaced_equal': True,
               'database_epoch': epoch('database-epoch'),
               'application_epoch': epoch('application-epoch'),
               'native_health': True, 'original_pvc_readonly': True,
               'shared_books_qualified': False,
               'books_original_mounted': True,
               'database_backed_setup_status_equal': True,
               'authenticated_catalog_api_qualified': False,
               'production_watchdog_qualified': False}
    (CONTROL / 'controller-receipt.json').write_text(json.dumps(receipt))
    (CONTROL / 'qualification-complete').touch()
    print(json.dumps(receipt), flush=True)


def main():
    # Once per Pod only. A verifier restart must not repeat native faults.
    if (CONTROL / 'failed').exists():
        raise RuntimeError('prior recovery qualification failed')
    if not (CONTROL / 'qualification-complete').exists():
        # Exclusive, persistent within this Pod. SIGKILL/OOM cannot rerun faults.
        started = CONTROL / 'qualification-started'
        with started.open('x'):
            pass
        qualify()
    while True:
        time.sleep(30)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        (CONTROL / 'failed').touch()
        raise SystemExit('controller recovery failed; private details withheld') from None
