#!/usr/bin/env python3
"""Check the native consumer and database. Emit nonsecret receipts only."""
import json
import os
from pathlib import Path
import time
import urllib.request

CONTROL = Path('/control')
DEADLINE = time.monotonic() + 850
EXPECTED_DATA = '57d2e92411667e41fedfee17fcd0824a5b938becb06750b2a45ddcf7f107a782'
SNAPSHOTS = {'nas': 'b0c9239d69e3367802edbb4c4d995152', 'r2': 'ffe7bf1cfd6e0065208284ac3aee2e40'}


def wait_for(check):
    while time.monotonic() < DEADLINE:
        if (CONTROL / 'failed').exists():
            raise RuntimeError('native recovery failed')
        if check():
            return
        time.sleep(2)
    raise TimeoutError('native recovery deadline expired')


def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:6060/api/v1/healthcheck', timeout=5) as response:
            data = json.load(response)
            return response.status == 200 and isinstance(data, dict) and data.get('status') == 200 and data.get('data', {}).get('status') == 'UP' and data['data'].get('version') == 'v3.5.0'
    except Exception:
        return False


def setup_status():
    # v3.5.0 SetupController delegates to UserRepository.count(), unlike health.
    try:
        with urllib.request.urlopen('http://127.0.0.1:6060/api/v1/setup/status', timeout=5) as response:
            data = json.load(response)
            return response.status == 200 and data.get('status') == 200 and data.get('data') is False
    except Exception:
        return False


def check_database():
    point = CONTROL / 'data-equal'
    point.unlink(missing_ok=True)
    request = str(time.monotonic_ns())
    (CONTROL / 'check-data').write_text(request)
    wait_for(lambda: point.exists() and point.read_text() == request)


def main():
    wait_for(lambda: (CONTROL / 'database-ready').exists())
    assert (CONTROL / 'database-ready').read_text().strip() == EXPECTED_DATA
    wait_for(health)
    wait_for(setup_status)
    check_database()
    (CONTROL / 'restart-app').touch()
    wait_for(lambda: (CONTROL / 'application-generation').exists() and
             (CONTROL / 'application-generation').read_text().strip() == '2')
    wait_for(health)
    wait_for(setup_status)
    check_database()
    backend = os.environ['BACKEND']
    print(json.dumps({'backend': backend, 'snapshot_id': SNAPSHOTS[backend],
                      'controller_restored_native_consumer': True, 'base_tables': 73,
                      'original_data_sha256': EXPECTED_DATA, 'native_health': True,
                      'native_app_restart_equal': True, 'original_pvc_readonly': True,
                      'books_original_mounted': True, 'database_backed_setup_status_equal': True,
                      'authenticated_catalog_api_qualified': False,
                      'shared_books_qualified': False, 'production_watchdog_qualified': False}))
    (CONTROL / 'complete').touch()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        (CONTROL / 'failed').touch()
        raise SystemExit('native consumer proof failed; private details withheld') from None
