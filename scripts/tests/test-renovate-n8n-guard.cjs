#!/usr/bin/env node
// Exercise Renovate's runtime filter, not just its config schema validator.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');

async function main() {
  const install = execFileSync('mise', ['where', 'npm:renovate'], { encoding: 'utf8' }).trim();
  const root = fs.realpathSync(path.join(install, 'node_modules', 'renovate'));
  const load = (name) => import(pathToFileURL(path.join(root, 'dist', name)).href);
  const { createRequire } = require('node:module');
  const renovateRequire = createRequire(path.join(root, 'package.json'));
  const json5 = renovateRequire('json5');
  const { api } = await load('modules/versioning/docker/index.js');
  const { filterVersions } = await load('workers/repository/process/lookup/filter.js');
  const config = json5.parse(fs.readFileSync(path.join(__dirname, '../../.github/renovate/packageRules.json5'), 'utf8'));
  const rules = config.packageRules.filter((rule) => rule.matchPackageNames?.includes('docker.io/n8nio/n8n') && rule.allowedVersions);
  assert.equal(rules.length, 0, 'n8n must not have a plugin-compatibility version exclusion');
  const versions = ['2.36.5', '2.37.4', '2.37.6', '2.37.10', '2.38.4'];
  const filter = (allowedVersions) => filterVersions(
    { versioning: 'docker', depName: 'docker.io/n8nio/n8n', allowedVersions },
    '2.36.4', null, versions.map((version) => ({ version })), api,
  ).map((release) => release.version);
  assert.throws(() => filter('!=2.37.4'), (error) => error.validationError === 'Invalid `allowedVersions`');
  assert.deepEqual(filter(undefined), versions);
  console.log('PASS: n8n has no version exclusions and Renovate runtime filtering accepts ordinary releases.');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
