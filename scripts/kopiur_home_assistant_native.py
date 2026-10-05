"""Home Assistant PG/helper/auth fixture, not production device or HACS recovery."""
import base64
import hashlib
import json
import time

import kopiur_pocket_id_native as pocket

IMAGE = 'ghcr.io/home-operations/home-assistant:2026.8.0@sha256:99c0293b3ad103151a9d8b860232fd1c6db425eadb9eb4905c0ecbef65b27363'
APP = 'home-assistant-fixture'
CLIENT = 'http://127.0.0.1:8123/'


def contract():
    scope = pocket.contract()
    scope['CONTRACTS'][APP] = (IMAGE, ['homeassistant_fixture'], '', 8123, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class HomeAssistantDrill(base):
        def auth_header(self):
            return 'Authorization: Bearer ' + self.api_key

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
                identity = json.dumps({'original_fixture_key': self.api_key}).encode()
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=identity)
                yaml = ('homeassistant:\n  name: Recovery fixture\n  latitude: 0\n  longitude: 0\n'
                    '  elevation: 0\n  unit_system: metric\n  time_zone: UTC\ndefault_config:\n'
                    'recorder:\n  db_url: !env_var HASS_POSTGRES_URL\n  commit_interval: 1\n'
                    'input_boolean:\n  restore_fixture:\n    name: Restore fixture\n')
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                    'umask 077; cat > /config/configuration.yaml', stdin=yaml.encode())
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            self.api_key = json.loads(self.isolated_config(raw))['original_fixture_key']
            env = {'HASS_POSTGRES_URL': 'postgresql://app:' + self.password + '@127.0.0.1:5432/homeassistant_fixture',
                'TZ': 'UTC', 'VENV_FOLDER': '/tmp/fixture-venv'}
            return self.start(name, self.image, network='container:' + database, env=env,
                mounts=[(config, '/config', 'rw')], user='568:568',
                command=['--log-file', '/tmp/fixture.log'])

        def capture(self, database, config, databases, **kwargs):
            # The pinned entrypoint installs a /proc stdout link even with an
            # explicit log file. It is runtime wiring, not persistent state.
            # Remove only that exact link after the producer has stopped.
            holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
            scope['run']('docker', 'exec', holder, 'sh', '-ec',
                'if test -L /config/home-assistant.log; then '
                'test "$(readlink /config/home-assistant.log)" = /proc/self/fd/1; '
                'unlink /config/home-assistant.log; fi')
            return super().capture(database, config, databases, **kwargs)

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(180):
                if self.request(container, '/api/onboarding', check=False).returncode == 0:
                    break
                state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                if state != b'true':
                    raise startup_failure(scope, self, container, 'isolated Home Assistant exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'Home Assistant readiness deadline')
            if container.endswith('-source-app'):
                response = json.loads(self.request(container, '/api/onboarding/users', {
                    'name': 'Recovery fixture', 'username': 'recovery-fixture', 'password': self.password,
                    'client_id': CLIENT, 'language': 'en'}).stdout)
                response = json.loads(self.request(container, '/auth/token', {
                    'grant_type': 'authorization_code', 'code': response['auth_code'],
                    'client_id': CLIENT}, form=True).stdout)
                self.api_key = response['access_token']
                self.wait_for_helper(container)
                self.request(container, '/api/services/input_boolean/turn_on',
                    {'entity_id': 'input_boolean.restore_fixture'}, authenticated=True)
                identity = json.dumps({'original_fixture_key': self.api_key}).encode()
                scope['run']('docker', 'exec', '-i', container, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=identity)
                claims = json.loads(base64.urlsafe_b64decode(self.api_key.split('.')[1] + '==='))
                for _ in range(30):
                    stored = scope['run']('docker', 'exec', container, 'cat', '/config/.storage/auth', check=False)
                    if stored.returncode == 0:
                        tokens = json.loads(stored.stdout).get('data', {}).get('refresh_tokens', [])
                        if any(token.get('id') == claims.get('iss') for token in tokens):
                            break
                    time.sleep(1)
                else:
                    raise RuntimeError('original fixture session was not persisted before capture')
            self.wait_for_helper(container)

        def wait_for_helper(self, container):
            # Onboarding responds before all configured entities have loaded.
            # Wait for the helper using the unchanged original bearer token.
            for _ in range(120):
                response = self.request(container, '/api/states/input_boolean.restore_fixture',
                    authenticated=True, check=False)
                if response.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('original fixture session could not read configured helper')

        def application_state(self, container):
            value = json.loads(self.request(container, '/api/states/input_boolean.restore_fixture',
                authenticated=True).stdout)
            if value.get('entity_id') != 'input_boolean.restore_fixture' or value.get('state') != 'on':
                raise ValueError('restored helper state differs')
            # State timestamps/context are fresh runtime observations. Original
            # historical recorder values remain covered by native SQL fingerprints.
            stable = {key: value[key] for key in ('entity_id', 'state', 'attributes')}
            return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = HomeAssistantDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    result = exercise(contract(), APP, [IMAGE, pocket.HTTP_IMAGE], {
        'production_database_binding_qualified': False,
        'runtime_log_stream_recovery_qualified': False,
        'production_devices_hacs_git_qualified': False})
    return result | {'fixture_app_name': APP, 'app': 'home-assistant'}
