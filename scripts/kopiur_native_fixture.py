"""Common credential-free native fixture qualification, never a provider proof."""
from kopiur_shared import validate_artifact
from kopiur_stateful_native import artifact_manifest


def startup_failure(scope, drill, container, reason):
    # Called only for disposable native fixtures. Never read production logs.
    result = scope['run']('docker', 'logs', '--tail', '80', container, check=False)
    text = (result.stdout + result.stderr).decode('utf-8', errors='replace')
    for secret in (drill.password, drill.backup_password, drill.api_key, getattr(drill, 'auth_secret', None)):
        if secret:
            text = text.replace(secret, '[fixture-credential]')
    return RuntimeError(reason + ': ' + text)


def exercise(native, app, images, limitations=None):
    limitations = limitations or {}
    if any(not key.endswith('_qualified') or value is not False for key, value in limitations.items()):
        raise ValueError('fixture limitations can only declare unqualified gates')
    for image in dict.fromkeys((native['PG_IMAGE'], *images)):
        native['run']('docker', 'pull', '--platform', 'linux/amd64', image, timeout=600)
    results = []

    def exported(source, expected, identity, visible):
        manifest = artifact_manifest(source)
        validation = validate_artifact(source, manifest)
        proof = native['restore_pvc'](source, app, config_mib=256, database_mib=512,
                                     expected_fingerprints=expected, original_api_key=identity,
                                     expected_application_state=visible)
        for key in ('native_restore', 'restored_app_ping', 'native_table_contents_equal',
                    'original_fixture_identity_used', 'application_visible_state_equal'):
            if proof.get(key) is not True:
                raise RuntimeError(app + ' native fixture mismatch: ' + key)
        results.append(proof | {'artifact_entries': validation['entries_equal'],
                                'producer_removed_before_restore': True,
                                'encrypted_transport_qualified': False,
                                'production_recovery_accepted': False} | limitations)

    native['fixture'](app, export=exported)
    if len(results) != 1:
        raise RuntimeError(app + ' native fixture receipt count differs')
    return results[0]
