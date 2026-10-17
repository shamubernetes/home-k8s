'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const runner = require(path.join(root, 'kubernetes/apps/renovate/renovate/app/config/fast-update-runner.cjs'));
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

test('every required exact-head GitHub Actions check must pass', () => {
  const sha = 'c'.repeat(40);
  const checks = runner.REQUIRED_CHECKS.map(name => ({ name, head_sha: sha, app: { id: 15368 }, status: 'completed', conclusion: 'success' }));
  assert(runner.checksPass(checks, sha));
  assert.equal(runner.checksPass(checks.slice(1), sha), false);
  assert.equal(runner.checksPass(checks, 'd'.repeat(40)), false);
  for (const changed of [{ app: { id: 1 } }, { conclusion: 'failure' }, { status: 'in_progress' }, { conclusion: 'skipped' }]) assert.equal(runner.checksPass([{ ...checks[0], ...changed }, ...checks.slice(1)], sha), false);
});
