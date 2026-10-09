"""Grimmory native MariaDB fixture adapter for the shared encrypted channel.

Uses only disposable original fixture credentials. Tests real app schema plus
binary/Unicode records, routines, events, views, and triggers. Exported /app/data,
/books and /bookdrop fixtures are not production NFS capture acceptance.
"""
import hashlib
import json
from pathlib import Path
import runpy
import shutil

ROOT = Path(__file__).resolve().parents[1]


def visible_state(app, docker):
    response = docker('exec', app, 'sh', '-c',
                      'wget -qO- http://127.0.0.1:6060/api/v1/healthcheck').stdout
    value = json.loads(response)
    if (set(value) != {'status', 'message', 'data', 'timestamp'} or value['status'] != 200
            or set(value['data']) != {'status', 'message', 'version', 'timestamp'}
            or value['data']['status'] != 'UP'):
        raise ValueError('unexpected native Grimmory health schema')
    # v3.5.0 HealthcheckController and SuccessResponse add request timestamps.
    # These are not stored records. Compare every other response field exactly.
    del value['timestamp']
    del value['data']['timestamp']
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fingerprint(database, sql, docker):
    data = docker('exec', database, 'bash', '-c',
        'MYSQL_PWD="$MYSQL_PASSWORD" mariadb-dump --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" '
        '--skip-comments --compact --no-create-info --skip-add-locks --skip-disable-keys '
        '--skip-extended-insert --order-by-primary --hex-blob grimmory').stdout
    return {'records_sha256': hashlib.sha256(data.encode()).hexdigest(),
            'tables': int(sql(database, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='grimmory' AND table_type='BASE TABLE';").stdout.strip())}


def contract():
    mixed = runpy.run_path(str(ROOT / 'scripts/k8s92-mixed-db-test.py'))
    docker, start, sql, wait = (mixed[name] for name in ('docker', 'start', 'sql', 'wait'))

    def cleanup():
        for name in mixed['CONTAINERS'][:]:
            mixed['remove'](name)
        for name in mixed['NETWORKS'][:]:
            docker('network', 'rm', name)
            mixed['NETWORKS'].remove(name)

    def fixture(app, export):
        try:
            mixed['grimmory'](export=export)
        finally:
            cleanup()
            shutil.rmtree(mixed['SCRATCH'])

    def restore(source, app, *, config_mib, database_mib, expected_fingerprints,
                original_api_key, expected_application_state):
        try:
            env, appenv = original_api_key['database'], original_api_key['application']
            net = mixed['network']('restore-' + source.parent.name)
            database = start('database-' + source.parent.name, mixed['MARIA'], net, env)
            wait(lambda: sql(database, 'SELECT 1;', ok=False).returncode == 0)
            docker('cp', str(source), database + ':/restore')
            docker('cp', str(ROOT / 'kubernetes/apps/media/grimmory/app/restore.sh'), database + ':/restore.sh')
            docker('exec', '-e', 'K8S92_ISOLATED_RESTORE=YES', database, 'bash', '/restore.sh', '/restore')
            if fingerprint(database, sql, docker) != expected_fingerprints:
                raise ValueError('native Grimmory table contents differ')
            if sql(database, 'SELECT HEX(body),text_value FROM lane_fixture;').stdout.strip() != '0001FEFF\tfixture-雪':
                raise ValueError('native binary/Unicode record differs')
            if sql(database, 'CALL lane_proc();').stdout.strip() != '1':
                raise ValueError('native procedure differs')
            if sql(database, 'SELECT COUNT(*) FROM lane_view;').stdout.strip() != '1':
                raise ValueError('native view differs')
            if sql(database, "SELECT COUNT(*) FROM information_schema.events WHERE event_schema='grimmory' AND event_name='lane_event';").stdout.strip() != '1':
                raise ValueError('native event differs')
            sql(database, "INSERT INTO lane_fixture VALUES(2,UNHEX('01'),'lower');")
            if sql(database, 'SELECT text_value FROM lane_fixture WHERE id=2;').stdout.strip() != 'LOWER':
                raise ValueError('native trigger differs')

            def setup(container):
                for name, target in (('app-data', '/app/data'), ('books', '/books'), ('bookdrop', '/bookdrop')):
                    docker('cp', str(source / name) + '/.', container + ':' + target)
            restored_app = start('app-' + source.parent.name, mixed['GRIMM'], 'container:' + database,
                                 appenv, extra=('--memory=1500m',), setup=setup)
            wait(lambda: docker('exec', restored_app, 'sh', '-c',
                'wget -qO- http://127.0.0.1:6060/api/v1/healthcheck >/dev/null', ok=False).returncode == 0)
            if visible_state(restored_app, docker) != expected_application_state:
                raise ValueError('native Grimmory health response differs')
            for target, expected in (('/books/fixture.txt', 'fixture-book-generation'),
                                     ('/bookdrop/fixture.txt', 'fixture-upload-generation')):
                if docker('exec', restored_app, 'cat', target).stdout != expected:
                    raise ValueError('native Grimmory mounted file differs')
            return {'app': app, 'native_restore': True, 'restored_app_ping': True,
                    'database_count': 1, 'tables': expected_fingerprints['tables'],
                    'native_table_contents_equal': True, 'original_fixture_identity_used': True,
                    'application_visible_state_equal': True,
                    'visible_state_scope': 'exact application health response and mounted book/upload fixture reads',
                    'binary_unicode_routine_event_trigger_view_equal': True,
                    'network': 'internal-no-external-egress'}
        finally:
            cleanup()
    return {'CONTRACTS': {'grimmory': (mixed['GRIMM'],)}, 'PG_IMAGE': mixed['MARIA'],
            'fixture': fixture, 'restore_pvc': restore}
