"""Verify all retained original sources and prepare isolated native PG imports."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from capture import validate_archive


def tree(root):
    result = {}
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ['link', os.readlink(path)]
        elif path.is_file():
            with path.open('rb') as handle:
                result[relative] = ['file', hashlib.file_digest(handle, 'sha256').hexdigest()]
        elif path.is_dir():
            result[relative] = ['dir']
        else:
            raise ValueError('special file in original')
    return result


def main():
    os.umask(0o077)
    work = Path('/work')
    os.chown(work, 999, 999)
    os.chmod(work, 0o700)
    count = 0
    for index in range(3):
        nas = Path('/original/nas-' + str(index))
        r2 = Path('/original/r2-' + str(index))
        baseline = tree(nas)
        assert baseline and tree(r2) == baseline
        count += len(baseline)
    for side in ['nas', 'r2']:
        raw = Path('/original/' + side + '-0')
        assert (raw / 'configuration.yaml').is_file()
        json.loads((raw / '.storage/auth').read_text())
        git = Path('/original/' + side + '-1/repository')
        result = subprocess.run(['git', '-c', 'safe.directory=*', '--git-dir=' + str(git),
                                 'fsck', '--full'], capture_output=True, timeout=120)
        assert result.returncode == 0
        root = Path('/original/' + side + '-2/current')
        native = json.loads((root / 'native.json').read_text())
        archive = root / 'homeassistant.tar'
        with archive.open('rb') as handle:
            assert hashlib.file_digest(handle, 'sha256').hexdigest() == native['sha256']
        validate_archive(archive)
        target = work / (side + '.dump')
        shutil.copyfile(root / 'database.dump', target)
        os.chown(target, 999, 999)
        os.chmod(target, 0o600)
    print(json.dumps({'all_three_original_sources_equal': True, 'stores': ['nas', 'r2'],
                      'entries_verified': count, 'native_config_archive_passed': True,
                      'original_auth_and_git_history_passed': True}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        print(json.dumps({'native_original_failed': type(error).__name__,
                          'lines': [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                                    if frame.filename == __file__], 'private_details_withheld': True}), flush=True)
        raise SystemExit(1) from None
