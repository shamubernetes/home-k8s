"""Tracearr native owner/session fixture, not production multi-store acceptance."""
import hashlib
import json
from pathlib import Path
import runpy
import secrets
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/connorgallopo/tracearr:2.6.1@sha256:4c4afa7b453a4a9efceccb9e3f48e57a88abaf8ef2e207abf0099fdac4c71aa4'
CACHE_IMAGE = 'ghcr.io/dragonflydb/dragonfly:v2.0.0@sha256:7426fdb31ddcf7bd9499b4205f36ebaa83b26149ba1609a0d5f8f474b3631233'
HTTP_IMAGE = 'ghcr.io/home-operations/prowlarr:2.6.5.5623@sha256:6152751c3ea2e7751564f5952173d5e83eed0e09f3fabd2cb6bdb58690c39e2f'


def contract():
    scope = runpy.run_path(str(ROOT / 'scripts/kopiur-postgres-drill'))
    native = scope['fixture'].__globals__
    native['CONTRACTS']['tracearr'] = (IMAGE, ['tracearr'], 'TRACEARR', 3000, '/config/fixture.json')
    base = native['DockerDrill']

    class TracearrDrill(base):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.initial_api_key = self.api_key
            self.auth_secret = None
            self.cookie_secret = self.api_key

        def request(self, container, path, *, body=None, authenticated=False, check=True):
            config = f'url = "http://127.0.0.1:3000{path}"\nheader = "Origin: http://127.0.0.1:3000"\n'
            if authenticated:
                config += 'header = "Authorization: Bearer ' + self.api_key + '"\n'
            if body is not None:
                config += 'header = "Content-Type: application/json"\n'
                config += 'data = ' + json.dumps(json.dumps(body)) + '\n'
            result = native['run']('docker','run','--rm','-i','--read-only','--cap-drop','ALL',
                                 '--security-opt','no-new-privileges','--network','container:' + container,
                                 '--entrypoint','curl',HTTP_IMAGE,'--fail-with-body','-sS','--max-time','5','--config','-',
                                 stdin=config.encode(),check=False)
            if result.returncode and check:
                message = (result.stdout + result.stderr).decode(errors='replace')
                for value in (self.password,self.api_key,self.auth_secret,self.cookie_secret):
                    if value:
                        message = message.replace(value,'[fixture-credential]')
                raise RuntimeError('Tracearr fixture request failed: ' + message[:2000])
            return result

        def app(self, name, database, config):
            holder = self.start(name + '-config-reader',native['PG_IMAGE'],user='568:568',
                                mounts=[(config,'/config','rw')],command=['sleep','infinity'])
            if name == 'source-app':
                self.auth_secret = secrets.token_hex(32)
                raw = json.dumps({'jwtSecret': self.auth_secret, 'cookieSecret': self.api_key}).encode()
                native['run']('docker','exec','-i',holder,'sh','-c','umask 077; cat > /config/fixture.json',stdin=raw)
            else:
                value = json.loads(native['run']('docker','exec',holder,'cat','/config/fixture.json').stdout)
                self.auth_secret = value['jwtSecret']
                if self.api_key != self.initial_api_key and value['originalSession'] != self.api_key:
                    raise ValueError('Tracearr original session identity differs')
                self.api_key = value['originalSession']
            value = json.loads(native['run']('docker','exec',holder,'cat','/config/fixture.json').stdout)
            self.cookie_secret = value['cookieSecret']
            # Each application boot gets a fresh isolated cache. Durable owner and
            # session recovery must therefore come from PostgreSQL, not Redis reuse.
            self.cache_databases = getattr(self,'cache_databases',{})
            if database in self.cache_databases:
                previous = self.cache_databases[database]
                if previous not in self.containers:
                    raise ValueError('Tracearr cache ownership differs')
                native['run']('docker','rm','-fv',previous)
                if native['run']('docker','inspect',previous,check=False).returncode == 0:
                    raise RuntimeError('Tracearr prior fixture cache remains')
                self.containers.remove(previous)
            cache = self.start(name + '-cache',CACHE_IMAGE,network='container:' + database,
                               user='568:568',entrypoint='dragonfly',
                               command=['--logtostderr','--proactor_threads=1',
                                        '--maxmemory=256mb','--bind=127.0.0.1'])
            self.cache_databases[database] = cache
            env = {'DATABASE_URL':'postgresql://app:' + self.password + '@127.0.0.1:5432/tracearr',
                   'REDIS_URL':'redis://127.0.0.1:6379/0','JWT_SECRET':value['jwtSecret'],
                   'COOKIE_SECRET':value['cookieSecret'],'NODE_ENV':'production','HOST':'0.0.0.0',
                   'PORT':'3000','CORS_ORIGIN':'http://127.0.0.1:3000'}
            container = self.start(name,self.image,network='container:' + database,env=env,
                                   mounts=[(config,'/config','rw'),(config,'/app/data','rw')],user='568:568')
            self.caches = getattr(self,'caches',{}) | {container:cache}
            return container

        def healthy(self, container):
            from kopiur_native_fixture import startup_failure
            for _ in range(180):
                cache = self.caches[container]
                if native['run']('docker','inspect','-f','{{.State.Running}}',cache).stdout.strip() != b'true':
                    raise startup_failure(native,self,cache,'isolated Tracearr cache exited')
                result = self.request(container,'/api/v1/setup/status',check=False)
                if result.returncode == 0:
                    break
                if native['run']('docker','inspect','-f','{{.State.Running}}',container).stdout.strip() != b'true':
                    raise startup_failure(native,self,container,'isolated Tracearr exited')
                time.sleep(1)
            else:
                raise startup_failure(native,self,container,'isolated Tracearr readiness deadline')
            if container.endswith('-source-app'):
                response = json.loads(self.request(container,'/api/v1/auth/sign-up/email',
                                      body={'name':'Recovery fixture','email':'recovery@fixture.invalid',
                                            'username':'recovery_fixture','password':self.password}).stdout)
                self.api_key = response['token']
                if not isinstance(self.api_key,str) or not self.api_key:
                    raise ValueError('Tracearr did not issue original fixture session')
                holder = next(value for value in self.containers if value.endswith('-source-config-holder'))
                config = json.loads(native['run']('docker','exec',holder,'cat','/config/fixture.json').stdout)
                config['originalSession'] = self.api_key
                native['run']('docker','exec','-i',holder,'sh','-c','umask 077; cat > /config/fixture.json',
                              stdin=json.dumps(config).encode())
            self.application_state(container)

        def isolated_config(self, raw):
            value = json.loads(raw)
            if len(raw) > 65536 or any(not isinstance(value.get(key),str) or not value[key]
                                      for key in ('jwtSecret','cookieSecret','originalSession')):
                raise ValueError('unexpected Tracearr original fixture identity')
            return raw

        def application_state(self, container):
            session = json.loads(self.request(container,'/api/v1/auth/get-session',authenticated=True).stdout)
            user = session.get('user') if isinstance(session,dict) else None
            if not isinstance(user,dict) or user.get('email') != 'recovery@fixture.invalid' or user.get('role') != 'owner':
                raise ValueError('Tracearr original owner session unavailable')
            status = json.loads(self.request(container,'/api/v1/setup/status').stdout)
            # This pinned Better Auth build stores passwords in accounts. Its
            # legacy setup flag still consults users.password_hash and may be false.
            if status.get('needsSetup') is not False or not isinstance(status.get('hasPasswordAuth'),bool):
                raise ValueError('Tracearr persisted configuration differs')
            # get-session contains cookie/cache expiry data. Native SQL separately
            # compares every durable session field; this checks visible owner state.
            visible = {'owner':{key:user.get(key) for key in ('id','email','name','role')},'setup':status}
            return hashlib.sha256(json.dumps(visible,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    native['DockerDrill'] = TracearrDrill
    return native


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(),'tracearr',[IMAGE,CACHE_IMAGE,HTTP_IMAGE],
                    {'production_redis_state_qualified':False,'production_server_credentials_qualified':False,
                     'production_image_cache_qualified':False})
