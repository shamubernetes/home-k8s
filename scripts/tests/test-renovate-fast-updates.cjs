'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const Module = require('node:module');
const sourcePath = path.join(root, 'kubernetes/apps/renovate/renovate/app/config/fast-update-runner.cjs');
const compiled = new Module(sourcePath);
compiled.filename = sourcePath;
// Match Flux's literal-dollar rendering before exercising runtime paths.
compiled._compile(fs.readFileSync(sourcePath, 'utf8').replaceAll('$${', '${'), sourcePath);
const runner = compiled.exports;
const preset = JSON.parse(fs.readFileSync(path.join(root, 'kubernetes/apps/renovate/renovate/app/config/fast-updates.json')));

test('one canonical preset excludes the exact image/file pair from regular Renovate', () => {
  const targets = runner.targetsFromPreset(preset);
  assert.deepEqual(targets, [{ file: 'kubernetes/apps/services/kanvil/app/helmrelease.yaml', image: 'ghcr.io/maudecode/kanvil' }]);
  const source = fs.readFileSync(path.join(root, '.github/renovate.json5'), 'utf8');
  assert(source.includes('github>shamubernetes/home-k8s//kubernetes/apps/renovate/renovate/app/config/fast-updates.json'));
});

test('fast configuration ignores repository presets and denies unselected dependencies', () => {
  const targets = runner.targetsFromPreset(preset), config = runner.buildConfig(targets);
  assert.deepEqual(config.repositories, ['shamubernetes/home-k8s']);
  assert.equal(config.requireConfig, 'ignored');
  assert.equal(config.inheritConfig, false);
  assert.deepEqual(config.enabledManagers, ['helm-values']);
  assert.deepEqual(config.includePaths, [targets[0].file]);
  assert.deepEqual(config.packageRules[0], { matchPackageNames: ['*'], enabled: false });
  assert.equal(config.packageRules[1].enabled, true);
  assert.deepEqual(config.packageRules[1].matchPackageNames, [targets[0].image]);
  assert.deepEqual(config.packageRules[1].matchFileNames, [targets[0].file]);
  assert.equal(config.automerge, false, 'Only the maintenance-guarded worker may merge');
  assert.equal(config.platformAutomerge, false);
  assert.equal(config.branchPrefix, 'fast-update/');
  assert.equal(config.minimumReleaseAge, null);
  assert.equal(config.branchPrefixOld, config.branchPrefix);
  assert.equal(config.cachePrivatePackages, false);
  assert.equal(config.cacheHardTtlMinutes, 0);
  assert.deepEqual(config.cacheTtlOverride, {
    'datasource-docker-releases-v2': 0,
    'datasource-docker-tags': 0,
    'datasource-docker-digest': 0
  });
  assert.deepEqual(config.allowedUnsafeExecutions, []);
});

test('another application is enabled by adding one allowlist rule', () => {
  const second = { description: 'Another app', matchDatasources: ['docker'], matchFileNames: ['kubernetes/apps/tools/example/app/helmrelease.yaml'], matchPackageNames: ['ghcr.io/example/app'], enabled: false };
  const targets = runner.targetsFromPreset({ packageRules: [...preset.packageRules, second] });
  const config = runner.buildConfig(targets);
  assert.equal(targets.length, 2);
  assert.equal(config.packageRules.length, 3);
  assert.deepEqual(config.includePaths, [preset.packageRules[0].matchFileNames[0], second.matchFileNames[0]]);
});

test('wildcard, duplicate, enabled, non-image and out-of-scope targets fail closed', () => {
  for (const changed of [
    { matchFileNames: ['kubernetes/**'] },
    { matchPackageNames: ['*'] },
    { matchFileNames: ['../../other/helmrelease.yaml'] },
    { enabled: true },
    { matchDatasources: ['npm'] },
    { matchPackageNames: ['ghcr.io/../private'] },
    { matchFileNames: [...preset.packageRules[0].matchFileNames, 'another.yaml'] },
    { unexpected: 'field' }
  ]) assert.throws(() => runner.targetsFromPreset({ packageRules: [{ ...preset.packageRules[0], ...changed }] }));
  assert.throws(() => runner.targetsFromPreset({ packageRules: [] }));
  assert.throws(() => runner.targetsFromPreset({ packageRules: [...preset.packageRules, ...preset.packageRules] }));
});

const oldPin = `-              tag: 0.1.2@sha256:${'a'.repeat(64)}`;
const newPin = `+              tag: 0.1.3@sha256:${'b'.repeat(64)}`;
test('only a stable tag-and-digest replacement can pass the merge boundary', () => {
  const file = preset.packageRules[0].matchFileNames[0];
  assert(runner.pinOnlyPatch(file, `@@ -1,3 +1,3 @@\n image:\n${oldPin}\n${newPin}`));
  for (const patch of [null, oldPin, `${oldPin}\n${newPin}\n+  evil: true`, `${oldPin}\n+              tag: latest`, `${oldPin}\n+              tag: 0.1.3-rc.1@sha256:${'b'.repeat(64)}`]) assert.equal(runner.pinOnlyPatch(file, patch), false);
  assert.equal(runner.pinOnlyPatch('README.md', `${oldPin}\n${newPin}`), false);
});

test('semantic merge guard rejects other images, config edits and downgrades', () => {
  const image = 'ghcr.io/maudecode/kanvil';
  const before = { spec: { values: { image: { repository: image, tag: '0.1.2@sha256:' + 'a'.repeat(64) }, sidecar: { repository: 'ghcr.io/example/sidecar', tag: '1.0.0@sha256:' + 'c'.repeat(64) }, env: { MODE: 'production' } } } };
  const after = structuredClone(before);
  after.spec.values.image.tag = '0.1.3@sha256:' + 'b'.repeat(64);
  assert(runner.imageOnlyChange(before, after, image));
  const repin = structuredClone(before);
  repin.spec.values.image.tag = '0.1.2@sha256:' + 'b'.repeat(64);
  assert(runner.imageOnlyChange(before, repin, image));
  const other = structuredClone(before);
  other.spec.values.sidecar.tag = '1.0.1@sha256:' + 'd'.repeat(64);
  assert.equal(runner.imageOnlyChange(before, other, image), false);
  const config = structuredClone(after);
  config.spec.values.env.MODE = 'test';
  assert.equal(runner.imageOnlyChange(before, config, image), false);
  for (const pin of ['0.1.1@sha256:' + 'b'.repeat(64), 'latest', '0.1.3-rc.1@sha256:' + 'b'.repeat(64)]) {
    const bad = structuredClone(before); bad.spec.values.image.tag = pin;
    assert.equal(runner.imageOnlyChange(before, bad, image), false);
  }
  assert.equal(runner.imageOnlyChange(before, before, image), false);
});

test('runtime Kubernetes client references the projected identity, never localhost or a copied token', () => {
  const config = runner.inClusterConfig('10.96.0.1', '443');
  assert.equal(config.clusters[0].cluster.server, 'https://10.96.0.1:443');
  assert.equal(config.clusters[0].cluster['certificate-authority'], '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt');
  assert.deepEqual(config.users[0].user, { tokenFile: '/var/run/secrets/kubernetes.io/serviceaccount/token' });
  assert.equal(config['current-context'], config.contexts[0].name);
  assert.equal(runner.inClusterConfig('fd00::1', '443').clusters[0].cluster.server, 'https://[fd00::1]:443');
  const cron = fs.readFileSync(path.join(root, 'kubernetes/apps/renovate/renovate/app/fast-update-cronjob.yaml'), 'utf8');
  assert(cron.includes('preferredDuringSchedulingIgnoredDuringExecution:'));
  assert(!cron.includes('requiredDuringSchedulingIgnoredDuringExecution:'));
  assert(cron.includes('imagePullPolicy: IfNotPresent'));
});

test('maintenance admission requires two active members and ten minutes remaining', () => {
  const now = 1000000;
  const status = { silenceId: 'owned', state: 'active', memberStates: { a: 'active', b: 'active' }, endsAt: new Date(now + 1800000).toISOString() };
  assert(runner.maintenanceActive(status, 'owned', now));
  for (const changed of [
    { state: 'expired' }, { silenceId: 'other' }, { memberStates: { a: 'active', b: 'expired' } },
    { memberStates: { a: 'active' } }, { endsAt: new Date(now + 599999).toISOString() }, { endsAt: 'invalid' }
  ]) assert.equal(runner.maintenanceActive({ ...status, ...changed }, 'owned', now), false);
});

function rolloutFixture(revision = 'a'.repeat(40)) {
  const merge = 'a'.repeat(40), image = 'ghcr.io/maudecode/kanvil', pin = '0.1.3@sha256:' + 'b'.repeat(64);
  const target = { file: 'kubernetes/apps/services/kanvil/app/helmrelease.yaml', image };
  const hr = { metadata: { name: 'kanvil' }, spec: { values: { controllers: { kanvil: { containers: { app: { image: { repository: image, tag: pin }, probes: { readiness: { spec: { httpGet: { port: 5180, path: '/healthz' } } } } } } } } } } };
  const ready = { metadata: { generation: 1 }, status: { conditions: [{ type: 'Ready', status: 'True', observedGeneration: 1 }] } };
  let clock = 0, apiCalls = 0, healthCalls = 0;
  const options = {
    read: async () => JSON.stringify(hr),
    observe: async kind => {
      if (kind.startsWith('gitrepositories')) return { status: { artifact: { revision: 'main@sha1:' + revision } } };
      if (kind.startsWith('kustomizations')) return { ...ready, spec: { path: './kubernetes/apps/services/kanvil/app', targetNamespace: 'services' }, status: { ...ready.status, lastAppliedRevision: 'main@sha1:' + revision } };
      if (kind.startsWith('helmreleases')) return ready;
      if (kind === 'deployments') return { items: [{ metadata: { generation: 1 }, spec: { replicas: 1, template: { spec: { containers: [{ image: image + ':' + pin }] } } }, status: { observedGeneration: 1, availableReplicas: 1, updatedReplicas: 1 } }] };
      throw new Error('Unexpected resource');
    },
    api: async () => { apiCalls++; return { status: 'ahead', merge_base_commit: { sha: merge } }; },
    request: async url => { healthCalls++; assert.equal(url, 'http://kanvil.services.svc.cluster.local:5180/healthz'); return { ok: true, status: 200 }; },
    now: () => clock, pause: async ms => { clock += ms; }, timeoutMs: 20, intervalMs: 5
  };
  return { hr, target, merge, options, run: () => runner.verifyRollout(target, merge, 'synthetic-unit-token', { parse: JSON.parse }, options), apiCalls: () => apiCalls, healthCalls: () => healthCalls };
}

test('rollout accepts the exact merged revision with expected image and health', async () => {
  const f = rolloutFixture(), observe = f.options.observe, calls = [];
  f.options.observe = async (...args) => { calls.push(args); return observe(...args); };
  const result = await f.run();
  assert(calls.some(args => args[0].startsWith('kustomizations') && args[2] === 'kanvil'));
  assert(calls.some(args => args[0] === 'deployments' && args[3] === 'app.kubernetes.io/instance=kanvil'));
  assert.equal(result.revision, f.merge);
  assert.equal(result.health, 200);
  assert.equal(f.apiCalls(), 0);
  assert.equal(f.healthCalls(), 1);
});

test('rollout accepts a verified descendant only when its target pin is unchanged', async () => {
  const later = 'c'.repeat(40), f = rolloutFixture(later), result = await f.run();
  assert.equal(result.appliedRevision, 'main@sha1:' + later);
  assert.equal(result.revision, f.merge);
  assert.equal(f.apiCalls(), 1, 'Immutable ancestry proof is cached across source and Kustomization');
});

test('rollout rejects divergent history and a descendant that changes the pin', async () => {
  const divergent = rolloutFixture('c'.repeat(40));
  divergent.options.api = async () => ({ status: 'diverged', merge_base_commit: { sha: 'd'.repeat(40) } });
  await assert.rejects(divergent.run(), /timed out.*Flux revisions/);
  assert.equal(divergent.healthCalls(), 0);
  const changed = rolloutFixture('c'.repeat(40)), later = structuredClone(changed.hr);
  later.spec.values.controllers.kanvil.containers.app.image.tag = '0.1.4@sha256:' + 'd'.repeat(64);
  changed.options.read = async (_file, sha) => JSON.stringify(sha === changed.merge ? changed.hr : later);
  await assert.rejects(changed.run(), /timed out.*Flux revisions/);
  assert.equal(changed.healthCalls(), 0);
});

test('rollout retries a transient Kubernetes read failure', async () => {
  const f = rolloutFixture(), observe = f.options.observe;
  let first = true;
  f.options.observe = async (...args) => { if (first) { first = false; const error = new Error('synthetic timeout'); error.code = 'ETIMEDOUT'; throw error; } return observe(...args); };
  assert.equal((await f.run()).health, 200);
});

test('rollout retries a transient merged-manifest read failure', async () => {
  const f = rolloutFixture(), read = f.options.read;
  let first = true;
  f.options.read = async (...args) => { if (first) { first = false; throw new Error('synthetic API timeout'); } return read(...args); };
  assert.equal((await f.run()).health, 200);
});

test('rollout retries HTTP transport and non-success observations', async () => {
  const f = rolloutFixture(); let attempts = 0;
  f.options.request = async () => {
    attempts++;
    if (attempts === 1) throw new TypeError('synthetic timeout');
    return attempts === 2 ? { ok: false, status: 503 } : { ok: true, status: 200 };
  };
  assert.equal((await f.run()).health, 200);
  assert.equal(attempts, 3);
});

test('rollout timeout retains safe last-observation diagnostics', async () => {
  const f = rolloutFixture();
  f.options.request = async () => ({ ok: false, status: 503 });
  await assert.rejects(f.run(), { name: 'RolloutVerificationTimeout', message: 'Rollout verification timed out (observe-health: HTTP 503)' });
});

test('unsupported health configuration fails before observation polling', async () => {
  const f = rolloutFixture();
  f.hr.spec.values.controllers.kanvil.containers.app.probes.readiness.spec.httpGet.port = 'http';
  f.options.observe = async () => assert.fail('must not poll malformed target');
  await assert.rejects(f.run(), /explicit HTTP readiness probe/);
});

test('every required exact-head GitHub Actions check must pass', () => {
  const sha = 'c'.repeat(40);
  const checks = runner.REQUIRED_CHECKS.map(name => ({ name, head_sha: sha, app: { id: 15368 }, status: 'completed', conclusion: 'success' }));
  assert(runner.checksPass(checks, sha));
  assert.equal(runner.checksPass(checks.slice(1), sha), false);
  assert.equal(runner.checksPass(checks, 'd'.repeat(40)), false);
  for (const changed of [{ app: { id: 1 } }, { conclusion: 'failure' }, { status: 'in_progress' }, { conclusion: 'skipped' }]) assert.equal(runner.checksPass([{ ...checks[0], ...changed }, ...checks.slice(1)], sha), false);
});
