"""Kaneo task/session fixture, not OIDC, Redis, RGW or bundled nginx acceptance."""
import hashlib
import json
import time
import kopiur_pocket_id_native as pocket

IMAGE = 'ghcr.io/usekaneo/kaneo:2.32.0@sha256:07eccd911182b13b4a5ac8a798a789dc100974f5e9cf2961c7dd636579a047f2'


def contract():
    scope = pocket.contract()
    scope['CONTRACTS']['kaneo'] = (IMAGE, ['kaneo'], '', 1337, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class KaneoDrill(base):
        def auth_header(self):
            return 'Authorization: Bearer ' + self.api_key

        def counts(self, container, database=None):
            database = database or self.databases[0]
            tables = self.sql(container,
                "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) FROM pg_tables "
                "WHERE schemaname IN ('public','drizzle') ORDER BY schemaname,tablename", database).splitlines()
            return {table: int(self.sql(container, 'SELECT count(*) FROM ' + table, database)) for table in tables}

        def fingerprints(self, container, database):
            return {table: hashlib.sha256(self.sql(container,
                'SELECT row_to_json(t)::text FROM ' + table + ' t ORDER BY row_to_json(t)::text',
                database).encode()).hexdigest() for table in self.counts(container, database)}

        def provision_scoped_backup(self, container, database):
            try:
                result = super().provision_scoped_backup(container, database)
            except RuntimeError:
                schemas = self.sql(container,
                    "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
                    "AND nspname <> 'information_schema' ORDER BY nspname", database)
                raise RuntimeError('isolated Kaneo grant rejected; observed schemas=' + json.dumps(schemas.splitlines())) from None
            self.sql(container, 'CREATE SCHEMA recovery_unqualified AUTHORIZATION app', database)
            try:
                rejected = False
                try:
                    super().provision_scoped_backup(container, database)
                except RuntimeError:
                    rejected = True
                if not rejected:
                    raise RuntimeError('unqualified additional schema was accepted')
            finally:
                self.sql(container, 'DROP SCHEMA recovery_unqualified', database)
            return result

        def isolated_config(self, raw):
            if len(raw) > 65536:
                raise ValueError('fixture identity exceeds bound')
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'original_fixture_key', 'auth_secret', 'task_id'}:
                raise ValueError('invalid Kaneo fixture identity')
            if any(not isinstance(value[key], str) or len(value[key]) < 16 for key in ('original_fixture_key', 'auth_secret')):
                raise ValueError('invalid Kaneo fixture secret')
            if value['task_id'] is not None and not isinstance(value['task_id'], str):
                raise ValueError('invalid fixture task identifier')
            return raw

        def write_identity(self, container, value):
            scope['run']('docker', 'exec', '-i', container, 'sh', '-c',
                'umask 077; cat > /config/fixture-identity.json', stdin=json.dumps(value).encode())

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
                self.write_identity(holder, {'original_fixture_key': self.api_key,
                    'auth_secret': self.api_key, 'task_id': None})
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            value = json.loads(self.isolated_config(raw))
            self.api_key = value['original_fixture_key']
            self.auth_secret = value['auth_secret']
            self.task_id = value['task_id']
            env = {'DATABASE_URL': 'postgresql://app:' + self.password + '@127.0.0.1:5432/kaneo?sslmode=disable',
                'AUTH_SECRET': self.auth_secret, 'KANEO_API_URL': 'http://127.0.0.1:1337/api',
                'KANEO_CLIENT_URL': 'http://127.0.0.1:1337', 'DISABLE_GUEST_ACCESS': 'true'}
            return self.start(name, self.image, network='container:' + database, env=env,
                mounts=[(config, '/config', 'rw')], user='568:568', entrypoint='node',
                command=['--enable-source-maps', '/app/apps/api/dist/index.js'])

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(120):
                if self.request(container, '/api/health', check=False).returncode == 0:
                    break
                if scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip() != b'true':
                    raise startup_failure(scope, self, container, 'isolated Kaneo API exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'isolated Kaneo readiness deadline')
            if container.endswith('-source-app'):
                user = json.loads(self.request(container, '/api/auth/sign-up/email', {
                    'name': 'Recovery fixture', 'email': 'recovery-fixture@example.invalid',
                    'password': 'Fixture1!' + self.password}).stdout)
                self.api_key = user['token']
                workspace = json.loads(self.request(container, '/api/auth/organization/create', {
                    'name': 'Recovery fixture', 'slug': 'recovery-fixture'}, authenticated=True).stdout)
                project = json.loads(self.request(container, '/api/project', {
                    'name': 'Recovery fixture', 'workspaceId': workspace['id'],
                    'icon': 'folder', 'slug': 'REC'}, authenticated=True).stdout)
                task = json.loads(self.request(container, '/api/task/' + project['id'], {
                    'title': 'Native recovery fixture', 'description': 'Original fixture state',
                    'priority': 'low', 'status': 'to-do'}, authenticated=True).stdout)
                self.task_id = task['id']
                self.write_identity(container, {'original_fixture_key': self.api_key,
                    'auth_secret': self.auth_secret, 'task_id': self.task_id})

        def application_state(self, container):
            task = json.loads(self.request(container, '/api/task/' + self.task_id, authenticated=True).stdout)
            if task.get('title') != 'Native recovery fixture' or task.get('description') != 'Original fixture state':
                raise ValueError('original Kaneo task is missing or changed')
            return hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = KaneoDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    result = exercise(contract(), 'kaneo', [IMAGE, pocket.HTTP_IMAGE], {
        'production_oidc_identity_qualified': False, 'production_redis_rgw_qualified': False,
        'bundled_nginx_entrypoint_qualified': False})
    return result | {'additional_unqualified_schema_rejected': True, 'migration_schema_contents_compared': True}
