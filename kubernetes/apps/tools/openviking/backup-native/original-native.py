"""Isolated exact-image native OVPack restore, with required original vectors."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tree(root):
    entries = {}
    for path in sorted(root.rglob('*')):
        name = str(path.relative_to(root))
        if path.is_symlink():
            entries[name] = ['link', os.readlink(path)]
        elif path.is_file():
            entries[name] = ['file', digest(path)]
        elif path.is_dir():
            entries[name] = ['dir']
        else:
            raise ValueError('unsupported original file kind')
    return entries


def pack(path):
    from openviking.storage.ovpack.operations import (
        base_name_from_entries, read_manifest, validate_manifest_content, read_dense_vectors,
    )
    from openviking.storage.ovpack.validation import _zip_file_members_by_path
    with zipfile.ZipFile(path) as archive:
        base = base_name_from_entries(archive.infolist())
        manifest = read_manifest(archive, base)
        records = validate_manifest_content(archive, manifest, archive.infolist(), base)
        vectors = read_dense_vectors(archive, manifest, base, records)
        # Use the exact image's canonical mapping. ZIP members include a content
        # prefix which is not present in the manifest's logical file paths.
        files = {name: hashlib.sha256(archive.read(safe_path)).hexdigest()
                 for name, (_, safe_path) in
                 _zip_file_members_by_path(archive.infolist(), base).items()}
    assert files, 'native package has no protected file entries'
    return records, vectors, files


async def native(side):
    from openviking.service.core import OpenVikingService
    from openviking.server.identity import RequestContext, Role
    from openviking_cli.session.user_id import UserIdentifier
    original = Path('/original/' + side + '-0/kopiur-native/current')
    capture = json.loads((original / 'capture.json').read_text())
    config = json.loads((original / 'ov.conf').read_text())
    config_file = Path('/work') / (side + '.conf')
    config_file.write_text(json.dumps(config))
    os.environ['OPENVIKING_CONFIG_FILE'] = str(config_file)
    # The native bundle contains its own verified OAuth escrow. The separate
    # live auth PVC may legitimately be newer; no cross-store atomicity gate.
    os.environ['OPENVIKING_CODEX_AUTH_PATH'] = str(original / 'codex_auth.json')
    assert digest(Path(os.environ['OPENVIKING_CODEX_AUTH_PATH'])) == capture['oauth_sha256']
    json.loads(Path('/original/' + side + '-1/codex_auth.json').read_text())
    for index, (account, item) in enumerate(sorted(capture['packages'].items())):
        source = original / item['file']
        assert digest(source) == item['sha256']
        before = pack(source)
        assert len(before[0]) == item['index_records'] and len(before[1]) == item['dense_vectors']
        user = UserIdentifier(account, 'default')
        service = OpenVikingService(path='/work/' + side + '-' + str(index), user=user)
        await service.initialize()
        try:
            context = RequestContext(user=user, role=Role.ROOT)
            await service.pack.restore_ovpack(str(source), ctx=context,
                                             on_conflict='overwrite', vector_mode='require')
            exported = Path('/work') / (side + '-' + str(index) + '.ovpack')
            await service.pack.backup_ovpack(str(exported), ctx=context, include_vectors=True)
            after = pack(exported)
            # Order is not part of the public package contract; records are identified by ID.
            records = lambda values: {row['record_id']: row for row in values}
            assert records(before[0]) == records(after[0])
            assert before[1] == after[1] and before[2] == after[2]
        finally:
            await service.close()
    print(json.dumps({'native_original_passed': side, 'index_records': capture['index_records'],
                      'dense_vectors': capture['dense_vectors'], 'vector_mode': 'require',
                      'all_original_records_vectors_files_equal': True}), flush=True)


def main(side):
    os.umask(0o077)
    if side == 'r2':
        for index in range(3):
            left = tree(Path('/original/nas-' + str(index)))
            assert left and left == tree(Path('/original/r2-' + str(index)))
    # Native singleton storage and config managers are process-local.
    for side in [side]:
        result = subprocess.run([sys.executable, __file__, side], capture_output=True, timeout=600)
        Path('/work/' + side + '-private.log').write_bytes(result.stdout + result.stderr)
        if result.returncode:
            for line in result.stdout.decode().splitlines():
                if line.startswith('{"native_original_failed":'):
                    print(line, flush=True)
            raise RuntimeError('isolated native original import failed for ' + side)
        # Emit only the explicit receipt, never native startup logs or user text.
        receipt = [line for line in result.stdout.decode().splitlines()
                   if line.startswith('{"native_original_passed":')]
        assert len(receipt) == 1
        print(receipt[0], flush=True)
    print(json.dumps({'all_three_original_sources_equal': side == 'r2', 'native_original_store_recovered': side,
                      'production_access': False}), flush=True)


if __name__ == '__main__':
    try:
        assert len(sys.argv) == 2
        if sys.argv[1] in ['nas', 'r2']:
            asyncio.run(native(sys.argv[1]))
        else:
            assert sys.argv[1] in ['verify-nas', 'verify-r2']
            main(sys.argv[1].removeprefix('verify-'))
    except Exception as error:
        import traceback
        print(json.dumps({'native_original_failed': type(error).__name__,
                          'lines': [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                                    if frame.filename == __file__], 'private_details_withheld': True}), flush=True)
        raise SystemExit(1) from None
