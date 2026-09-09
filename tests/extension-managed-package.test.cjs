const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const script = path.resolve(__dirname, '../scripts/package-managed-flow.cjs');

test('private package embeds configuration outside repository and leaves public source secret-free', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-flow-package-'));
    try {
        const key = 'synthetic'.repeat(8);
        const keyFile = path.join(root, 'key');
        fs.writeFileSync(keyFile, key, { mode: 0o600 });
        const output = path.join(root, 'package');
        const log = execFileSync(process.execPath, [script, '--key-file', keyFile, '--output', output], { encoding: 'utf8' });
        assert.equal(log.includes(key), false);
        const config = fs.readFileSync(path.join(output, 'extension', 'managed-config.js'), 'utf8');
        assert.ok(config.includes('https://nathanai.xyz'));
        assert.ok(config.includes(key));
        assert.equal(fs.statSync(path.join(output, 'extension', 'managed-config.js')).mode & 0o777, 0o600);
        const publicConfig = fs.readFileSync(path.resolve(__dirname, '../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension/managed-config.js'), 'utf8');
        assert.equal(publicConfig.includes(key), false);
        assert.ok(fs.existsSync(path.join(output, 'Hidemium-Flow-Auto-1.3.0.zip')));
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('packager refuses credentials in the git checkout', () => {
    assert.throws(() => execFileSync(process.execPath, [script, '--key-file', '/unused', '--output', path.resolve(__dirname, '../build/private-package')], { stdio: 'pipe' }));
});
