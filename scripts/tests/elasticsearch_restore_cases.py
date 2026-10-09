"""Synthetic escrow inputs for isolated ARC restore-plan regressions."""
import copy
import secrets
from kopiur_elasticsearch_escrow import CONFIG_FILES, _encoded, _validate_parts
from kopiur_nonrel_native import IMAGES


def restore_inputs():
    binding = {'generation': 'a' * 32, 'source_uid': 'b' * 64, 'source_pod_uid': 'b' * 64,
               'engine_image': IMAGES['elasticsearch'], 'runtime_version': '8.19.23',
               'credential_versions': {'owned-synthetic': 'fixture'},
               'config_paths': sorted(CONFIG_FILES), 'config_directories': ['.']}
    runtime = {'image': binding['engine_image'], 'version': binding['runtime_version'],
               'variables': {'discovery.type': 'single-node', 'xpack.security.enabled': 'true',
                             'xpack.security.http.ssl.enabled': 'false', 'xpack.ml.enabled': 'false',
                             'ingest.geoip.downloader.enabled': 'false',
                             'ES_JAVA_OPTS': '-Xms256m -Xmx256m -XX:ActiveProcessorCount=2',
                             'path.repo': '/usr/share/elasticsearch/data/snapshot'}}
    credentials = {'elastic_username': 'elastic', 'elastic_password': secrets.token_hex(24)}
    payloads = {'native': b'native-archive-placeholder', 'runtime': _encoded(runtime),
                'credentials': _encoded(credentials), 'config-dir/.': b'',
                **{'config/' + path: b'synthetic-config' for path in CONFIG_FILES}}
    parts = {name: {'binding': copy.deepcopy(binding), 'data': data,
                    'mode': 0o700 if name.startswith('config-dir/') else 0o600, 'uid': 1000, 'gid': 0}
             for name, data in payloads.items()}
    manifest = {'schema': 'k8s92-elasticsearch-escrow/v1', 'binding': binding,
                'entries': _validate_parts(binding, parts)}
    return manifest, parts, runtime, credentials
