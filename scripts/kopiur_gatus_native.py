"""Gatus native result-history fixture, not production or backend qualification."""
import hashlib
import json
from pathlib import Path
import runpy
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/twin/gatus:v5.37.0@sha256:094eb186e55235db367e90e9d56140e5897b7044c41de23cde4cc18e6da1242e'
HTTP_IMAGE = 'ghcr.io/home-operations/prowlarr:2.6.5.5623@sha256:6152751c3ea2e7751564f5952173d5e83eed0e09f3fabd2cb6bdb58690c39e2f'


def contract():
    scope = runpy.run_path(str(ROOT / 'scripts/kopiur-postgres-drill'))
    native = scope['fixture'].__globals__
    native['CONTRACTS']['gatus'] = (IMAGE, ['gatus'], 'GATUS', 8080, '/config/gatus.yaml')
    base = native['DockerDrill']

    class GatusDrill(base):
        def request(self, container, path, post=False, authenticated=False, check=True):
            config = f'url = "http://127.0.0.1:{self.port}{path}"\n'
            if post:
                config += 'request = "POST"\n'
            if authenticated:
                config += 'header = "Authorization: Bearer ' + self.api_key + '"\n'
            return scope['run']('docker', 'run', '--rm', '-i', '--read-only', '--cap-drop', 'ALL',
                                '--security-opt', 'no-new-privileges', '--network', 'container:' + container,
                                '--entrypoint', 'curl', HTTP_IMAGE, '-fsS', '--max-time', '5',
                                '--config', '-', stdin=config.encode(), check=check)

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(value for value in self.containers if value.endswith('-source-config-holder'))
                template = ('web:\n  port: 8080\nstorage:\n  type: postgres\n'
                            '  path: "${KOPIUR_DB_URI}"\n  caching: false\n'
                            'external-endpoints:\n  - name: fixture\n    group: recovery\n'
                            '    token: "' + self.api_key + '"\n')
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                             'umask 077; cat > /config/gatus.yaml', stdin=template.encode())
            env = {'KOPIUR_DB_URI': 'postgres://app:' + self.password + '@127.0.0.1:5432/gatus?sslmode=disable',
                   'GATUS_CONFIG_PATH': self.config_file, 'GATUS_DELAY_START_SECONDS': '0'}
            return self.start(name, self.image, network='container:' + database, env=env,
                              mounts=[(config, '/config', 'rw')], user='568:568')

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(120):
                if self.request(container, '/health', check=False).returncode == 0:
                    break
                state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                if state != b'true':
                    raise startup_failure(scope, self, container, 'isolated Gatus fixture exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'isolated Gatus readiness deadline')
            if container.endswith('-source-app'):
                for success in ('true', 'false'):
                    self.request(container, '/api/v1/endpoints/recovery_fixture/external?success=' + success,
                                 post=True, authenticated=True)

        def isolated_config(self, raw):
            if len(raw) > 65536 or b'${KOPIUR_DB_URI}' not in raw or b'external-endpoints:' not in raw:
                raise ValueError('unexpected isolated Gatus fixture config')
            return raw

        def application_state(self, container):
            response = json.loads(self.request(container, '/api/v1/endpoints/recovery_fixture/statuses').stdout)
            if not isinstance(response, dict) or response.get('name') != 'fixture':
                raise ValueError('Gatus persisted fixture status unavailable')
            digest = hashlib.sha256(json.dumps(response, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if container.endswith('-restored-app'):
                # Compare captured records before a deliberate post-restore write.
                # This confirms the unchanged external token is actually accepted.
                self.request(container, '/api/v1/endpoints/recovery_fixture/external?success=true',
                             post=True, authenticated=True)
            return digest

    native['DockerDrill'] = GatusDrill
    return native


def fixture():
    from kopiur_native_fixture import exercise
    proof = exercise(contract(), 'gatus', [IMAGE, HTTP_IMAGE])
    return proof | {'post_restore_original_token_write_probe': True,
                    'records_compared_before_deliberate_write_probe': True}
