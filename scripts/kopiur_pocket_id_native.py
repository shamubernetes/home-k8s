"""Pocket ID users/JWKS fixture, not production passkey or RGW acceptance."""
import hashlib
import json
from pathlib import Path
import runpy
import time
from kopiur_grafana_native import HTTP_IMAGE

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/pocket-id/pocket-id:v2.17.0-distroless@sha256:b009a094716d0a21db821641a0ff41a1dbdf648924f4c1555f5f2430dffb9fd3'


def contract():
    loaded = runpy.run_path(str(ROOT / 'scripts/kopiur-postgres-drill'))
    scope = loaded['fixture'].__globals__
    scope['CONTRACTS']['pocket-id'] = (IMAGE, ['pocket_id'], '', 1411, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class PocketDrill(base):
        def request(self, container, path, payload=None, authenticated=False, check=True):
            config = 'url = "http://127.0.0.1:1411' + path + '"\n'
            if authenticated:
                config += 'header = "X-API-Key: ' + self.api_key + '"\n'
            if payload is not None:
                config += 'header = "Content-Type: application/json"\nrequest = "POST"\n'
                config += 'data = ' + json.dumps(json.dumps(payload)) + '\n'
            return scope['run']('docker', 'run', '--rm', '-i', '--read-only',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--network', 'container:' + container, '--entrypoint', 'curl', HTTP_IMAGE,
                '-fsS', '--max-time', '5', '--config', '-', stdin=config.encode(), check=check)

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
                identity = json.dumps({'original_fixture_key': self.api_key}).encode()
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=identity)
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            self.api_key = json.loads(self.isolated_config(raw))['original_fixture_key']
            env = {'DB_CONNECTION_STRING': 'postgresql://app:' + self.password + '@127.0.0.1:5432/pocket_id?sslmode=disable',
                'ENCRYPTION_KEY': self.api_key, 'STATIC_API_KEY': self.api_key,
                'APP_URL': 'http://127.0.0.1:1411', 'PORT': '1411', 'FILE_BACKEND': 'filesystem',
                'UPLOAD_PATH': '/config/uploads', 'ANALYTICS_DISABLED': 'true', 'VERSION_CHECK_DISABLED': 'true'}
            return self.start(name, self.image, network='container:' + database, env=env,
                mounts=[(config, '/config', 'rw')], user='568:568')

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(120):
                if self.request(container, '/.well-known/jwks.json', check=False).returncode == 0:
                    break
                state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                if state != b'true':
                    raise startup_failure(scope, self, container, 'isolated Pocket ID fixture exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'isolated Pocket ID readiness deadline')
            if container.endswith('-source-app'):
                self.request(container, '/api/users', {'username': 'recovery-fixture',
                    'displayName': 'Recovery fixture', 'isAdmin': False}, authenticated=True)

        def isolated_config(self, raw):
            if len(raw) > 65536:
                raise ValueError('fixture identity exceeds bound')
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'original_fixture_key'}:
                raise ValueError('invalid fixture identity')
            if not isinstance(value['original_fixture_key'], str) or len(value['original_fixture_key']) < 16:
                raise ValueError('invalid fixture key')
            return raw

        def application_state(self, container):
            users = json.loads(self.request(container, '/api/users', authenticated=True).stdout)
            if 'recovery-fixture' not in json.dumps(users):
                raise ValueError('native fixture user is missing')
            jwks = json.loads(self.request(container, '/.well-known/jwks.json').stdout)
            if not isinstance(jwks.get('keys'), list) or not jwks['keys']:
                raise ValueError('native signing-key catalog is empty')
            return hashlib.sha256(json.dumps({'users': users, 'jwks': jwks}, sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = PocketDrill
    original_restore = scope['restore_pvc']

    def restore(source, app_name, **kwargs):
        identity = json.loads((Path(source) / 'fixture-identity.json').read_bytes())
        supplied = kwargs.get('original_api_key')
        if supplied is not None and supplied != identity['original_fixture_key']:
            raise ValueError('supplied original fixture identity changed')
        return original_restore(source, app_name, **kwargs)

    scope['restore_pvc'] = restore
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'pocket-id', [IMAGE, HTTP_IMAGE], {
        'production_passkey_login_qualified': False,
        'production_rgw_uploads_qualified': False,
        'original_key_denial_qualified': False})
