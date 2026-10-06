"""Convex native document/admin-key fixture, not production RGW/deployment acceptance."""
import hashlib
import json
import re
import time
import kopiur_pocket_id_native as pocket

IMAGE = 'ghcr.io/get-convex/convex-backend:latest@sha256:1f2044e3eac463ac78973b136c0baf72d4ada602611d853d6f99f280e29e0a98'
TABLE = 'recovery_fixture'


def stable_snapshot(value):
    if value.get('hasMore') is not False:
        raise ValueError('native snapshot is incomplete')
    rows = value.get('values')
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('native fixture document count changed')
    if rows[0].get('_table') != TABLE or rows[0].get('value') != 'native-recovery-proof':
        raise ValueError('native fixture document changed')
    if not rows[0].get('_id'):
        raise ValueError('native document identity is missing')
    return rows


def contract():
    scope = pocket.contract()
    scope['CONTRACTS']['convex'] = (IMAGE, ['convex'], '', 3210, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class ConvexDrill(base):
        def auth_header(self):
            return 'Authorization: Convex ' + self.api_key

        def isolated_config(self, raw):
            if len(raw) > 65536:
                raise ValueError('fixture identity exceeds bound')
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'original_fixture_key', 'auth_secret'}:
                raise ValueError('invalid fixture identity fields')
            if not isinstance(value['original_fixture_key'], str) or len(value['original_fixture_key']) < 16:
                raise ValueError('invalid original fixture key')
            if not isinstance(value['auth_secret'], str) or not re.fullmatch('[a-f0-9]{64}', value['auth_secret']):
                raise ValueError('invalid original instance secret')
            return raw

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
                identity = {'original_fixture_key': self.api_key,
                    'auth_secret': hashlib.sha256(self.api_key.encode()).hexdigest()}
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=json.dumps(identity).encode())
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            identity = json.loads(self.isolated_config(raw))
            self.api_key = identity['original_fixture_key']
            self.auth_secret = identity['auth_secret']
            env = {'POSTGRES_URL': 'postgresql://app:' + self.password + '@127.0.0.1:5432/convex?sslmode=disable',
                'INSTANCE_NAME': 'convex', 'INSTANCE_SECRET': self.auth_secret,
                'CONVEX_CLOUD_ORIGIN': 'http://127.0.0.1:3210',
                'CONVEX_SITE_ORIGIN': 'http://127.0.0.1:3211',
                'DO_NOT_REQUIRE_SSL': '1', 'DISABLE_BEACON': 'true', 'RUST_LOG': 'info'}
            # The pinned image has root-owned mode-744 entrypoint/key scripts and no USER override.
            scope['run']('docker', 'run', '--rm', '--read-only', '--network', 'none',
                '--cap-drop=ALL', '--cap-add=CHOWN', '--security-opt=no-new-privileges:true',
                '--user', '0:0', '--entrypoint', 'sh',
                '--mount', 'type=volume,src=' + config + ',dst=/config', scope['PG_IMAGE'],
                '-ceu', 'chown 0:0 /config; chown -R 0:0 /config; chmod 0775 /config')
            try:
                return self.start(name, self.image, network='container:' + database, env=env,
                    mounts=[(config, '/config', 'rw'), (config, '/convex/data', 'rw')], user='0:0')
            except RuntimeError as error:
                user = scope['run']('docker', 'inspect', '--format', '{{.Config.User}}', IMAGE, check=False).stdout.decode().strip()
                if not re.fullmatch(r'[A-Za-z0-9_:-]{0,128}', user):
                    user = 'unavailable'
                modes = scope['run']('docker', 'run', '--rm', '--read-only', '--network', 'none',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--user', '0:0',
                    '--entrypoint', 'stat', IMAGE, '-c', '%a %u %g %n',
                    '/convex', '/convex/run_backend.sh', '/convex/generate_admin_key.sh', check=False).stdout.decode()[:2048]
                raise RuntimeError(str(error) + '; image user=' + user + '; fixed-path modes=' + modes.strip()) from error

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(180):
                if self.request(container, '/version', check=False).returncode == 0:
                    break
                if scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip() != b'true':
                    raise startup_failure(scope, self, container, 'isolated Convex exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'Convex readiness deadline')
            if container.endswith('-source-app'):
                # Generate the original fixture key with the pinned native tool.
                # Never regenerate it in restored/exported processes.
                key = scope['run']('docker', 'exec', container, './generate_admin_key.sh').stdout.decode().strip()
                if not key or len(key) > 65536:
                    raise ValueError('native admin key exceeds bound or is empty')
                self.api_key = key
                identity = {'original_fixture_key': self.api_key, 'auth_secret': self.auth_secret}
                scope['run']('docker', 'exec', '-i', container, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=json.dumps(identity).encode())
                result = json.loads(self.request(container,
                    '/api/import?tableName=' + TABLE + '&format=jsonArray',
                    [{'value': 'native-recovery-proof'}], authenticated=True).stdout)
                if result.get('numWritten') != 1:
                    raise ValueError('native import did not write exactly one document')
            self.application_state(container)

        def application_state(self, container):
            value = json.loads(self.request(container, '/api/list_snapshot?tableName=' + TABLE,
                authenticated=True).stdout)
            return hashlib.sha256(json.dumps(stable_snapshot(value), sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = ConvexDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'convex', [IMAGE, pocket.HTTP_IMAGE], {
        'production_database_binding_qualified': False,
        'production_rgw_bucket_set_qualified': False,
        'deployed_functions_and_search_rebuild_qualified': False})
