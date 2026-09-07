#!/usr/bin/env node
// Exercise the real Renovate lookup with explicit, offline registry fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');

async function main() {
  const install = execFileSync('mise', ['where', 'npm:renovate'], { encoding: 'utf8' }).trim();
  const root = fs.realpathSync(path.join(install, 'node_modules', 'renovate'));
  const load = (name) => import(pathToFileURL(path.join(root, 'dist', name)).href);
  await (await load('logger/index.js')).init();
  const { createRequire } = require('node:module');
  const json5 = createRequire(path.join(root, 'package.json'))('json5');
  const repo = path.resolve(__dirname, '../..');
  const config = json5.parse(fs.readFileSync(path.join(repo, '.github/renovate/packageRules.json5'), 'utf8'));
  const rules = config.packageRules.filter((rule) => rule.matchPackageNames?.includes('docker.io/n8nio/n8n') && rule.allowedVersions);
  assert.equal(rules.length, 0, 'n8n must not need per-version exclusions');

  const packageFile = 'kubernetes/apps/services/n8n-operator/n8n/n8ninstance.yaml';
  const content = fs.readFileSync(path.join(repo, packageFile), 'utf8');
  const { extractPackageFile } = await load('modules/manager/kubernetes/index.js');
  const extracted = await extractPackageFile(content, packageFile, {});
  const deps = extracted.deps.filter((dep) => dep.depName === 'docker.io/n8nio/n8n');
  assert.equal(deps.length, 1, 'extract exactly one n8n dependency');
  assert.equal(deps[0].currentValue, 'stable', 'track the upstream stable channel, not numeric beta tags');
  assert.match(deps[0].currentDigest, /^sha256:[a-f0-9]{64}$/, 'keep an immutable digest pin');

  const { getDatasourceFor } = await load('modules/datasource/common.js');
  const { lookupUpdates } = await load('workers/repository/process/lookup/index.js');
  const datasource = getDatasourceFor('docker');
  const original = { getReleases: datasource.getReleases, getDigest: datasource.getDigest };
  const oldDigest = `sha256:${'a'.repeat(64)}`;
  const newStableDigest = `sha256:${'b'.repeat(64)}`;
  const requestedTags = [];
  // A fixture numeric beta exists, but stable-channel lookup must never enumerate it.
  datasource.getReleases = async () => { throw new Error('stable lookup must not enumerate numeric releases such as 2.38.4'); };
  let stableDigest = oldDigest;
  datasource.getDigest = async (_config, tag) => {
    requestedTags.push(tag);
    assert.equal(tag, 'stable', 'never request beta, next, latest, or a numeric tag');
    return stableDigest;
  };
  const input = {
    ...deps[0], packageName: deps[0].depName, manager: 'kubernetes', packageFile,
    versioning: 'docker', currentDigest: oldDigest, pinDigests: true,
    updatePinnedDependencies: true, minimumReleaseAge: '0 days',
  };
  const lookup = async () => {
    const { val, err } = (await lookupUpdates(input)).unwrap();
    assert.equal(err, undefined);
    assert.equal(val.skipReason, undefined);
    return val;
  };
  try {
    const unchanged = await lookup();
    assert.deepEqual(unchanged.warnings, []);
    assert.deepEqual(unchanged.updates, [], 'beta publication alone cannot create an update');
    stableDigest = newStableDigest;
    const promoted = await lookup();
    assert.deepEqual(promoted.warnings, []);
    assert.equal(promoted.updates.length, 1, 'a future stable promotion must remain eligible');
    assert.equal(promoted.updates[0].updateType, 'digest');
    assert.equal(promoted.updates[0].newValue, 'stable');
    assert.equal(promoted.updates[0].newDigest, newStableDigest);
    stableDigest = null;
    const missing = await lookup();
    assert.deepEqual(missing.updates, [], 'missing stable digest must not fall back to a beta');
    assert.equal(missing.warnings.length, 1, 'missing stable digest must be visible');
    assert.deepEqual(requestedTags, ['stable', 'stable', 'stable']);
  } finally {
    Object.assign(datasource, original);
  }
  console.log('PASS: pinned n8n stable channel rejects numeric beta discovery, accepts stable digest promotion, and fails closed on missing stable digest.');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
