"""Grafana PostgreSQL/dashboard native fixture, not production JWT or CSI acceptance."""
import hashlib
import json
from pathlib import Path
import runpy
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'docker.io/grafana/grafana:12.3.1@sha256:2175aaa91c96733d86d31cf270d5310b278654b03f5718c59de12a865380a31f'
HTTP_IMAGE = 'ghcr.io/home-operations/prowlarr:2.6.5.5623@sha256:6152751c3ea2e7751564f5952173d5e83eed0e09f3fabd2cb6bdb58690c39e2f'


def contract():
    native = runpy.run_path(str(ROOT / 'scripts/kopiur-postgres-drill'))
    base = native['DockerDrill']
    scope = base.__init__.__globals__
    scope['CONTRACTS']['grafana'] = (IMAGE, ['grafana'], '', 3000, '/config/grafana.ini')

    class GrafanaDrill(base):
        def request(self, container, path, payload=None, authenticated=False, check=True):
            config = 'url = "http://127.0.0.1:3000' + path + '"\n'
            if authenticated:
                config += 'user = "admin:' + self.api_key + '"\n'
            if payload is not None:
                config += 'header = "Content-Type: application/json"\nrequest = "POST"\n'
                config += 'data = ' + json.dumps(json.dumps(payload)) + '\n'
            return scope['run']('docker', 'run', '--rm', '-i', '--read-only', '--cap-drop', 'ALL',
                                '--security-opt', 'no-new-privileges', '--network', 'container:' + container,
                                '--entrypoint', 'curl', HTTP_IMAGE, '-fsS', '--max-time', '5', '--config', '-',
                                stdin=config.encode(), check=check)

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(c for c in self.containers if c.endswith('-source-config-holder'))
                ini = ('[analytics]\nreporting_enabled=false\ncheck_for_updates=false\n'
                       '[security]\nadmin_password=' + self.api_key + '\nsecret_key=' + self.api_key + '\n')
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-ec',
                             'umask 077; cat > /config/grafana.ini', stdin=ini.encode())
            env = {'GF_DATABASE_TYPE': 'postgres', 'GF_DATABASE_HOST': '127.0.0.1:5432',
                   'GF_DATABASE_NAME': 'grafana', 'GF_DATABASE_USER': 'app',
                   'GF_DATABASE_PASSWORD': self.password, 'GF_DATABASE_SSL_MODE': 'disable',
                   'GF_PATHS_CONFIG': '/config/grafana.ini', 'GF_PATHS_DATA': '/config/data',
                   'GF_PATHS_LOGS': '/config/logs', 'GF_PATHS_PLUGINS': '/config/plugins',
                   'GF_AUTH_BASIC_ENABLED': 'true', 'GF_AUTH_ANONYMOUS_ENABLED': 'false',
                   'GF_SECURITY_ADMIN_PASSWORD': self.api_key, 'GF_SECURITY_SECRET_KEY': self.api_key,
                   'GF_ANALYTICS_REPORTING_ENABLED': 'false', 'GF_ANALYTICS_CHECK_FOR_UPDATES': 'false',
                   'GF_PLUGINS_PREINSTALL_DISABLED': 'true', 'GF_LOG_LEVEL': 'error'}
            return self.start(name, self.image, network='container:' + database, env=env,
                              mounts=[(config, '/config', 'rw')], user='568:568')

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(120):
                if self.request(container, '/api/health', check=False).returncode == 0:
                    break
                state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                if state != b'true':
                    raise startup_failure(scope, self, container, 'isolated Grafana fixture exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'isolated Grafana readiness deadline')
            if container.endswith('-source-app'):
                value = json.loads(self.request(container, '/api/dashboards/db', {
                    'dashboard': {'id': None, 'uid': 'recovery-fixture', 'title': 'Native recovery fixture',
                                  'schemaVersion': 41, 'version': 0, 'panels': []},
                    'overwrite': False}, authenticated=True).stdout)
                if value.get('status') != 'success':
                    raise ValueError('Grafana fixture dashboard write failed')

        def isolated_config(self, data):
            if len(data) > 65536 or not data.startswith(b'[analytics]\n'):
                raise ValueError('unexpected Grafana fixture configuration')
            return data

        def application_state(self, container):
            value = json.loads(self.request(container, '/api/dashboards/uid/recovery-fixture', authenticated=True).stdout)
            if value.get('dashboard', {}).get('uid') != 'recovery-fixture':
                raise ValueError('restored Grafana dashboard differs')
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    scope['DockerDrill'] = GrafanaDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'grafana', [IMAGE, HTTP_IMAGE], {
        'production_jwt_identity_qualified': False,
        'datasource_secret_decryption_qualified': False})
