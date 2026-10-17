'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createRequire } = require('node:module');

const REPOSITORY = 'shamubernetes/home-k8s';
const PREFIX = 'fast-update/';
const PRESET = 'kubernetes/apps/renovate/renovate/app/config/fast-updates.json';
const REQUIRED_CHECKS = ['Static Analysis - Success', 'Flate - Success', 'Talos Image Availability', 'Talos - Validate'];
const WORK = process.env.RENOVATE_BASE_DIR || '/tmp/renovate-fast';
const kubectl = process.env.KUBECTL_BIN || '/tools/kubectl';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const report = event => console.log(JSON.stringify(event));

function targetsFromPreset(preset) {
  assert(Array.isArray(preset.packageRules) && preset.packageRules.length > 0, 'Empty fast-update allowlist');
  const seen = new Set();
  return preset.packageRules.map(rule => {
    assert.deepEqual(Object.keys(rule).sort(), ['description', 'enabled', 'matchDatasources', 'matchFileNames', 'matchPackageNames'].sort());
    assert.equal(rule.enabled, false, 'Regular Renovate must exclude every fast target');
    assert.deepEqual(rule.matchDatasources, ['docker']);
    assert.equal(rule.matchFileNames.length, 1);
    assert.equal(rule.matchPackageNames.length, 1);
    const file = rule.matchFileNames[0], image = rule.matchPackageNames[0];
    assert(/^kubernetes\/apps\/[a-z0-9-]+\/[a-z0-9-]+\/app\/helmrelease\.yaml$/.test(file), 'Expected an exact application HelmRelease path');
    assert(/^ghcr\.io\/[a-z0-9._/-]+$/.test(image) && !image.includes('..'), 'Expected an exact public GHCR image');
    assert(!seen.has(file), 'One fast-update image per manifest');
    seen.add(file);
    return { file, image };
  });
}

function buildConfig(targets) {
  return {
    platform: 'github', repositories: [REPOSITORY], autodiscover: false,
    onboarding: false, requireConfig: 'ignored', inheritConfig: false, extends: [],
    enabledManagers: ['helm-values'], includePaths: targets.map(t => t.file),
    'helm-values': { managerFilePatterns: targets.map(t => t.file) },
    packageRules: [
      { matchPackageNames: ['*'], enabled: false },
      ...targets.map(t => ({ matchManagers: ['helm-values'], matchDatasources: ['docker'], matchFileNames: [t.file], matchPackageNames: [t.image], enabled: true, allowedVersions: '/^\\d+\\.\\d+\\.\\d+$/', versioning: 'semver' }))
    ],
    minimumReleaseAge: null, ignoreUnstable: true, pinDigests: true,
    separateMajorMinor: false, separateMinorPatch: false,
    vulnerabilityAlerts: { enabled: false },
    dependencyDashboard: false, dependencyDashboardTitle: 'Fast-update lane (dashboard disabled)',
    branchPrefix: PREFIX, branchPrefixOld: PREFIX,
    prCreation: 'immediate',
    pruneStaleBranches: true, prHourlyLimit: 0, prConcurrentLimit: 0,
    branchConcurrentLimit: 0, schedule: [], rebaseWhen: 'conflicted',
    automerge: false, platformAutomerge: false,
    reviewers: [], assignees: [], reviewersFromCodeOwners: false,
    assigneesFromCodeOwners: false, labels: ['fast-update'],
    semanticCommits: 'enabled', commitMessagePrefix: 'chore(fast-update):',
    gitAuthor: 'ShamuBot <148669015+shamubot[bot]@users.noreply.github.com>',
    baseDir: WORK, cacheDir: path.join(WORK, 'cache'), repositoryCache: 'disabled',
    cachePrivatePackages: false,
    cacheTtlOverride: { 'datasource-docker-releases-v2': 0, 'datasource-docker-tags': 0, 'datasource-docker-digest': 0 },
    cacheHardTtlMinutes: 0,
    allowedCommands: [], allowedEnv: [], allowScripts: false, allowPlugins: false,
    allowedUnsafeExecutions: []
  };
}

function pinOnlyPatch(file, patch) {
  if (!patch || !file.endsWith('/helmrelease.yaml')) return false;
  const changed = patch.split('\n').filter(line => /^[+-]/.test(line) && !/^(---|\+\+\+)/.test(line));
  return changed.length === 2 && changed[0].startsWith('-') && changed[1].startsWith('+') && changed.every(line => /^[+-]\s+tag: ['"]?\d+\.\d+\.\d+@sha256:[a-f0-9]{64}['"]?\s*$/.test(line));
}

function imageOnlyChange(before, after, image) {
  const find = (value, paths = [], prefix = []) => {
    if (!value || typeof value !== 'object') return paths;
    if (value.repository === image && typeof value.tag === 'string') paths.push(prefix);
    for (const [key, child] of Object.entries(value)) if (child && typeof child === 'object') find(child, paths, [...prefix, key]);
    return paths;
  };
  const positions = find(before);
  if (positions.length !== 1) return false;
  const keys = positions[0];
  const at = object => keys.reduce((value, key) => value?.[key], object);
  const oldImage = at(before), newImage = at(after);
  if (!newImage || newImage.repository !== image || oldImage.tag === newImage.tag) return false;
  const pin = /^(\d+)\.(\d+)\.(\d+)@sha256:[a-f0-9]{64}$/;
  const oldPin = pin.exec(oldImage.tag), newPin = pin.exec(newImage.tag);
  if (!oldPin || !newPin) return false;
  for (let i = 1; i <= 3; i++) {
    const previous = Number(oldPin[i]), next = Number(newPin[i]);
    if (!Number.isSafeInteger(previous) || !Number.isSafeInteger(next) || next < previous) return false;
    if (next > previous) break;
  }
  const expected = structuredClone(before);
  at(expected).tag = newImage.tag;
  try { assert.deepEqual(expected, after); return true; } catch { return false; }
}

function checksPass(checks, sha) {
  return REQUIRED_CHECKS.every(name => checks.some(c => c.name === name && c.head_sha === sha && c.app?.id === 15368 && c.status === 'completed' && c.conclusion === 'success'));
}

function kget(kind, namespace, name) {
  return JSON.parse(execFileSync(kubectl, ['-n', namespace, 'get', kind, ...(name ? [name] : []), '-o', 'json'], { timeout: 30000, encoding: 'utf8' }));
}

async function github(url, token, method = 'GET', body) {
  const response = await fetch(`https://api.github.com$${url}`, {
    method, headers: { Authorization: `Bearer $${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000)
  });
  if (!response.ok) throw new Error(`GitHub $${method} $${url.split('?')[0]} returned $${response.status}`);
  return response.status === 204 ? null : response.json();
}

async function pages(url, token, field) {
  const result = [];
  for (let page = 1; ; page++) {
    const data = await github(`$${url}$${url.includes('?') ? '&' : '?'}per_page=100&page=$${page}`, token);
    const rows = field ? data[field] : data;
    assert(Array.isArray(rows)); result.push(...rows);
    if (rows.length < 100) {
      if (field && Number.isInteger(data.total_count)) assert.equal(result.length, data.total_count, 'Incomplete GitHub pagination');
      return result;
    }
  }
}

async function installationToken() {
  const appId = process.env.MEND_RNV_GITHUB_APP_ID;
  const key = process.env.MEND_RNV_GITHUB_APP_KEY;
  assert(appId && key, 'Renovate GitHub App identity is missing');
  const now = Math.floor(Date.now() / 1000);
  const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const unsigned = `$${encode({ alg: 'RS256', typ: 'JWT' })}.$${encode({ iat: now - 60, exp: now + 540, iss: appId })}`;
  const jwt = `$${unsigned}.$${crypto.sign('RSA-SHA256', Buffer.from(unsigned), key).toString('base64url')}`;
  const app = await github('/app', jwt);
  const installation = await github(`/repos/$${REPOSITORY}/installation`, jwt);
  const issued = await github(`/app/installations/$${installation.id}/access_tokens`, jwt, 'POST', {
    repositories: ['home-k8s'], permissions: { contents: 'write', pull_requests: 'write', issues: 'write', checks: 'read' }
  });
  assert(issued.token && issued.repositories?.every(r => r.full_name === REPOSITORY));
  return { token: issued.token, author: `$${app.slug}[bot]` };
}

async function readFileAt(file, revision, token) {
  const record = await github(`/repos/$${REPOSITORY}/contents/$${file}?ref=$${encodeURIComponent(revision)}`, token);
  assert.equal(record.type, 'file'); assert.equal(record.encoding, 'base64');
  return Buffer.from(record.content, 'base64').toString('utf8');
}

async function readyPull(targets, token, author, yaml) {
  const pulls = await pages(`/repos/$${REPOSITORY}/pulls?state=open&base=main`, token);
  for (const summary of pulls.filter(p => p.head.ref.startsWith(PREFIX) && p.user.login === author && p.head.repo?.full_name === REPOSITORY)) {
    const pr = await github(`/repos/$${REPOSITORY}/pulls/$${summary.number}`, token);
    if (pr.draft || pr.mergeable !== true || pr.mergeable_state !== 'clean') continue;
    const files = await pages(`/repos/$${REPOSITORY}/pulls/$${pr.number}/files`, token);
    if (files.length !== 1) continue;
    const target = targets.find(t => t.file === files[0].filename);
    if (!target || files[0].status !== 'modified' || !pinOnlyPatch(target.file, files[0].patch)) continue;
    const before = yaml.parse(await readFileAt(target.file, pr.base.sha, token));
    const after = yaml.parse(await readFileAt(target.file, pr.head.sha, token));
    if (!imageOnlyChange(before, after, target.image)) continue;
    const checks = await pages(`/repos/$${REPOSITORY}/commits/$${pr.head.sha}/check-runs?filter=latest`, token, 'check_runs');
    if (checksPass(checks, pr.head.sha)) return { pr, target };
  }
  return null;
}

function maintenance(helper, action, args = []) {
  const env = { ...process.env, KUBECTL_BIN: kubectl, MAINTENANCE_STATE_FILE: path.join(WORK, 'maintenance.json') };
  const raw = execFileSync('python3', [helper, action, ...args], { env, encoding: 'utf8', timeout: 120000 });
  return JSON.parse(raw);
}

function maintenanceActive(status, silenceId, now = Date.now()) {
  const states = Object.values(status.memberStates || {});
  return status.silenceId === silenceId && status.state === 'active' &&
    states.length >= 2 && states.every(state => state === 'active') &&
    Date.parse(status.endsAt) - now >= 600000;
}

async function verifyRollout(target, mergeSha, token, yaml, options = {}) {
  const observe = options.observe || kget;
  const read = options.read || readFileAt;
  const api = options.api || github;
  const request = options.request || fetch;
  const pause = options.pause || sleep;
  const now = options.now || Date.now;
  const deadline = now() + (options.timeoutMs ?? 480000);
  let lastObservation = 'waiting for merged manifest', text;
  while (text === undefined && now() < deadline) {
    try { text = await read(target.file, mergeSha, token); }
    catch (error) { lastObservation = 'observe-manifest: ' + error.name; await pause(options.intervalMs ?? 5000); }
  }
  if (text === undefined) {
    const error = new Error('Rollout verification timed out (' + lastObservation + ')');
    error.name = 'RolloutVerificationTimeout'; throw error;
  }
  const hr = yaml.parse(text);
  assert(hr.spec?.values?.controllers && hr.metadata?.name, 'Fast targets require an app-template HelmRelease');
  const release = hr.spec.releaseName || hr.metadata.name;
  const root = './' + path.posix.dirname(target.file);
  const matches = Object.values(hr.spec.values.controllers).flatMap(c => Object.values(c.containers || {})).filter(c => c.image?.repository === target.image);
  assert.equal(matches.length, 1, 'Expected exactly one allowlisted container');
  const container = matches[0];
  assert(container.image.tag, 'Allowlisted image pin missing');
  const expected = `$${target.image}:$${container.image.tag}`;
  const probe = container.probes?.readiness?.spec?.httpGet;
  assert(probe && Number.isInteger(probe.port) && probe.path.startsWith('/') && (!probe.scheme || probe.scheme === 'HTTP'), 'Fast targets require an explicit HTTP readiness probe');
  const revisionCache = new Map();
  const containsChange = async revision => {
    const sha = revision?.match(/(?:^|:)([a-f0-9]{40})$/)?.[1];
    if (sha === mergeSha) return true;
    if (!sha) return false;
    if (revisionCache.has(sha)) return revisionCache.get(sha);
    const comparison = await api(`/repos/$${REPOSITORY}/compare/$${mergeSha}...$${sha}`, token);
    let accepted = comparison.status === 'ahead' && comparison.merge_base_commit?.sha === mergeSha;
    if (accepted) {
      const later = yaml.parse(await read(target.file, sha, token));
      const pins = Object.values(later.spec?.values?.controllers || {}).flatMap(c => Object.values(c.containers || {})).filter(c => c.image?.repository === target.image).map(c => c.image.tag);
      accepted = pins.length === 1 && pins[0] === container.image.tag;
    }
    revisionCache.set(sha, accepted);
    return accepted;
  };
  const ready = object => object?.status?.conditions?.some(c => c.type === 'Ready' && c.status === 'True' && c.observedGeneration === object.metadata.generation);
  let stage = 'observe-source';
  lastObservation = 'waiting for Flux';
  while (now() < deadline) {
    try {
      stage = 'observe-source';
      const source = await observe('gitrepositories.source.toolkit.fluxcd.io', 'flux-system', 'home-kubernetes');
      stage = 'observe-kustomization';
      const ks = (await observe('kustomizations.kustomize.toolkit.fluxcd.io', 'flux-system')).items.find(k => k.spec.path === root);
      stage = 'verify-ancestry';
      if (ks && ready(ks) && await containsChange(source.status?.artifact?.revision) && await containsChange(ks.status?.lastAppliedRevision)) {
        const namespace = ks.spec.targetNamespace;
        stage = 'observe-helmrelease';
        const live = await observe('helmreleases.helm.toolkit.fluxcd.io', namespace, hr.metadata.name);
        stage = 'observe-deployment';
        const deployments = (await observe('deployments', namespace)).items.filter(d => d.spec.template.spec.containers.some(c => c.image === expected));
        const deployed = deployments.length > 0 && deployments.every(d => d.status.observedGeneration === d.metadata.generation && (d.status.availableReplicas || 0) === d.spec.replicas && (d.status.updatedReplicas || 0) === d.spec.replicas);
        if (ready(live) && deployed) {
          stage = 'observe-health';
          const health = await request(`http://$${release}.$${namespace}.svc.cluster.local:$${probe.port}$${probe.path}`, { signal: AbortSignal.timeout(10000) });
          if (health.ok) return { revision: mergeSha, sourceRevision: source.status.artifact.revision, appliedRevision: ks.status.lastAppliedRevision, release, namespace, image: expected, health: health.status };
          lastObservation = `observe-health: HTTP $${health.status}`;
        } else lastObservation = 'waiting for ready HelmRelease and expected Deployment image';
      } else lastObservation = 'waiting for Flux revisions containing the intended unchanged image pin';
    } catch (error) {
      // Observe again within the deadline. Never emit HTTP/client error objects.
      const code = error.code || error.cause?.code;
      lastObservation = `$${stage}: $${error.name}$${typeof code === 'string' && /^[A-Z_]+$/.test(code) ? ' ' + code : ''}`;
    }
    await pause(options.intervalMs ?? 5000);
  }
  const error = new Error(`Rollout verification timed out ($${lastObservation})`);
  error.name = 'RolloutVerificationTimeout';
  throw error;
}

async function main() {
  fs.mkdirSync(WORK, { recursive: true, mode: 0o700 });
  const { token, author } = await installationToken();
  const base = await github(`/repos/$${REPOSITORY}/commits/main`, token);
  const preset = JSON.parse(await readFileAt(PRESET, base.sha, token));
  const targets = targetsFromPreset(preset);
  const config = buildConfig(targets);
  const configPath = path.join(WORK, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify(config), { mode: 0o600 });
  const requireRenovate = createRequire('/usr/local/renovate/package.json');
  const yaml = requireRenovate('yaml');
  report({ phase: 'scan', targets, repository: REPOSITORY });
  const log = path.join(WORK, 'scan.jsonl');
  // Full debug output stays inside the private, short-lived Pod filesystem.
  execFileSync('/usr/local/renovate/node', ['/usr/local/renovate/dist/renovate.js'], {
    env: { PATH: process.env.PATH, HOME: WORK, TMPDIR: WORK, RENOVATE_CONFIG_FILE: configPath, RENOVATE_TOKEN: token, LOG_LEVEL: 'debug', LOG_FORMAT: 'json' },
    stdio: ['ignore', fs.openSync(log, 'w', 0o600), fs.openSync(path.join(WORK, 'scan-errors.log'), 'w', 0o600)], timeout: 180000
  });
  const records = fs.readFileSync(log, 'utf8').split('\n').flatMap(line => { try { return [JSON.parse(line)]; } catch { return []; } });
  const finished = records.find(record => record.msg === 'Repository finished');
  const extraction = records.find(record => record.msg === 'Dependency extraction complete');
  assert.equal(finished?.result, 'done', 'Renovate did not finish repository processing');
  assert.equal(extraction?.stats?.total?.fileCount, targets.length, 'Expected exact allowlisted manifest extraction');
  report({ phase: 'scan-complete', targets: targets.length, stats: extraction.stats });
  const candidate = await readyPull(targets, token, author, yaml);
  if (!candidate) { report({ phase: 'complete', result: 'no merge-ready fast update' }); return; }
  const helper = path.join(WORK, 'cluster-maintenance');
  fs.writeFileSync(helper, await readFileAt('scripts/cluster-maintenance', base.sha, token), { mode: 0o700 });
  const baseline = JSON.parse(process.env.FAST_UPDATE_BASELINE_ALERTS || '[]');
  assert(Array.isArray(baseline) && baseline.every(s => typeof s === 'string'));
  const args = ['--reason', `fast-update PR $${candidate.pr.number}: $${candidate.target.image}`, '--duration', '30m', ...baseline.flatMap(s => ['--allow-active-alert', s])];
  const window = maintenance(helper, 'begin', args);
  assert(window.silenceId && window.state === 'active');
  report({ phase: 'maintenance', pull: candidate.pr.number, silenceId: window.silenceId });
  let merged = false;
  try {
    const active = maintenance(helper, 'status', ['--id', window.silenceId]);
    assert(maintenanceActive(active, window.silenceId), 'Maintenance window is not active with at least ten minutes remaining');
    const result = await github(`/repos/$${REPOSITORY}/pulls/$${candidate.pr.number}/merge`, token, 'PUT', { merge_method: 'squash', sha: candidate.pr.head.sha });
    assert(result.merged && result.sha, 'Protected merge rejected'); merged = true;
    report({ phase: 'merged', pull: candidate.pr.number, revision: result.sha });
    const evidence = await verifyRollout(candidate.target, result.sha, token, yaml);
    report({ phase: 'healthy', ...evidence });
    maintenance(helper, 'end', ['--id', window.silenceId]);
    report({ phase: 'complete', result: 'deployed and verified', silenceId: window.silenceId });
  } catch (error) {
    // Release an unused window. After merge, preserve fail-closed helper checks;
    // never roll an old binary back across an unknown database migration.
    if (!merged) {
      try { maintenance(helper, 'end', ['--id', window.silenceId]); }
      catch { report({ phase: 'maintenance-expiry-required', silenceId: window.silenceId, merged }); }
    } else {
      report({ phase: 'rollout-verification-failed', silenceId: window.silenceId, expiresAt: window.endsAt });
    }
    throw error;
  }
}

module.exports = { targetsFromPreset, buildConfig, pinOnlyPatch, imageOnlyChange, checksPass, REQUIRED_CHECKS, installationToken, maintenanceActive, verifyRollout };
if (require.main === module) main().catch(error => {
  // Error objects from HTTP clients or child processes can contain credentials.
  report({ phase: 'failed', errorType: error.name, message: error instanceof assert.AssertionError || error.name === 'RolloutVerificationTimeout' ? error.message : 'Fast-update run failed; inspect private Pod diagnostics' });
  process.exitCode = 1;
});
