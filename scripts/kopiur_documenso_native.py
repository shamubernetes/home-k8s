"""Documenso native PDF/catalog fixture, not production signing or Redis acceptance."""
import hashlib
import json
import re
import time
import kopiur_pocket_id_native as pocket

IMAGE = 'docker.io/documenso/documenso:v2.19.0@sha256:a0592ef8edbc8d74e5e236d9c2dffb3fc4f0104a3ea1c52baaae4673799d8d91'
EMAIL = 'recovery-fixture@example.invalid'


def fixture_pdf():
    stream = b'BT /F1 12 Tf 20 100 Td (Recovery fixture) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    result = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(str(index).encode() + b' 0 obj\n' + value + b'\nendobj\n')
    start = len(result)
    result.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        result.extend(f'{offset:010d} 00000 n \n'.encode())
    result.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF\n')
    return bytes(result)


def contract():
    scope = pocket.contract()
    scope['CONTRACTS']['documenso'] = (IMAGE, ['documenso'], '', 3000, '/config/fixture-identity.json')
    base = scope['DockerDrill']

    class DocumensoDrill(base):
        def auth_header(self):
            return 'Authorization: Bearer ' + self.api_key

        def isolated_config(self, raw):
            if len(raw) > 65536:
                raise ValueError('fixture identity exceeds bound')
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'original_fixture_key', 'original_encryption_key'}:
                raise ValueError('invalid fixture identity fields')
            if not isinstance(value['original_fixture_key'], str) or len(value['original_fixture_key']) < 16:
                raise ValueError('invalid original fixture key')
            if not isinstance(value['original_encryption_key'], str) or not re.fullmatch('[a-f0-9]{64}', value['original_encryption_key']):
                raise ValueError('invalid original fixture encryption key')
            return raw

        def app(self, name, database, config):
            if name == 'source-app':
                holder = next(item for item in self.containers if item.endswith('-source-config-holder'))
                identity = {'original_fixture_key': 'api_' + self.api_key,
                    'original_encryption_key': hashlib.sha256(self.api_key.encode()).hexdigest()}
                scope['run']('docker', 'exec', '-i', holder, 'sh', '-c',
                    'umask 077; cat > /config/fixture-identity.json', stdin=json.dumps(identity).encode())
            raw = scope['run']('docker', 'run', '--rm', '--network', 'none', '--user', '568:568',
                '--mount', 'type=volume,src=' + config + ',dst=/config,readonly,volume-nocopy',
                scope['PG_IMAGE'], 'cat', '/config/fixture-identity.json').stdout
            identity = json.loads(self.isolated_config(raw))
            self.api_key = identity['original_fixture_key']
            self.auth_secret = identity['original_encryption_key']
            url = 'postgres://app:' + self.password + '@127.0.0.1:5432/documenso?sslmode=disable'
            env = {'NEXT_PRIVATE_DATABASE_URL': url, 'NEXT_PRIVATE_DIRECT_DATABASE_URL': url,
                'NEXTAUTH_SECRET': self.auth_secret, 'NEXT_PRIVATE_ENCRYPTION_KEY': self.auth_secret,
                'NEXT_PRIVATE_ENCRYPTION_SECONDARY_KEY': self.auth_secret,
                'NEXT_PUBLIC_WEBAPP_URL': 'http://localhost:3000',
                'NEXT_PRIVATE_INTERNAL_WEBAPP_URL': 'http://127.0.0.1:3000',
                'NEXT_PUBLIC_UPLOAD_TRANSPORT': 'database', 'NEXT_PRIVATE_JOBS_PROVIDER': 'local',
                'DOCUMENSO_DISABLE_TELEMETRY': 'true', 'NEXT_PUBLIC_FEATURE_BILLING_ENABLED': 'false',
                'NPM_CONFIG_CACHE': '/tmp/npm-cache',
                'NEXT_PRIVATE_SMTP_HOST': '127.0.0.1', 'NEXT_PRIVATE_SMTP_PORT': '2525',
                'NEXT_PRIVATE_SMTP_FROM_ADDRESS': EMAIL, 'NEXT_PRIVATE_SMTP_FROM_NAME': 'Recovery fixture'}
            return self.start(name, self.image, network='container:' + database, env=env,
                mounts=[(config, '/config', 'rw')], user='568:568')

        def create_pdf(self, container):
            boundary = 'recovery-fixture-boundary'
            body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="payload"\r\n\r\n'
                + json.dumps({'title': 'Recovery fixture'}) + '\r\n--' + boundary
                + '\r\nContent-Disposition: form-data; name="file"; filename="fixture.pdf"\r\n'
                + 'Content-Type: application/pdf\r\n\r\n').encode() + fixture_pdf()
            body += ('\r\n--' + boundary + '--\r\n').encode()
            config = 'url = "http://127.0.0.1:3000/api/v2/document/create"\n'
            config += 'header = ' + json.dumps(self.auth_header()) + '\n'
            config += 'header = "Content-Type: multipart/form-data; boundary=' + boundary + '"\n'
            config += 'request = "POST"\ndata = ' + json.dumps(body.decode('ascii')) + '\n'
            scope['run']('docker', 'run', '--rm', '-i', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--network', 'container:' + container,
                '--entrypoint', 'curl', pocket.HTTP_IMAGE, '-fsS', '--max-time', '30',
                '--config', '-', stdin=config.encode())

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(180):
                if self.request(container, '/api/health', check=False).returncode == 0:
                    break
                if scope['run']('docker', 'inspect', '-f', '{{.State.Running}}', container).stdout.strip() != b'true':
                    raise startup_failure(scope, self, container, 'isolated Documenso exited')
                time.sleep(1)
            else:
                raise startup_failure(scope, self, container, 'Documenso readiness deadline')
            if container.endswith('-source-app'):
                self.request(container, '/api/auth/email-password/signup', {'name': 'Recovery fixture',
                    'email': EMAIL, 'password': 'Fixture1!' + self.password})
                database = next(item for item in self.containers if item.endswith('-source-db'))
                token = hashlib.sha512(self.api_key.encode()).hexdigest()
                self.sql(database, 'UPDATE "User" SET "emailVerified"=now() WHERE email=\'' + EMAIL + '\'; '
                    'INSERT INTO "ApiToken" (name,token,"userId","teamId") '
                    'SELECT \'Recovery fixture\',\'' + token + '\',u.id,t.id FROM "User" u '
                    'JOIN "Organisation" o ON o."ownerUserId"=u.id JOIN "Team" t ON t."organisationId"=o.id '
                    'WHERE u.email=\'' + EMAIL + '\'', 'documenso', 'app')
                self.create_pdf(container)

        def application_state(self, container):
            value = json.loads(self.request(container, '/api/v2/document', authenticated=True).stdout)
            if 'Recovery fixture' not in json.dumps(value):
                raise ValueError('original-token document catalog is missing the fixture')
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    scope['DockerDrill'] = DocumensoDrill
    return scope


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(), 'documenso', [IMAGE, pocket.HTTP_IMAGE], {
        'fixture_encryption_key_use_qualified': False,
        'production_signing_certificate_and_key_use_qualified': False,
        'production_redis_smtp_delivery_qualified': False})
