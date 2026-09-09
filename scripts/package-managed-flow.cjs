const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

process.umask(0o077);
const root = path.resolve(__dirname, '..');
const extension = path.join(root, 'tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension');
function argument(name, fallback) {
    const index = process.argv.indexOf(name);
    return index < 0 ? fallback : process.argv[index + 1];
}
function main() {
    const outputArgument = argument('--output');
    const keyFile = argument('--key-file');
    if (!outputArgument || !keyFile) throw Error('Usage: --key-file PRIVATE_FILE --output PRIVATE_DIRECTORY');
    const output = path.resolve(outputArgument);
    const parent = fs.realpathSync(path.dirname(output));
    const relative = path.relative(root, parent);
    if (!relative.startsWith('..' + path.sep) && relative !== '..') throw Error('Private package must be outside the repository');
    if (fs.existsSync(output)) throw Error('Output already exists; choose a new directory');
    const collectorKey = fs.readFileSync(keyFile, 'utf8').trim();
    if (!/^[A-Za-z0-9_-]{32,512}$/.test(collectorKey)) throw Error('Invalid private collector key');
    const serverUrl = new URL(argument('--server', 'https://nathanai.xyz'));
    if (serverUrl.username || serverUrl.password || serverUrl.search || serverUrl.hash || serverUrl.pathname !== '/' ||
        (serverUrl.protocol !== 'https:' && !(serverUrl.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(serverUrl.hostname)))) throw Error('Invalid collector origin');
    const version = JSON.parse(fs.readFileSync(path.join(extension, 'manifest.json'), 'utf8')).version;
    const destination = path.join(output, 'extension');
    fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
    const files = fs.readdirSync(extension).filter(name => /\.(js|html|json)$/.test(name));
    for (const file of files) fs.copyFileSync(path.join(extension, file), path.join(destination, file));
    const deploymentId = `managed-${version}-${crypto.randomUUID()}`;
    fs.writeFileSync(path.join(destination, 'managed-config.js'), `const MANAGED_FLOW_CONFIG = ${JSON.stringify({ deploymentId, serverUrl: serverUrl.origin, collectorKey })};\n`, { mode: 0o600 });
    fs.chmodSync(path.join(destination, 'managed-config.js'), 0o600);
    const zip = path.join(output, `Hidemium-Flow-Auto-${version}.zip`);
    execFileSync('zip', ['-q', zip, ...files], { cwd: destination, stdio: 'pipe' });
    fs.chmodSync(zip, 0o600);
    console.log(JSON.stringify({ version, archive: zip, extension: destination, server: serverUrl.origin, containsPrivateKey: true }));
}
try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; }
