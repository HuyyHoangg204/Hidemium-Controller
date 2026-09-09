const test = require('node:test');
const assert = require('node:assert/strict');
const { createCookieSync } = require('../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension/cookie-sync.js');

function fixture(overrides = {}) {
    const sent = [];
    const options = {
        getSettings: async () => ({ enabled: true, operationMode: 'cookie' }),
        readCookie: async () => 'synthetic',
        resolveAccount: async () => 'Current@Example.test',
        readGrok: async () => null,
        send: async payload => { sent.push(payload); return { ok: true }; },
        ...overrides,
    };
    return { run: createCookieSync(options), sent };
}

test('disabled collector neither reads nor sends credentials', async () => {
    const { run, sent } = fixture({ getSettings: async () => ({ enabled: false, operationMode: 'cookie' }), readCookie: async () => { throw Error('must not read'); } });
    assert.equal((await run()).state, 'disabled');
    assert.equal(sent.length, 0);
});
test('each capture uses verified current identity, not a URL hint', async () => {
    const { run, sent } = fixture();
    assert.equal((await run()).state, 'synced');
    assert.equal(sent[0].account, 'current@example.test');
});
test('does not upload a mixed snapshot if cookie changes during verification', async () => {
    let reads = 0;
    const { run, sent } = fixture({ readCookie: async () => ++reads === 1 ? 'before' : 'after' });
    assert.equal((await run()).state, 'session_changed');
    assert.equal(sent.length, 0);
});
test('coalesces concurrent capture calls into one upload', async () => {
    const { run, sent } = fixture();
    await Promise.all([run(), run(), run()]);
    assert.equal(sent.length, 1);
});
test('propagates server rejection without remembering success', async () => {
    const { run } = fixture({ send: async () => ({ ok: false, status: 422 }) });
    assert.deepEqual(await run(), { state: 'rejected', status: 422 });
});
test('returns a safe failure and unlocks after network error', async () => {
    let attempts = 0;
    const { run } = fixture({ send: async () => { if (++attempts === 1) throw Error('SECRET'); return { ok: true }; } });
    assert.deepEqual(await run(), { state: 'unavailable' });
    assert.equal((await run()).state, 'synced');
});
test('rechecks the kill switch immediately before sending', async () => {
    let reads = 0;
    const { run, sent } = fixture({ getSettings: async () => ({ enabled: ++reads === 1, operationMode: 'cookie' }) });
    assert.equal((await run()).state, 'disabled');
    assert.equal(sent.length, 0);
});
test('no identity means no upload even if cookies exist', async () => {
    const { run, sent } = fixture({ resolveAccount: async () => null });
    assert.equal((await run()).state, 'needs_login');
    assert.equal(sent.length, 0);
});
