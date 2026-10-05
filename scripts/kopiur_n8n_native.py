"""Pinned n8n workflow/credential fixture, never production automation acceptance."""
import hashlib
import json
import time

import kopiur_pocket_id_native as pocket

IMAGE = 'docker.io/n8nio/n8n:stable@sha256:307d6065be25619aa24cfc63a7c2f04ca56d084a08c05c8e9f189a89f353b1ec'
WORKFLOW_ID = 'RecoveryFixture1'
CREDENTIAL_ID = 'RecoverySecret01'


def visible_state(workflows, credentials, original_key):
    if len(workflows) != 1 or workflows[0].get('id') != WORKFLOW_ID:
        raise ValueError('fixture workflow catalog changed')
    if len(credentials) != 1 or credentials[0].get('id') != CREDENTIAL_ID:
        raise ValueError('fixture credential catalog changed')
    if credentials[0].get('data', {}).get('password') != original_key:
        raise ValueError('original fixture encryption key did not recover credential')
    workflow = {key: workflows[0][key] for key in ('id', 'name', 'active', 'nodes', 'connections', 'settings')}
    credential = {key: credentials[0][key] for key in ('id', 'name', 'type', 'data')}
    return hashlib.sha256(json.dumps({'workflow': workflow, 'credential': credential}, sort_keys=True).encode()).hexdigest()


def contract():
    scope = pocket.contract()
    scope['CONTRACTS']['n8n'] = (IMAGE, ['n8n'], '', 5678, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class N8nDrill(base):
        def app(self, name, database, config):
            holder = next(item for item in self.containers if item.endswith('-source-config-holder') or item.endswith('-config-copy'))
            if name == 'source-app':
                identity = json.dumps({'original_fixture_key': self.api_key}).encode()
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=identity)
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            self.api_key = json.loads(self.isolated_config(raw))['original_fixture_key']
            env = {'DB_TYPE': 'postgresdb', 'DB_POSTGRESDB_HOST': '127.0.0.1',
                'DB_POSTGRESDB_PORT': '5432', 'DB_POSTGRESDB_DATABASE': 'n8n',
                'DB_POSTGRESDB_USER': 'app', 'DB_POSTGRESDB_PASSWORD': self.password,
                'N8N_ENCRYPTION_KEY': self.api_key, 'N8N_USER_FOLDER': '/config',
                'N8N_HOST': 'localhost', 'N8N_PORT': '5678', 'N8N_PROTOCOL': 'http',
                'N8N_SECURE_COOKIE': 'false', 'N8N_DIAGNOSTICS_ENABLED': 'false',
                'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false', 'N8N_TEMPLATES_ENABLED': 'false',
                'N8N_RUNNERS_ENABLED': 'false', 'N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS': 'true'}
            return self.start(name, self.image, network='container:' + database, env=env,
                mounts=[(config, '/config', 'rw')], user='568:568')

        def cli_import(self, container, kind, value):
            path = '/tmp/fixture-' + kind + '.json'
            scope['run']('docker', 'exec', '-i', container, 'sh', '-ec',
                'umask 077; cat > ' + path, stdin=json.dumps(value).encode())
            scope['run']('docker', 'exec', container, 'n8n', 'import:' + kind, '--input=' + path)

        def cli_export(self, container, kind, decrypted=False):
            path = '/tmp/fixture-export-' + kind + '.json'
            args = ['docker', 'exec', container, 'n8n', 'export:' + kind, '--all', '--output=' + path]
            if decrypted:
                args.append('--decrypted')
            scope['run'](*args)
            return json.loads(scope['run']('docker', 'exec', container, 'cat', path).stdout)

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(180):
                if self.request(container, '/healthz/readiness', check=False).returncode == 0:
                    break
                state = scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip()
                if state != b'true':
                    raise startup_failure(scope, self, container, 'isolated n8n exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'isolated n8n readiness deadline')
            if container.endswith('-source-app'):
                self.request(container, '/rest/owner/setup', {'email': 'recovery-fixture@example.invalid',
                    'firstName': 'Recovery', 'lastName': 'Fixture', 'password': self.password + 'F1!'})
                self.cli_import(container, 'credentials', [{'id': CREDENTIAL_ID,
                    'name': 'Recovery credential', 'type': 'httpBasicAuth',
                    'data': {'user': 'recovery-fixture', 'password': self.api_key}}])
                self.cli_import(container, 'workflow', [{'id': WORKFLOW_ID, 'name': 'Recovery fixture',
                    'active': False, 'nodes': [{'id': 'fixture-manual-trigger', 'name': 'Manual Trigger',
                        'type': 'n8n-nodes-base.manualTrigger', 'typeVersion': 1,
                        'position': [0, 0], 'parameters': {}}], 'connections': {}, 'settings': {}}])
                self.application_state(container)

        def application_state(self, container):
            return visible_state(self.cli_export(container, 'workflow'),
                self.cli_export(container, 'credentials', decrypted=True), self.api_key)

    scope['DockerDrill'] = N8nDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    result = exercise(contract(), 'n8n', [IMAGE, pocket.HTTP_IMAGE], {
        'production_workflow_executions_qualified': False,
        'production_community_nodes_license_qualified': False})
    return result | {'original_fixture_encryption_key_used': True,
        'native_cli_workflow_and_decrypted_credential_equal': True}
