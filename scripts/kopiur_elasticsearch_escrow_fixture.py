"""Configuration archive adapters for owned synthetic Elasticsearch fixtures.

Callers must obtain the archive through their owned Docker fixture controller.
This module performs no production I/O and does not establish capture authority,
encryption, isolation, keystore loading or engine recovery acceptance.
"""
import copy
import io
import json
import os
import sys
import tarfile

from kopiur_elasticsearch_escrow import (
    EscrowError, _binding, _same_binding, _validate_component, _validate_parts, component_names,
)


def capture_configuration(drill, source, binding, *, native, runtime, credentials,
                          observe, require_capture_authority, synthetic):
    """Read config bytes from an exact owned synthetic ARC container.

    Authority and the complete binding are rechecked around Docker operations.
    The caller supplies separately captured native/runtime/credential components.
    This read does not qualify their capture or release source admission.
    """
    expected = _binding(binding)
    if (synthetic is not True or sys.platform != 'linux'
            or not os.environ.get('RUNNER_NAME', '').startswith('ghar-set-zoo-')
            or drill.service != 'elasticsearch'):
        raise EscrowError('owned synthetic Elasticsearch ARC fixture required')
    if (not isinstance(source, str) or not isinstance(drill.prefix, str)
            or not source.startswith(drill.prefix + '-')):
        raise EscrowError('owned synthetic source name required')
    # Validate non-config components before allowing any Docker read. The final
    # archive decoder checks their exact binding and original metadata again.
    for name, part in (('native', native), ('runtime', runtime), ('credentials', credentials)):
        _validate_component(expected, name, part)

    def guard():
        if require_capture_authority(copy.deepcopy(expected)) is not True:
            raise EscrowError('affirmative capture authority required')
        if not _same_binding(observe(), expected):
            raise EscrowError('source identity or runtime/credential version changed')
        if drill.registered_id(source) != expected['source_uid']:
            raise EscrowError('synthetic container identity changed')

    def inspect_source():
        raw = drill.run('inspect', '--format', '{{json .}}', expected['source_uid'])
        try:
            actual = json.loads(raw)
            valid = (isinstance(actual, dict)
                     and actual.get('Id') == expected['source_uid']
                     and actual.get('Name') == '/' + source
                     and isinstance(actual.get('Config'), dict)
                     and actual['Config'].get('Image') == expected['engine_image'])
        except (ValueError, TypeError, UnicodeError):
            valid = False
        if not valid:
            raise EscrowError('synthetic source runtime identity differs')

    guard()
    inspect_source()
    guard()
    archive = drill.run('cp', expected['source_uid'] +
                        ':/usr/share/elasticsearch/config/.', '-')
    guard()
    inspect_source()
    guard()
    return configuration_parts(expected, archive, native=native,
                               runtime=runtime, credentials=credentials)


def configuration_parts(binding, archive, *, native, runtime, credentials):
    """Decode Docker cp config/. tar bytes, preserving exact file metadata.

    Reject links and unexpected files rather than silently excluding additional
    configuration dependencies. Directory entries are allowed only when they
    are parents of an inventoried configuration file.
    """
    expected = _binding(binding)
    if not isinstance(archive, bytes) or not archive:
        raise EscrowError('synthetic configuration archive absent')
    paths = set(expected['config_paths'])
    parents = {'.'}
    for path in paths:
        segments = path.split('/')
        parents.update('/'.join(segments[:i]) for i in range(1, len(segments)))
    parts = {}
    seen = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
            for member in source:
                name = member.name
                if name.startswith('./'):
                    name = name[2:]
                if member.isdir() and name.endswith('/'):
                    name = name[:-1]
                if name in seen:
                    raise EscrowError('duplicate synthetic configuration archive entry')
                seen.add(name)
                if member.isdir() and name in expected.get('config_directories', []):
                    parts['config-dir/' + name] = {
                        'binding': copy.deepcopy(expected), 'data': b'',
                        'mode': member.mode, 'uid': member.uid, 'gid': member.gid,
                    }
                    continue
                if member.isdir() and name in parents and (
                        'config_directories' not in expected or name == '.'):
                    continue
                if not member.isfile() or name not in paths:
                    raise EscrowError('unexpected or unsafe synthetic configuration archive entry')
                stream = source.extractfile(member)
                if stream is None:
                    raise EscrowError('synthetic configuration file absent')
                with stream:
                    data = stream.read()
                if len(data) != member.size:
                    raise EscrowError('truncated synthetic configuration file')
                parts['config/' + name] = {
                    'binding': copy.deepcopy(expected), 'data': data,
                    'mode': member.mode, 'uid': member.uid, 'gid': member.gid,
                }
    except (tarfile.TarError, EOFError, OSError) as error:
        raise EscrowError('invalid synthetic configuration archive') from error
    for name, part in (('native', native), ('runtime', runtime), ('credentials', credentials)):
        parts[name] = copy.deepcopy(part)
    _validate_parts(expected, parts)
    return parts


def configuration_archive(binding, parts):
    """Prepare validated config tar for an owned controller, never extract it.

    Runtime and credential components remain separate inputs to the controller.
    Native restore must not start until independent configuration checks pass.
    """
    expected = _binding(binding)
    names = component_names(expected) - {'native'}
    if not isinstance(parts, dict) or set(parts) not in (names, names | {'native'}):
        raise EscrowError('configuration/key/runtime coverage incomplete')
    for name, part in parts.items():
        _validate_component(expected, name, part)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as target:
        for path in sorted(expected.get('config_directories', [])):
            part = parts['config-dir/' + path]
            member = tarfile.TarInfo(path)
            member.type = tarfile.DIRTYPE
            member.mode, member.uid, member.gid = part['mode'], part['uid'], part['gid']
            target.addfile(member)
        for path in sorted(expected['config_paths']):
            part = parts['config/' + path]
            member = tarfile.TarInfo(path)
            member.size = len(part['data'])
            member.mode = part['mode']
            member.uid = part['uid']
            member.gid = part['gid']
            target.addfile(member, io.BytesIO(part['data']))
    return output.getvalue()
