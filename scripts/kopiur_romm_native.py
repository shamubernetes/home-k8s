"""RomM user/JWT/native-tree fixture, not production ROM/NFS/Dragonfly acceptance."""
import hashlib
from http.cookies import SimpleCookie
import json
import time
import kopiur_pocket_id_native as pocket

IMAGE = 'ghcr.io/rommapp/romm:5.3.1@sha256:0d66b4ea152a237c7f24b95e87459f46d3c596a7c0b35a82f26bc971487278b3'


def stable_users(value):
    if not isinstance(value, list) or len(value) != 1 or value[0].get('username') != 'recovery-fixture':
        raise ValueError('native fixture user catalog changed')
    fields = ('id', 'username', 'email', 'enabled', 'role', 'oauth_scopes')
    result = [{key: user[key] for key in fields} for user in value]
    for user in result:
        user['oauth_scopes'] = sorted(user['oauth_scopes'])
    return result


def contract():
    scope = pocket.contract()
    scope['CONTRACTS']['romm'] = (IMAGE, ['romm'], '', 8080, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class RomMDrill(base):
        def auth_header(self):
            return 'Authorization: Bearer ' + self.api_key

        def request(self, container, path, payload=None, authenticated=False, check=True, form=False):
            extra_headers = ()
            if payload is not None and not authenticated and getattr(self, 'csrf_token', None):
                extra_headers = ('Cookie: csrftoken=' + self.csrf_token, 'x-csrftoken: ' + self.csrf_token)
            return super().request(container, path, payload, authenticated, check, form, extra_headers)

        def bootstrap_csrf(self, container):
            config = 'url = "http://127.0.0.1:8080/api/heartbeat"\ndump-header = "/dev/stdout"\noutput = "/dev/null"\n'
            raw = scope['run']('docker', 'run', '--rm', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges:true', '--network', 'container:' + container,
                '--entrypoint', 'curl', '-i', pocket.HTTP_IMAGE, '-fsS', '--max-time', '20',
                '--config', '-', stdin=config.encode()).stdout
            if len(raw) > 65536:
                raise ValueError('native CSRF headers exceed bound')
            for line in raw.decode('latin-1').splitlines():
                if line.lower().startswith('set-cookie:'):
                    cookie = SimpleCookie()
                    cookie.load(line.split(':', 1)[1].strip())
                    if 'csrftoken' in cookie:
                        self.csrf_token = cookie['csrftoken'].value
                        return
            raise ValueError('native CSRF cookie is missing')

        def isolated_config(self, raw):
            if len(raw) > 65536:
                raise ValueError('fixture identity exceeds bound')
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'original_fixture_key', 'auth_secret'}:
                raise ValueError('invalid fixture identity fields')
            if any(not isinstance(value[key], str) or len(value[key]) < 16 for key in value):
                raise ValueError('invalid original fixture identity')
            return raw

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
                identity = {'original_fixture_key': self.api_key, 'auth_secret': self.api_key}
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-ec',
                    'umask 077; mkdir -p /config/config /config/assets /config/resources /config/library; '
                    'cat > /config/fixture-identity.json; '
                    'printf native-filetree-fixture > /config/library/fixture.bin',
                    stdin=json.dumps(identity).encode())
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            identity = json.loads(self.isolated_config(raw))
            self.api_key, self.auth_secret = identity['original_fixture_key'], identity['auth_secret']
            env = {'ROMM_DB_DRIVER': 'postgresql', 'DB_HOST': '127.0.0.1', 'DB_PORT': '5432',
                'DB_USER': 'app', 'DB_PASSWD': self.password, 'DB_NAME': 'romm',
                'ROMM_AUTH_SECRET_KEY': self.auth_secret, 'ROMM_PORT': '8080', 'ROMM_BASE_PATH': '/romm',
                'REDIS_HOST': '', 'REDIS_DB': '12', 'WEB_SERVER_CONCURRENCY': '1',
                'ENABLE_SCHEDULED_RESCAN': 'false', 'ENABLE_RESCAN_ON_FILESYSTEM_CHANGE': 'false',
                'ENABLE_SCHEDULED_UPDATE_SWITCH_TITLEDB': 'false',
                'ENABLE_SCHEDULED_UPDATE_LAUNCHBOX_METADATA': 'false', 'ENABLE_SYNC_FOLDER_WATCHER': 'false',
                'HASHEOUS_API_ENABLED': 'false', 'HLTB_API_ENABLED': 'false',
                'OTEL_SDK_DISABLED': 'true', 'LOGLEVEL': 'WARNING', 'TZ': 'UTC'}
            # The production image keeps executable code under /backend. One
            # captured volume supplies /romm's configuration/assets/library and
            # the disposable bundled Valkey state. Nginx config is generated in
            # the same captured tree. This does not bind any production store.
            mounts = [(config, path, 'rw') for path in ('/config', '/romm', '/redis-data', '/etc/nginx/conf.d')]
            return self.start(name, self.image, network='container:' + database, env=env, mounts=mounts, user='568:568')

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(180):
                if self.request(container, '/api/heartbeat', check=False).returncode == 0:
                    break
                if scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip() != b'true':
                    raise startup_failure(scope, self, container, 'isolated RomM exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'RomM readiness deadline')
            if container.endswith('-source-app'):
                self.bootstrap_csrf(container)
                password = 'Fixture1!' + self.password
                self.request(container, '/api/users', {'username': 'recovery-fixture',
                    'email': 'recovery-fixture@example.invalid', 'password': password, 'role': 'admin'})
                token = json.loads(self.request(container, '/api/token', {'grant_type': 'password',
                    'username': 'recovery-fixture', 'password': password, 'scope': 'users.read'}, form=True).stdout)
                self.api_key = token['access_token']
                identity = {'original_fixture_key': self.api_key, 'auth_secret': self.auth_secret}
                scope['run']('docker', 'exec', '-i', container, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=json.dumps(identity).encode())

        def application_state(self, container):
            value = json.loads(self.request(container, '/api/users', authenticated=True).stdout)
            return hashlib.sha256(json.dumps(stable_users(value), sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = RomMDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'romm', [IMAGE, pocket.HTTP_IMAGE], {
        'production_rom_catalog_and_shared_nfs_qualified': False,
        'production_dragonfly_session_recovery_qualified': False})
