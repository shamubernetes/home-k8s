"""Atuin users/session native fixture, not client decryption or production acceptance."""
import hashlib
import json
from pathlib import Path
import runpy
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/atuinsh/atuin:18.23.0@sha256:f232feeead54a0a13132b9cd477e312c3380e9dc90f1db24c4f79ce6c8e034ea'


def contract():
    native = runpy.run_path(str(ROOT / 'scripts/kopiur-postgres-drill'))
    base = native['DockerDrill']
    scope = base.__init__.__globals__
    scope['CONTRACTS']['atuin'] = (IMAGE, ['atuin'], '', 8888, '/config/fixture-session.json')

    class AtuinDrill(base):
        def request(self, container, path, payload=None, authenticated=False, check=True):
            config = 'url = "http://127.0.0.1:8888' + path + '"\n'
            if authenticated:
                config += 'header = "Authorization: Token ' + self.api_key + '"\n'
            if payload is not None:
                config += 'header = "Content-Type: application/json"\nrequest = "POST"\n'
                config += 'data = ' + json.dumps(json.dumps(payload)) + '\n'
            return scope['run']('docker', 'exec', '-i', container, 'curl', '-fsS',
                                '--max-time', '5', '--config', '-', stdin=config.encode(), check=check)

        def app(self, name, database, config):
            env = {'ATUIN_DB_URI': 'postgresql://app:' + self.password + '@127.0.0.1:5432/atuin',
                   'ATUIN_HOST': '127.0.0.1', 'ATUIN_PORT': '8888',
                   'ATUIN_OPEN_REGISTRATION': 'true', 'ATUIN_METRICS__ENABLE': 'false',
                   'ATUIN_TLS__ENABLE': 'false', 'ATUIN_CONFIG_DIR': '/config', 'RUST_LOG': 'error'}
            return self.start(name, self.image, network='container:' + database, env=env,
                              mounts=[(config, '/config', 'rw')], command=['start'], user='568:568')

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(120):
                if self.request(container, '/healthz', check=False).returncode == 0:
                    break
                state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                if state != b'true':
                    raise startup_failure(scope, self, container, 'isolated Atuin fixture exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'isolated Atuin readiness deadline')
            if container.endswith('-source-app'):
                result = json.loads(self.request(container, '/register', {
                    'username': 'recovery-fixture', 'email': 'fixture@example.invalid',
                    'password': self.password}).stdout)
                self.api_key = result['session']
                # Fixture identity only. Do not infer original production escrow.
                identity = json.dumps({'username': 'recovery-fixture', 'session': self.api_key}).encode()
                scope['run']('docker', 'exec', '-i', container, 'sh', '-ec',
                             'umask 077; cat > /config/fixture-session.json', stdin=identity)

        def isolated_config(self, data):
            if len(data) > 65536:
                raise ValueError('fixture session exceeds bound')
            value = json.loads(data)
            if set(value) != {'username', 'session'} or value['username'] != 'recovery-fixture':
                raise ValueError('unexpected fixture session')
            return data

        def application_state(self, container):
            identity = self.isolated_config(scope['run']('docker', 'exec', container, 'cat', self.config_file).stdout)
            if json.loads(identity)['session'] != self.api_key:
                raise ValueError('original fixture session changed')
            value = json.loads(self.request(container, '/api/v0/me', authenticated=True).stdout)
            if value != {'username': 'recovery-fixture'}:
                raise ValueError('restored Atuin user differs')
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = AtuinDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'atuin', [IMAGE], {'client_history_decryption_qualified': False})
