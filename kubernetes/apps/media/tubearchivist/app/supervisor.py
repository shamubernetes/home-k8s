#!/usr/bin/env python3
"""Pinned TubeArchivist native lifecycle supervisor with warm idle drain.

Resume restarts only worker and beat, never destructive ta_startup/run.sh.
An admitted long task keeps writing and makes capture fail, it is not killed to
manufacture an idle boundary. The web process stays up behind ASGI admission.
"""
import hashlib
import importlib.metadata
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import coordination as gate

SOURCES = {
    'run.sh': '0ec3c184265a2130b1ab444fe7b8068a54c792a353b2a22e900401e53b6452fa',
    'backend_start.py': 'ba3dfb1e0de9c92b60b4a79c328221c98bf0ce5550e38585f5c76f452fffd3f4',
    'beat_auto_spawn.sh': 'b87b04c1ec9e94e03d740fa4674c2d67c84c6c5ba9826e8030c79f11779608e9',
    'config/asgi.py': '483f31cfb8f314a6e88704a7f019a5b5aff1bb95f3c0fdd1109cffb2dcc649a0',
}


def members(group):
    result = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] != 'Z':
                result.append(int(path.name))
        except FileNotFoundError:
            pass
    return result


def run():
    os.chdir('/app')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ['PYTHONPATH'] = str(Path(__file__).parent) + ':/app'
    for relative, expected in SOURCES.items():
        gate.check(hashlib.sha256(Path(relative).read_bytes()).hexdigest() == expected,
                   'unqualified native supervisor source')
    gate.check(gate.identity() is not None, 'Linux process identity required')
    for package, version in {'Django':'6.0.7', 'celery':'5.6.3', 'uvicorn':'0.52.4',
                             'django-celery-beat':'2.9.0'}.items():
        gate.check(importlib.metadata.version(package) == version, 'unqualified native runtime')
    gate.check(os.environ.get('TA_AUTO_UPDATE_YTDLP', '').lower() not in {'release', 'nightly'},
               'mutable runtime auto-update requires requalification')
    # Single supervisor, including after a controller/collector crash.
    with gate.lock('supervisor.lock', blocking=False):
        gate.recover()
        with gate.writer():
            # Only initial normal-source startup. Never use on a held recovery.
            import backup
            gate.check(not backup.redis_client().exists(backup.RECOVERY_KEY),
                       'restored execution is held, normal startup refused')
            for command in ('ta_stop_on_error', 'migrate', 'collectstatic', 'ta_envcheck',
                            'ta_connection', 'ta_startup'):
                args = ['--noinput', '-c'] if command == 'collectstatic' else []
                subprocess.run([sys.executable, 'manage.py', command, *args], check=True)
        services = {}
        leases = {}
        commands = {
            'worker': ['celery', '-A', 'task.celery', 'worker', '--loglevel=INFO',
                       '--concurrency', '4', '--max-tasks-per-child', '5', '--max-memory-per-child', '150000'],
            'beat': ['celery', '-A', 'task', 'beat', '--loglevel=INFO', '--scheduler',
                     'coordinated_beat:DrainedScheduler'],
        }
        nginx = subprocess.Popen(['nginx', '-g', 'daemon off;'], start_new_session=True)
        web = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'coordinated_asgi:application',
                                '--host', '0.0.0.0', '--port', os.environ.get('TA_BACKEND_PORT', '8080'),
                                '--workers', '4', '--log-level', 'error'], start_new_session=True)
        closing = None
        stopping = set()
        running = True

        def shutdown(*_):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            while running:
                gate.recover()
                state = gate.current()
                closed = bool(state and state.get('admission') == 'CLOSED')
                if closed and closing != state['generation']:
                    closing = state['generation']
                    # SIGTERM is Celery warm shutdown, no forced kill on deadline.
                    # Beat closes at a scheduler boundary and proves final sync.
                    for name in ('beat', 'worker'):
                        process = services.get(name)
                        if process and process.poll() is None:
                            process.terminate()
                            stopping.add(name)
                for name, process in list(services.items()):
                    if process.poll() is None or members(process.pid):
                        continue
                    os.close(leases.pop(name))
                    services.pop(name)
                    stopping.discard(name)
                if not closed:
                    closing = None
                    for name, command in commands.items():
                        if name not in services:
                            fd = gate.writer_fd()
                            try:
                                process = subprocess.Popen(command, start_new_session=True)
                            except BaseException:
                                os.close(fd)
                                raise
                            leases[name] = fd
                            services[name] = process
                elif not services:
                    clean = gate.read(gate.ROOT / 'beat-clean.json', {})
                    gate.atomic(gate.ROOT / 'drained.json', {
                        'generation': state['generation'], 'supervisor': gate.identity(),
                        'beat_clean': clean.get('generation') == state['generation'] and clean.get('clean') is True,
                        'drained_at': time.monotonic(),
                        'qualified_sources': SOURCES,
                    })
                gate.check(web.poll() is None and nginx.poll() is None, 'native web service exited')
                gate.atomic(gate.ROOT / 'supervisor.json', {
                    'owner': gate.identity(), 'closing': closing,
                    'services': {name: gate.identity(p.pid) for name, p in services.items()},
                    'web': gate.identity(web.pid), 'nginx': gate.identity(nginx.pid),
                    'updated_at': time.monotonic(),
                })
                time.sleep(0.05)
        finally:
            # Container termination is not a checkpoint. Let admitted tasks drain.
            for process in [*services.values(), web, nginx]:
                if process.poll() is None:
                    process.terminate()
            for process in [*services.values(), web, nginx]:
                process.wait()
            for fd in leases.values():
                os.close(fd)


if __name__ == '__main__':
    os.umask(0o077)
    run()
