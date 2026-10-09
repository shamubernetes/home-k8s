"""Read-only original consumer roster, release and process-selection checks.

Explicit authority is required before process reads. Registry labels and reviewed
source-file digests qualify a release, not the semantics of arbitrary source code.
Complete observed process lifetimes and selected startup environment stay bound.
This is not an all-writer fence or proof of application-loaded configuration.
"""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

from kopiur_elasticsearch_escrow import EscrowError, _digest, _encoded
from kopiur_elasticsearch_resolution import validate_contracts
from kopiur_elasticsearch_source_witness import validate_witness


PROFILES = {
    'media/tubearchivist': {'namespace': 'media', 'image_repository': 'bbilly1/tubearchivist',
        'deployments': {'tubearchivist': 'app'}, 'interpreter': 'python3', 'variable': None},
    'services/zoo-cowbell': {'namespace': 'services', 'image_repository': 'ghcr.io/thezoo-house/cowbell',
        'deployments': {'zoo-cowbell': 'cowbell', 'zoo-cowbell-worker': 'worker',
                        'zoo-cowbell-dispatcher': 'dispatcher'},
        'interpreter': 'bun', 'variable': 'CATALOG_INDEX_PREFIX'},
}

# Read all processes in the container, except this direct exec reader. Never
# return cmdlines, the rest of environ, credentials, or parser diagnostics.
PYTHON_PROJECTION = r'''
import json,os,pathlib,sys
try:
 root=pathlib.Path('/proc'); own=str(os.getpid()); key=json.load(sys.stdin)['variable']
 if key not in (None,'CATALOG_INDEX_PREFIX'): raise ValueError()
 def pids(): return sorted(p.name for p in root.iterdir() if p.name.isdecimal() and p.name!=own)
 ids=pids(); records=[]
 for pid in ids:
  p=root/pid; before=(p/'stat').read_bytes(); env=(p/'environ').read_bytes()
  fields=before[before.rfind(b')')+2:].split(); ticks=int(fields[19])
  after=(p/'stat').read_bytes(); after_fields=after[after.rfind(b')')+2:].split()
  if fields[19]!=after_fields[19] or after_fields[0] in (b'Z',b'X'): raise ValueError()
  if fields[0] in (b'Z',b'X') or not env.endswith(b'\0'): raise ValueError()
  pairs=[v.split(b'=',1) for v in env[:-1].split(b'\0')]
  names=[v[0] for v in pairs]
  if any(len(v)!=2 or not v[0] for v in pairs) or len(names)!=len(set(names)): raise ValueError()
  values=[v.decode('utf-8') for k,v in pairs if key is not None and k==key.encode()]
  records.append({'pid':int(pid),'start_ticks':str(ticks),'selection':values[0] if values else None})
 if ids!=pids(): raise ValueError()
 for record in records:
  raw=(root/str(record['pid'])/'stat').read_bytes(); fields=raw[raw.rfind(b')')+2:].split()
  if fields[19].decode()!=record['start_ticks'] or fields[0] in (b'Z',b'X'): raise ValueError()
 print(json.dumps({'boot_id':(root/'sys/kernel/random/boot_id').read_text().strip(),'processes':records}))
except Exception:
 sys.stderr.write('consumer process projection failed\n');sys.exit(1)
'''

JAVASCRIPT_PROJECTION = r'''
import fs from 'node:fs';
try {
 const {variable}=JSON.parse(fs.readFileSync(0,'utf8'));
 if(variable!==null&&variable!=='CATALOG_INDEX_PREFIX')throw Error();
 const pids=()=>fs.readdirSync('/proc').filter(p=>/^\d+$/.test(p)&&Number(p)!==process.pid).sort();
 const ids=pids(), records=[];
 for(const pid of ids){
  const root='/proc/'+pid, before=fs.readFileSync(root+'/stat');
  const env=fs.readFileSync(root+'/environ');
  const fields=before.toString().slice(before.toString().lastIndexOf(')')+2).trim().split(/\s+/);
  const after=fs.readFileSync(root+'/stat').toString();
  const afterFields=after.slice(after.lastIndexOf(')')+2).trim().split(/\s+/);
  if(fields[19]!==afterFields[19]||['Z','X'].includes(afterFields[0]))throw Error();
  if(['Z','X'].includes(fields[0])||!/^\d+$/.test(fields[19])||env.at(-1)!==0)throw Error();
  const names=new Set();let selection=null;
  for(const entry of env.subarray(0,env.length-1).toString('utf8').split('\0')){
   const at=entry.indexOf('='), name=entry.slice(0,at);
   if(at<1||names.has(name))throw Error();names.add(name);
   if(name===variable){selection=entry.slice(at+1);if(selection.includes('\uFFFD'))throw Error();}
  }
  records.push({pid:Number(pid),start_ticks:fields[19],selection});
 }
 if(JSON.stringify(ids)!==JSON.stringify(pids()))throw Error();
 for(const record of records){
  const raw=fs.readFileSync('/proc/'+record.pid+'/stat','utf8');
  const fields=raw.slice(raw.lastIndexOf(')')+2).trim().split(/\s+/);
  if(fields[19]!==record.start_ticks||['Z','X'].includes(fields[0]))throw Error();
 }
 console.log(JSON.stringify({boot_id:fs.readFileSync('/proc/sys/kernel/random/boot_id','utf8').trim(),processes:records}));
}catch{console.error('consumer process projection failed');process.exit(1);}
'''


def projection_command(interpreter):
    if interpreter == 'python3':
        return ['python3', '-c', PYTHON_PROJECTION]
    if interpreter == 'bun':
        return ['bun', '--no-install', '--no-env-file', '--eval', JAVASCRIPT_PROJECTION]
    raise EscrowError('qualified consumer process interpreter required')


def process_projection(value, *, expected_selection):
    if (not isinstance(value, dict) or set(value) != {'boot_id', 'processes'}
            or not isinstance(value['boot_id'], str)
            or not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['boot_id'])
            or not isinstance(value['processes'], list) or not value['processes']):
        raise EscrowError('complete consumer process projection required')
    pids = set()
    for item in value['processes']:
        if (not isinstance(item, dict) or set(item) != {'pid', 'start_ticks', 'selection'}
                or type(item['pid']) is not int or item['pid'] < 1 or item['pid'] in pids
                or not isinstance(item['start_ticks'], str)
                or not re.fullmatch(r'[1-9][0-9]*', item['start_ticks'])
                or item['selection'] is not None and not isinstance(item['selection'], str)):
            raise EscrowError('consumer process lifetime invalid')
        if item['selection'] != expected_selection:
            raise EscrowError('consumer startup selection differs from reviewed contract')
        pids.add(item['pid'])
    if 1 not in pids:
        raise EscrowError('consumer init process missing')
    return {'boot_id': value['boot_id'], 'processes': sorted(copy.deepcopy(value['processes']),
                                                           key=lambda p: p['pid'])}


class ConsumerAuthority:
    """Bind all original query/worker/dispatcher replicas, never just one Pod.

    Plans are reviewed inputs: application/store, declared_image, runtime image, source URL,
    revision, checkout, source_files {relative path: sha256}, selectors,
    required_indices, expected_selection and default_selection. Without OCI labels,
    only a complete reviewed source_witness may qualify selected TubeArchivist bytes.
    The original source capture grant must explicitly include these process reads.
    """
    def __init__(self, plans, *, require_authority, run=None):
        self.plans = copy.deepcopy(plans)
        self.require_authority = require_authority
        self.run = run or self._run
        self.expected = None
        apps = set()
        if not isinstance(plans, list) or not plans:
            raise EscrowError('reviewed original consumer plans required')
        for plan in plans:
            if (not isinstance(plan, dict) or set(plan) - {'source_witness'} != {'application', 'store', 'image', 'declared_image',
                    'source_url', 'source_revision', 'source_checkout', 'source_files',
                    'selectors', 'required_indices', 'expected_selection', 'default_selection'}
                    or not isinstance(plan['application'], str)
                    or plan['application'] not in PROFILES or plan['application'] in apps
                    or not isinstance(plan['image'], str)
                    or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', plan['image'])
                    or not isinstance(plan['declared_image'], str)
                    or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', plan['declared_image'])
                    or plan['declared_image'].split('@')[1] != plan['image'].split('@')[1]
                    or not isinstance(plan['source_revision'], str)
                    or not re.fullmatch(r'[0-9a-f]{40}', plan['source_revision'])
                    or not isinstance(plan['source_url'], str)
                    or not re.fullmatch(r'https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', plan['source_url'])
                    or not isinstance(plan['source_checkout'], str) or not plan['source_checkout'].startswith('/')
                    or not isinstance(plan['source_files'], dict) or not plan['source_files']):
                raise EscrowError('reviewed original consumer release plan invalid')
            for path, digest in plan['source_files'].items():
                if (not isinstance(path, str) or path.startswith('/')
                        or any(x in ('', '.', '..') for x in path.split('/'))
                        or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                    raise EscrowError('reviewed consumer source-file binding invalid')
            if 'source_witness' in plan:
                validate_witness(plan)
            if PROFILES[plan['application']]['variable'] is None and plan['expected_selection'] is not None:
                raise EscrowError('fixed-source consumer cannot supply environment selection')
            if plan['expected_selection'] is not None and not isinstance(plan['expected_selection'], str):
                raise EscrowError('explicit reviewed startup selection required')
            if plan['application'] == 'services/zoo-cowbell':
                if (not isinstance(plan['default_selection'], str)
                        or not {'web/src/config/runtime.ts', 'web/src/catalog/model.ts',
                                'web/src/catalog/elasticsearch.ts'} <= set(plan['source_files'])):
                    raise EscrowError('reviewed Cowbell runtime and generation source coverage required')
                prefix = (plan['expected_selection'] or '').strip() or plan['default_selection']
                if (not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*', prefix)
                        or plan['selectors'] != [prefix, prefix + '-state']
                        or plan['required_indices'] != plan['selectors']):
                    raise EscrowError('Cowbell startup prefix and generation-state selection differ')
            elif plan['default_selection'] is not None:
                raise EscrowError('fixed-source consumer cannot supply default environment selection')
            # Reuse selection schema qualification before any release/process I/O.
            validate_contracts([self._contract(plan, {'pod_uid': 'preflight', 'container_id': 'preflight',
                'started_at': 'preflight', 'restart_count': 0})])
            apps.add(plan['application'])

    @staticmethod
    def _run(argv, *, data=None):
        try:
            result = subprocess.run(argv, input=data, capture_output=True, timeout=60, check=False)
            if result.returncode == 0:
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise EscrowError('consumer authority read failed') from None

    def _raw_read(self, argv, *, data=None):
        self._authorized()
        try:
            value = self.run(argv, data=data)
            self._authorized()
            if isinstance(value, bytes) and len(value) <= 8 * 1024 * 1024:
                return value
        except Exception:
            pass
        raise EscrowError('consumer authority observation failed') from None

    def _read(self, argv, *, data=None):
        value = self._raw_read(argv, data=data)
        try:
            def unique(pairs):
                result = {}
                for k, v in pairs:
                    if k in result:
                        raise ValueError()
                    result[k] = v
                return result
            return json.loads(value, object_pairs_hook=unique)
        except Exception:
            raise EscrowError('consumer authority observation failed') from None

    def _authorized(self):
        try:
            if self.require_authority(copy.deepcopy(self.plans)) is True:
                return
        except Exception:
            pass
        raise EscrowError('explicit original consumer read authority required') from None

    def with_checkpoint(self, checkpoint):
        """Compose the source fence/grant with consumer authority at every I/O."""
        def authorized(plans):
            if checkpoint() is not True:
                return False
            self._authorized()
            return checkpoint() is True
        return ConsumerAuthority(self.plans, require_authority=authorized, run=self.run)

    def release(self, plan):
        config = self._read(['crane', 'config', plan['image']])
        labels = config.get('config', {}).get('Labels', {})
        if labels is None:
            labels = {}
        labelled = (isinstance(labels, dict)
                    and labels.get('org.opencontainers.image.source') == plan['source_url']
                    and labels.get('org.opencontainers.image.revision') == plan['source_revision'])
        witness = validate_witness(plan) if 'source_witness' in plan else None
        if (not isinstance(labels, dict) or any(k in labels and labels[k] != plan[field]
                for k, field in [('org.opencontainers.image.source', 'source_url'),
                                  ('org.opencontainers.image.revision', 'source_revision')])
                or not labelled and witness is None):
            raise EscrowError('immutable consumer image source provenance absent or different')
        for path, digest in plan['source_files'].items():
            data = self._raw_read(['git', '-C', plan['source_checkout'], 'show',
                                  plan['source_revision'] + ':' + path])
            if _digest(data) != digest:
                raise EscrowError('reviewed consumer source bytes differ')
        if witness is not None:
            observed = self._read([sys.executable, str(Path(__file__).with_name(
                'kopiur_elasticsearch_source_witness.py')), '--image', plan['image']])
            if _encoded(observed) != _encoded(witness):
                raise EscrowError('immutable artifact source bytes differ from reviewed witness')
        result = {key: copy.deepcopy(plan[key]) for key in
                  ('image', 'declared_image', 'source_url', 'source_revision', 'source_files')}
        result['source_evidence'] = ('selected-artifact-bytes' if witness is not None else 'oci-labels')
        if witness is not None:
            result['source_witness_sha256'] = _digest(_encoded(plan['source_witness']))
        return result

    def roster(self, plan):
        profile = PROFILES[plan['application']]
        namespace = profile['namespace']
        def get(kind):
            # Inspect the namespace, not only expected workload labels. Additional
            # deployments, orphan pods and old image digests must not disappear.
            return self._read(['kubectl', 'get', kind, '-n', namespace, '-o', 'json'])['items']
        def relevant(resource, *, deployment=False):
            meta = resource['metadata']
            spec = resource['spec']['template']['spec'] if deployment else resource['spec']
            prefix = 'zoo-cowbell' if plan['application'] == 'services/zoo-cowbell' else 'tubearchivist'
            label = meta.get('labels', {}).get('app.kubernetes.io/name', '')
            images = [c['image'].removeprefix('docker.io/').split('@')[0].split(':')[0]
                      for c in spec.get('containers', []) + spec.get('initContainers', [])]
            return (meta['name'] == prefix or meta['name'].startswith(prefix + '-')
                    or label == prefix.removeprefix('zoo-')
                    or label.startswith(prefix.removeprefix('zoo-') + '-')
                    or profile['image_repository'] in images)
        try:
            deployments, replicasets, pods = get('deployments'), get('replicasets'), get('pods')
            wanted = profile['deployments']
            selected = [d for d in deployments if relevant(d, deployment=True)]
            if {d['metadata']['name'] for d in selected} != set(wanted) or len(selected) != len(wanted):
                raise EscrowError('complete original consumer deployment roster required')
            result = []
            seen = set()
            for d in selected:
                meta, spec, status = d['metadata'], d['spec'], d['status']
                count = spec['replicas']; container = wanted[meta['name']]
                if (meta.get('deletionTimestamp') or type(count) is not int or count < 1
                        or status['observedGeneration'] != meta['generation']
                        or any(status.get(k, 0) != count for k in
                               ('replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas'))
                        or status.get('unavailableReplicas', 0)):
                    raise EscrowError('stable complete consumer replicas required')
                labels = spec['selector']['matchLabels']
                if spec['selector'].get('matchExpressions'):
                    raise EscrowError('qualified consumer deployment selector required')
                members = [p for p in pods if all(p['metadata'].get('labels', {}).get(k) == v
                                                for k, v in labels.items())]
                if len(members) != count:
                    raise EscrowError('complete consumer pod roster changed')
                for pod in members:
                    m = pod['metadata']; owner = self._owner(m, 'ReplicaSet')
                    if m['uid'] in seen:
                        raise EscrowError('consumer pod selected by multiple controllers')
                    seen.add(m['uid'])
                    rs = [r for r in replicasets if r['metadata']['uid'] == owner['uid']]
                    if (len(rs) != 1 or self._owner(rs[0]['metadata'], 'Deployment')['uid'] != meta['uid']
                            or m.get('deletionTimestamp') or pod['status']['phase'] != 'Running'
                            or pod['spec'].get('shareProcessNamespace')
                            or len(pod['spec']['containers']) != 1
                            or pod['spec']['containers'][0]['name'] != container
                            or pod['spec']['containers'][0]['image'] != plan['declared_image']):
                        raise EscrowError('consumer pod release/ownership differs')
                    c = pod['status']['containerStatuses']
                    if (len(c) != 1 or c[0]['name'] != container or c[0]['ready'] is not True
                            or c[0]['imageID'] != plan['image']
                            or not re.fullmatch(r'containerd://[0-9a-f]{64}', c[0]['containerID'])
                            or type(c[0]['restartCount']) is not int or c[0]['restartCount'] < 0):
                        raise EscrowError('consumer runtime image/lifetime differs')
                    result.append({'deployment': meta['name'], 'deployment_uid': meta['uid'],
                        'generation': meta['generation'], 'template_sha256': _digest(_encoded(spec['template'])),
                        'pod': m['name'], 'pod_uid': m['uid'], 'container': container,
                        'container_id': c[0]['containerID'], 'started_at': c[0]['state']['running']['startedAt'],
                        'restart_count': c[0]['restartCount']})
            for pod in pods:
                if relevant(pod) and pod['metadata']['uid'] not in seen:
                    states = pod.get('status', {}).get('containerStatuses', [])
                    if (pod.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
                            or not states or any('terminated' not in s.get('state', {}) for s in states)):
                        raise EscrowError('unmatched active original consumer pod')
            return sorted(result, key=lambda r: (r['deployment'], r['pod_uid']))
        except EscrowError:
            raise
        except Exception:
            raise EscrowError('consumer roster observation incomplete') from None

    @staticmethod
    def _owner(meta, kind):
        owners = [o for o in meta.get('ownerReferences', []) if o.get('controller') is True]
        if len(owners) != 1 or owners[0]['kind'] != kind:
            raise EscrowError('exact consumer controller ownership required')
        return owners[0]

    def observe(self):
        self._authorized()
        result = []
        for plan in self.plans:
            release = self.release(plan)  # Missing provenance denies process reads.
            roster = self.roster(plan)
            profile = PROFILES[plan['application']]
            processes = []
            for member in roster:
                self._authorized()
                if _encoded(self.roster(plan)) != _encoded(roster):
                    raise EscrowError('consumer roster changed before process read')
                raw = self._read(['kubectl', 'exec', '-n', profile['namespace'], member['pod'],
                    '-c', member['container'], '--stdin', '--', *projection_command(profile['interpreter'])],
                    data=_encoded({'variable': profile['variable']}))
                self._authorized()
                if _encoded(self.roster(plan)) != _encoded(roster):
                    raise EscrowError('consumer roster changed during process read')
                processes.append({'pod_uid': member['pod_uid'], 'projection': process_projection(raw,
                                    expected_selection=plan['expected_selection'])})
            result.append({'application': plan['application'], 'release': release,
                           'roster': roster, 'processes': processes})
        self._authorized()
        return sorted(result, key=lambda r: r['application'])

    @staticmethod
    def _contract(plan, runtime):
        return {'application': plan['application'], 'store': plan['store'],
                'selectors': copy.deepcopy(plan['selectors']), 'required_indices': copy.deepcopy(plan['required_indices']),
                'release': {'image': plan['image'], 'source_revision': plan['source_revision']},
                'runtime': {**runtime, 'selection_sha256': _digest(_encoded({
                    'selectors': plan['selectors'], 'required_indices': plan['required_indices']}))}}

    def prepare(self):
        first = self.observe()
        if _encoded(self.observe()) != _encoded(first):
            raise EscrowError('consumer release/process authority changed during preparation')
        self.expected = copy.deepcopy(first)
        return self.contracts()

    def contracts(self):
        if self.expected is None:
            raise EscrowError('consumer authority not prepared')
        by_app = {r['application']: r for r in self.expected}
        return validate_contracts([self._contract(p, {k: by_app[p['application']]['roster'][0][k]
            for k in ('pod_uid', 'container_id', 'started_at', 'restart_count')}) for p in self.plans])

    def require(self, contracts):
        if _encoded(validate_contracts(contracts)) != _encoded(self.contracts()):
            raise EscrowError('consumer selection contract differs from prepared authority')
        if _encoded(self.observe()) != _encoded(self.expected):
            raise EscrowError('consumer release/process authority changed')
        return True

    def receipt(self):
        if self.expected is None:
            raise EscrowError('consumer authority not prepared')
        return {'consumer_authority_sha256': _digest(_encoded(self.expected)),
                'application_count': len(self.expected),
                'container_count': sum(len(r['roster']) for r in self.expected),
                'startup_selection_verified': True, 'application_loaded_selection_verified': False,
                'all_writer_fence_verified': False, 'production_recovery_accepted': False}
