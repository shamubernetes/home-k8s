"""Prepare reviewed original consumer releases without granting capture authority.

A release recipe records reviewed source semantics, not effective application
configuration. Every startup selection is supplied explicitly. Complete ledger,
query and native snapshot coverage is checked before any private source I/O.
"""
import copy

from kopiur_elasticsearch_catalog import qualified_indices, validate_catalog
from kopiur_elasticsearch_consumer_authority import ConsumerAuthority, PROFILES
from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_queries import contract_digests, query_records, validate_queries
from kopiur_elasticsearch_resolution import matches, validate_contracts
from kopiur_elasticsearch_restore import consumer_inventory
from kopiur_elasticsearch_source_witness import SOURCE_PATHS, SOURCE_URL

SCHEMA = 'k8s92-reviewed-original-consumer-releases/v1'


def build_original_plans(releases, *, checkouts, startup_selections):
    """Bind both original consumers to reviewed release bytes and explicit inputs.

    No environment/default lookup, process read, credential read or registry
    call occurs here. ConsumerAuthority rechecks release bytes and all replicas
    under the independent source grant/fence when these plans are executed.
    """
    if (not isinstance(releases, dict) or set(releases) != {'schema', 'consumers'}
            or releases['schema'] != SCHEMA or not isinstance(releases['consumers'], list)
            or len(releases['consumers']) != len(PROFILES)
            or not isinstance(checkouts, dict) or set(checkouts) != set(PROFILES)
            or not isinstance(startup_selections, dict) or set(startup_selections) != set(PROFILES)):
        raise EscrowError('complete reviewed original release and startup inputs required')
    plans, apps = [], set()
    for recipe in releases['consumers']:
        if (not isinstance(recipe, dict) or set(recipe) - {'source_witness'} != {
                'application', 'store', 'image', 'declared_image', 'source_url',
                'source_revision', 'source_files', 'selection'}
                or not isinstance(recipe['application'], str)
                or recipe['application'] not in PROFILES or recipe['application'] in apps):
            raise EscrowError('unique reviewed original consumer recipe required')
        app = recipe['application']
        if (recipe['store'] != 'elasticsearch-indices:' + app
                or not isinstance(recipe['source_files'], dict)):
            raise EscrowError('original consumer store or source identity differs')
        selection = recipe['selection']
        expected = startup_selections[app]
        plan = {k: copy.deepcopy(v) for k, v in recipe.items() if k != 'selection'}
        plan.update(source_checkout=checkouts[app], expected_selection=expected)
        if app == 'media/tubearchivist':
            if (recipe['source_url'] != SOURCE_URL or 'source_witness' not in recipe
                    or set(recipe['source_files']) != set(SOURCE_PATHS)
                    or not isinstance(selection, dict) or set(selection) != {'selectors', 'required_indices'}
                    or selection['selectors'] != ['ta_*'] or expected is not None):
                raise EscrowError('reviewed fixed TubeArchivist selection and artifact witness required')
            plan.update(copy.deepcopy(selection), default_selection=None)
        else:
            if (recipe['source_url'] != 'https://github.com/TheZoo-House/cowbell'
                    or 'source_witness' in recipe or not isinstance(selection, dict)
                    or set(selection) != {'default_prefix'}
                    or not isinstance(selection['default_prefix'], str)
                    or expected is not None and not isinstance(expected, str)):
                raise EscrowError('reviewed Cowbell default and explicit startup selection required')
            prefix = (expected or '').strip() or selection['default_prefix']
            plan.update(default_selection=selection['default_prefix'],
                        selectors=[prefix, prefix + '-state'],
                        required_indices=[prefix, prefix + '-state'])
        plans.append(plan)
        apps.add(app)
    # Validate all image, source, witness, checkout and selection fields with the
    # same concrete adapter used by execution. Construction performs no reads.
    ConsumerAuthority(plans, require_authority=lambda _: False)
    return sorted(plans, key=lambda p: p['application'])


def preflight_consumer_export(binding, *, ledger, backend, selections, contracts, indices):
    """Refuse partial or mismatched plans before release/process/credential I/O.

    Prefix expansion is still checked by authenticated native resolution during
    capture. This only qualifies explicitly supplied physical indices and queries.
    """
    indices = qualified_indices(indices, 'prepared consumer indices')
    selections = validate_contracts(selections)
    if not isinstance(contracts, list) or not contracts:
        raise EscrowError('complete reviewed consumer queries required')
    records = []
    for selection in selections:
        selected = [name for name in indices if any(matches(name, s) for s in selection['selectors'])]
        if not set(selection['required_indices']) <= set(selected):
            raise EscrowError('prepared native selection omits required consumer state')
        records.append({'application': selection['application'], 'store': selection['store'],
                        'indices': sorted(selected)})
    inventory = consumer_inventory(ledger, binding, backend, records, indices)
    ordered_queries = query_records(contracts, {'consumers': inventory['records']})
    query_digests = contract_digests(ordered_queries)
    if 'source_catalog' in binding:
        catalog = validate_catalog(binding['source_catalog'], binding)
        if (catalog['ledger_sha256'] != _digest(_encoded(ledger))
                or _encoded(catalog['consumers']) != _encoded(inventory['records'])
                or set(catalog['indices']) != set(indices)):
            raise EscrowError('prepared consumer catalog differs from bound evidence')
    if 'source_queries' in binding:
        queries = validate_queries(binding['source_queries'], binding)
        if _encoded(queries['contracts']) != _encoded(query_digests):
            raise EscrowError('prepared consumer query contracts differ from bound evidence')
    return {'consumer_inventory_sha256': inventory['sha256'],
            'selection_contracts_sha256': _digest(_encoded(selections)),
            'query_contracts_sha256': _digest(_encoded(query_digests)),
            'production_recovery_accepted': False}


def preflight_original_export(binding, *, ledger, backend, consumer_authority, contracts, indices):
    if not isinstance(consumer_authority, ConsumerAuthority):
        raise EscrowError('concrete original consumer authority adapter required')
    if 'source_catalog' not in binding or 'source_queries' not in binding:
        raise EscrowError('complete bound original catalog and query evidence required')
    ConsumerAuthority(consumer_authority.plans, require_authority=lambda _: False)
    # Placeholder lifetimes only validate reviewed plans before observing them.
    # The execution path replaces these with fresh complete authority contracts.
    selections = [consumer_authority._contract(p, {'pod_uid': 'preflight',
        'container_id': 'preflight', 'started_at': 'preflight', 'restart_count': 0})
        for p in consumer_authority.plans]
    return preflight_consumer_export(binding, ledger=ledger, backend=backend,
        selections=selections, contracts=contracts, indices=indices)
