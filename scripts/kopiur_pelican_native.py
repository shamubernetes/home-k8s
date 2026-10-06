"""Pelican native PostgreSQL and original-key fixture, not live game-server recovery."""
import base64
import hashlib
import json
from pathlib import Path
import runpy
import secrets
import time

ROOT=Path(__file__).resolve().parents[1]
IMAGE='ghcr.io/pelican/panel:v1.0.0-beta36@sha256:45cecf27176630ac628ebc9badc0ad273bdaa9bf441648c363dd8daeb9cc3bff'
HTTP_IMAGE='ghcr.io/home-operations/prowlarr:2.6.5.5623@sha256:6152751c3ea2e7751564f5952173d5e83eed0e09f3fabd2cb6bdb58690c39e2f'
BOOT="require '/var/www/html/vendor/autoload.php'; $app=require '/var/www/html/bootstrap/app.php'; $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); $cfg=json_decode(file_get_contents('/config/fixture.json'),true,512,JSON_THROW_ON_ERROR); "
SEED=BOOT+"$u=App\\Models\\User::create(['username'=>'recovery_fixture','email'=>'recovery@fixture.invalid','password'=>Illuminate\\Support\\Facades\\Hash::make($cfg['password']),'mfa_app_secret'=>'original-fixture-mfa-secret']); App\\Models\\ApiKey::create(['user_id'=>$u->id,'key_type'=>App\\Models\\ApiKey::TYPE_APPLICATION,'identifier'=>substr($cfg['originalToken'],0,16),'token'=>substr($cfg['originalToken'],16),'memo'=>'Recovery fixture','permissions'=>['user'=>1]]);"
VISIBLE=BOOT+"$key=App\\Models\\ApiKey::findToken($cfg['originalToken']); if (!$key || $key->user->email!=='recovery@fixture.invalid' || $key->user->mfa_app_secret!=='original-fixture-mfa-secret') {throw new RuntimeException('Original Pelican fixture key or user differs');} echo json_encode(['uuid'=>$key->user->uuid,'email'=>$key->user->email,'username'=>$key->user->username,'originalTokenDecrypted'=>true,'originalMfaSecretDecrypted'=>true],JSON_THROW_ON_ERROR);"


def contract():
    scope=runpy.run_path(str(ROOT/'scripts/kopiur-postgres-drill'))
    native=scope['fixture'].__globals__
    native['CONTRACTS']['pelican']=(IMAGE,['pelican'],'PELICAN',8000,'/config/fixture.json')
    base=native['DockerDrill']
    class PelicanDrill(base):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.initial_api_key=self.api_key
            self.auth_secret=None
        def app(self,name,database,config):
            holder=self.start(name+'-config-reader',native['PG_IMAGE'],user='568:568',
                              mounts=[(config,'/config','rw')],command=['sleep','infinity'])
            if name=='source-app':
                self.api_key='papp_'+'f'*11+secrets.token_hex(16)
                self.auth_secret='base64:'+base64.b64encode(secrets.token_bytes(32)).decode()
                value={'appKey':self.auth_secret,'originalToken':self.api_key,'password':self.password}
                native['run']('docker','exec','-i',holder,'sh','-c','umask 077; cat > /config/fixture.json',
                              stdin=json.dumps(value).encode())
                native['run']('docker','exec','-i',holder,'sh','-c','umask 077; cat > /config/pelican-seed.php',
                              stdin=('<?php\n'+SEED).encode())
            else:
                value=json.loads(native['run']('docker','exec',holder,'cat','/config/fixture.json').stdout)
                if self.api_key!=self.initial_api_key and value['originalToken']!=self.api_key:
                    raise ValueError('Pelican original token differs')
                self.api_key=value['originalToken']
                self.auth_secret=value['appKey']
            env={'APP_KEY':value['appKey'],'APP_ENV':'production','APP_DEBUG':'false',
                 'APP_URL':'http://127.0.0.1:8000','DB_CONNECTION':'pgsql','DB_HOST':'127.0.0.1',
                 'DB_PORT':'5432','DB_DATABASE':'pelican','DB_USERNAME':'app','DB_PASSWORD':self.password,
                 'CACHE_STORE':'array','SESSION_DRIVER':'array','QUEUE_CONNECTION':'sync','LOG_CHANNEL':'stderr'}
            mounts=[(config,'/config','rw'),(config,'/var/www/html/storage','rw'),
                    (config,'/var/www/html/bootstrap/cache','rw'),(config,'/pelican-data','rw')]
            command='mkdir -p /config/framework/cache/data /config/framework/sessions /config/framework/views /config/logs; '
            if name=='source-app':
                command+='php artisan migrate --force; php /config/pelican-seed.php; '
            command+='exec php artisan serve --host=0.0.0.0 --port=8000 --no-reload'
            # Only immutable native PHP code and the fixed fixture seed execute.
            # Scheduler, Caddy and plugin installation are outside this fixture.
            container=self.start(name,self.image,network='container:'+database,env=env,mounts=mounts,
                                 user='568:82',entrypoint='/bin/ash',command=['-ec',command])
            return container
        def php(self,container,code):
            result=native['run']('docker','exec','-i',container,'php',
                                stdin=('<?php\n'+code).encode(),check=False,timeout=120)
            if result.returncode:
                message=(result.stdout+result.stderr).decode(errors='replace')
                for value in (self.password,self.api_key,self.auth_secret):
                    if value:
                        message=message.replace(value,'[fixture-credential]')
                raise RuntimeError('Pelican isolated native CLI failed: '+message[-4000:])
            return result
        def healthy(self,container):
            from kopiur_native_fixture import startup_failure
            for _ in range(120):
                result=native['run']('docker','run','--rm','--read-only','--cap-drop','ALL',
                                    '--security-opt','no-new-privileges','--network','container:'+container,
                                    '--entrypoint','curl',HTTP_IMAGE,'-fsS','--max-time','5','http://127.0.0.1:8000/up',check=False)
                if result.returncode==0:
                    break
                if native['run']('docker','inspect','-f','{{.State.Running}}',container).stdout.strip()!=b'true':
                    raise startup_failure(native,self,container,'isolated Pelican exited')
                time.sleep(1)
            else:
                raise startup_failure(native,self,container,'isolated Pelican readiness deadline')
            self.application_state(container)
        def isolated_config(self,raw):
            value=json.loads(raw)
            if len(raw)>65536 or any(not isinstance(value.get(key),str) or not value[key]
                                    for key in ('appKey','originalToken','password')):
                raise ValueError('unexpected Pelican original fixture identity')
            return raw
        def application_state(self,container):
            value=json.loads(self.php(container,VISIBLE).stdout)
            if value.get('originalTokenDecrypted') is not True or value.get('originalMfaSecretDecrypted') is not True:
                raise ValueError('Pelican original fixture key did not decrypt')
            return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    native['DockerDrill']=PelicanDrill
    return native


def fixture():
    from kopiur_native_fixture import exercise
    return exercise(contract(),'pelican',[IMAGE,HTTP_IMAGE],
                    {'production_plugins_and_catalog_qualified':False,'production_wings_game_state_qualified':False,
                     'bundled_caddy_scheduler_entrypoint_qualified':False})
