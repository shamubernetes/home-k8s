#!/usr/bin/env python3
"""Boot Wizarr from both independently restored originals, never production."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys


def require(value):
    if not value:
        raise ValueError('original Wizarr recovery acceptance failed')


def inventory(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False,
                                     onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in sorted(dirs + files):
            path = Path(base) / name
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            digest = None
            if path.is_file():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            result[str(path.relative_to(root))] = [info.st_mode, info.st_uid, info.st_gid,
                                                 info.st_mtime_ns if path.is_file() else None,
                                                 info.st_size if path.is_file() else None, digest]
    return result


def database(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = sum(db.execute('SELECT count(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0] for t in tables)
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode() + b'\n')
        return {'tables': len(tables), 'rows': rows, 'sha256': digest.hexdigest()}


def native_child():
    # This import initializes the real pinned app, native key loader and ORM.
    from app import create_app
    from app.config import ProductionConfig
    app = create_app(ProductionConfig)
    secrets = json.loads(Path('/data/database/secrets.json').read_text())
    require(bool(secrets.get('SECRET_KEY')) and app.config['SECRET_KEY'] == secrets['SECRET_KEY'])
    client = app.test_client()
    response = client.get('/', follow_redirects=True)
    require(response.status_code == 200 and b'wizarr' in response.data.lower())
    from app.extensions import db
    from sqlalchemy import text
    with app.app_context():
        native_tables = list(db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))
        require(len(native_tables) > 0)
    result = database(Path('/data/database/database.db'))
    result.update(native_app_initialized=True, native_http=200, original_key_loaded=True)
    Path('/work/child-result.json').write_text(json.dumps(result))


def main():
    require(os.environ.get('RECOVERY_TARGET') == 'isolated')
    originals = {b: Path('/original') / b for b in ('nas', 'r2')}
    trees = {b: inventory(p) for b, p in originals.items()}
    require(trees['nas'] and trees['nas'] == trees['r2'])
    results = {}
    Path('/work/tmp').mkdir(mode=0o700, exist_ok=True)
    active = Path('/data/database')
    for backend, original in originals.items():
        require(active.is_dir() and not active.is_symlink())
        for path in active.iterdir():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.copytree(original, active, dirs_exist_ok=True)
        consistent = original / 'kopiur/wizarr-online.db'
        expected = database(consistent)
        require(expected['tables'] > 0)
        for suffix in ('-wal', '-shm', '-journal'):
            (active / ('database.db' + suffix)).unlink(missing_ok=True)
        shutil.copyfile(consistent, active / 'database.db')
        original_keys = (original / 'secrets.json').read_bytes()
        Path('/work/child-result.json').unlink(missing_ok=True)
        Path('/work/native-child-error.json').unlink(missing_ok=True)
        env = dict(os.environ, FLASK_SKIP_SCHEDULER='true', PYTHONPATH='/app', HOME='/work',
                   TMPDIR='/work/tmp', PYTHONDONTWRITEBYTECODE='1')
        # Native startup logs may contain original private integration state.
        with open(os.devnull, 'wb') as sink:
            child = subprocess.run(['/app/.venv/bin/python', __file__, '--native-child'], cwd='/app', env=env,
                                   stdout=sink, stderr=sink, timeout=180)
        if child.returncode != 0 and Path('/work/native-child-error.json').is_file():
            print(Path('/work/native-child-error.json').read_text(), flush=True)
        require(child.returncode == 0)
        result = json.loads(Path('/work/child-result.json').read_text())
        require(all(result[k] == expected[k] for k in ('tables', 'rows', 'sha256')))
        require((active / 'secrets.json').read_bytes() == original_keys)
        require(inventory(original) == trees[backend])
        result['snapshot_id'] = os.environ[backend.upper() + '_SNAPSHOT']
        result['original_unchanged'] = True
        results[backend] = result
    require(results['nas']['sha256'] == results['r2']['sha256'])
    receipt = {'app': 'wizarr', 'independent_original_native_recovery': True,
               'nas_r2_native_data_equal': True, 'full_original_tree_equal': True,
               'regenerated_directory_mtimes_compared': False,
               'results': results}
    Path('/work/native-receipt.json').write_text(json.dumps(receipt, sort_keys=True))
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        native_child() if sys.argv[1:] == ['--native-child'] else main()
    except Exception as error:
        import traceback
        frames = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                  if frame.filename == __file__]
        failure = json.dumps({'failed': True, 'class': type(error).__name__,
                              'source_lines': frames})
        if sys.argv[1:] == ['--native-child']:
            Path('/work/native-child-error.json').write_text(failure)
        print(failure, flush=True)
        raise SystemExit(1) from None
