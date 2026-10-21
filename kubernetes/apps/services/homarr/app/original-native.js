// Consume both real Homarr originals with the installed native SQLite and crypto.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const Database = require('/app/node_modules/better-sqlite3');
const {TextDecoder} = require('util');
function requireState(value) { if (!value) throw new Error('original Homarr acceptance failed'); }
function hashFile(file) {
  const hash = crypto.createHash('sha256'), buffer = Buffer.alloc(1024 * 1024);
  const fd = fs.openSync(file, 'r');
  try { let count; while ((count = fs.readSync(fd, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, count)); }
  finally { fs.closeSync(fd); }
  return hash.digest('hex');
}
function inventory(root) {
  const entries = {};
  function visit(directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      const file = path.join(directory, name), info = fs.lstatSync(file, {bigint: true});
      requireState(info.isFile() || info.isDirectory());
      entries[path.relative(root, file)] = [String(info.mode), String(info.uid), String(info.gid),
        info.isFile() ? String(info.mtimeNs) : null, info.isFile() ? hashFile(file) : null];
      if (info.isDirectory()) visit(file);
    }
  }
  visit(root); return entries;
}
function database(file, key) {
  const db = new Database(file, {readonly: true});
  try {
    requireState(db.pragma('integrity_check', {simple: true}) === 'ok');
    const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all().map(row => row.name);
    requireState(tables.length > 0);
    const hash = crypto.createHash('sha256'); let rows = 0, decrypted = 0;
    const encode = value => JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? {integer: String(item)} : item);
    for (const table of tables) {
      hash.update(encode(db.prepare('SELECT sql FROM sqlite_master WHERE name=?').get(table)));
      for (const row of db.prepare('SELECT * FROM "' + table.replaceAll('"', '""') + '"').safeIntegers().iterate()) {
        hash.update(encode(row) + '\n'); rows++;
        if (table === 'integrationSecret' && row.value !== null) {
          requireState(typeof row.value === 'string' && /^[0-9a-f]+\.[0-9a-f]{32}$/.test(row.value));
          const [encrypted, iv] = row.value.split('.');
          // Exact native Homarr format observed in its installed decryptSecret:
          // AES-256-CBC, ciphertext.hex + '.' + iv.hex. Never log plaintext.
          const decipher = crypto.createDecipheriv('aes-256-cbc', key, Buffer.from(iv, 'hex'));
          const plaintext = Buffer.concat([decipher.update(Buffer.from(encrypted, 'hex')), decipher.final()]);
          new TextDecoder('utf-8', {fatal: true}).decode(plaintext); decrypted++;
        }
      }
    }
    return {tables: tables.length, rows, logical_sha256: hash.digest('hex'), native_original_secrets_decrypted: decrypted, integrity_check: 'ok'};
  } finally { db.close(); }
}
function main() {
  requireState(process.env.RECOVERY_TARGET === 'isolated');
  requireState(/^[0-9a-fA-F]{64}$/.test(process.env.SECRET_ENCRYPTION_KEY || ''));
  const key = Buffer.from(process.env.SECRET_ENCRYPTION_KEY, 'hex');
  const points = JSON.parse(process.env.ORIGINAL_POINTS);
  const trees = {}, results = {};
  for (const backend of ['nas', 'r2']) trees[backend] = inventory('/original/' + backend);
  requireState(JSON.stringify(trees.nas) === JSON.stringify(trees.r2));
  for (const backend of ['nas', 'r2']) {
    const original = '/original/' + backend, target = '/work/' + backend;
    requireState(!fs.existsSync(target)); fs.cpSync(original, target, {recursive: true, preserveTimestamps: true, errorOnExist: true});
    for (const name of Object.keys(trees[backend])) {
      const info = fs.statSync(path.join(original, name)); fs.chownSync(path.join(target, name), info.uid, info.gid);
    }
    const rootInfo = fs.statSync(original); fs.chownSync(target, rootInfo.uid, rootInfo.gid);
    for (const suffix of ['-wal', '-shm', '-journal']) fs.rmSync(path.join(target, 'db.sqlite' + suffix), {force: true});
    // Replay the actual source's VACUUM INTO artifact before native consumption.
    fs.copyFileSync(path.join(original, 'kopiur/homarr-online.sqlite'), path.join(target, 'db.sqlite'));
    results[backend] = database(path.join(target, 'db.sqlite'), key);
    requireState(JSON.stringify(inventory(original)) === JSON.stringify(trees[backend]));
    results[backend].snapshot_id = points[backend]; results[backend].original_unchanged = true;
  }
  requireState(results.nas.logical_sha256 === results.r2.logical_sha256 &&
    results.nas.native_original_secrets_decrypted === results.r2.native_original_secrets_decrypted);
  const receipt = {app: 'homarr', independent_original_native_recovery: true, nas_r2_native_data_equal: true,
    original_key_reference: 'homarr-secret/SECRET_ENCRYPTION_KEY', full_original_tree_equal: true, results};
  fs.writeFileSync('/work/native-receipt.json', JSON.stringify(receipt)); console.log(JSON.stringify(receipt));
}
try { main(); } catch (error) {
  console.log(JSON.stringify({failed: true, error_class: error.name, native_code: error.code || null})); process.exitCode = 1;
}
