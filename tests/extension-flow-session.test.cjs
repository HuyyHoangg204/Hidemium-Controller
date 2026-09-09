const test = require('node:test');
const assert = require('node:assert/strict');
const { COOKIE_NAMES, selectFlowCookies, createFlowSessionSync, flowCollectorOrigin } = require('../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension/flow-session.js');

const now = 1788980000000;
function cookies() {
    return COOKIE_NAMES.map(name => ({ name, value: `synthetic-${name}`, domain: name.includes('OSID') ? 'flow.google.com' : '.google.com', path: '/', secure: true, httpOnly: true, sameSite: 'no_restriction', hostOnly: name.includes('OSID'), session: false, expirationDate: now / 1000 + 3600, storeId: '0' }));
}
function fixture(overrides = {}) {
    const sent = [];
    const options = {
        getSettings: async () => ({ enabled: true, operationMode: 'cookie', flowSessionSyncEnabled: true, serverUrl: 'http://127.0.0.1:43210' }),
        inspect: async () => ({ account: 'Flow@Example.test', tabId: 7, storeId: '0' }),
        readCookies: async () => cookies(),
        supports: async () => true,
        send: async (server, payload) => { sent.push({ server, payload }); return true; },
        now: () => now,
        timeoutMs: 100,
        ...overrides,
    };
    return { sync: createFlowSessionSync(options), sent };
}

test('exports exactly the tested set, preserving Chrome restoration attributes', () => {
    const extra = { ...cookies()[0], name: 'unrelated', domain: '.google.com' };
    const selected = selectFlowCookies([...cookies(), extra], '0', now);
    assert.equal(selected.length, 16);
    assert.deepEqual(selected.find(cookie => cookie.name === 'SID'), cookies().find(cookie => cookie.name === 'SID'));
    assert.deepEqual(selected, selectFlowCookies([...cookies()].reverse(), '0', now));
});
for (const [label, mutate] of [
    ['missing Flow cookie', values => values.filter(cookie => cookie.name !== 'OSID')],
    ['duplicate', values => [...values, values[0]]],
    ['different path', values => values.map((cookie, index) => index ? cookie : { ...cookie, path: '/other' })],
    ['mixed store', values => values.map((cookie, index) => index ? cookie : { ...cookie, storeId: '1' })],
    ['partitioned', values => values.map((cookie, index) => index ? cookie : { ...cookie, partitionKey: { topLevelSite: 'https://flow.google.com' } })],
    ['expired', values => values.map((cookie, index) => index ? cookie : { ...cookie, expirationDate: now / 1000 - 1 })],
    ['empty', values => values.map((cookie, index) => index ? cookie : { ...cookie, value: '' })],
    ['insecure secure-prefix', values => values.map(cookie => cookie.name === '__Secure-OSID' ? { ...cookie, secure: false } : cookie)],
    ['wrong host-only scope', values => values.map(cookie => cookie.name === 'OSID' ? { ...cookie, hostOnly: false } : cookie)],
]) test(`rejects ${label}`, () => assert.throws(() => selectFlowCookies(mutate(cookies()), '0', now)));
test('preserves a session cookie without inventing expiry', () => {
    const values = cookies().map(({ expirationDate, ...cookie }) => ({ ...cookie, session: true }));
    assert.equal(selectFlowCookies(values, '0', now)[0].expirationDate, undefined);
});
test('disabled or missing explicit Flow opt-in does not touch cookies', async () => {
    for (const changes of [{ enabled: false }, { flowSessionSyncEnabled: false }, { operationMode: 'captcha' }]) {
        const { sync, sent } = fixture({ getSettings: async () => ({ enabled: true, operationMode: 'cookie', flowSessionSyncEnabled: true, ...changes }), readCookies: async () => { throw Error('must not read'); } });
        assert.equal((await sync()).state, 'disabled');
        assert.equal(sent.length, 0);
    }
});
test('real Flow identity owns versioned payload and no Labs cookie is mixed in', async () => {
    const { sync, sent } = fixture();
    assert.equal((await sync()).state, 'synced');
    assert.equal(sent[0].payload.protocol, 'flow-session-v1');
    assert.equal(sent[0].payload.session.account, 'flow@example.test');
    assert.equal(sent[0].payload.session.cookies.length, 16);
    assert.equal(sent[0].payload.veo_cookie, undefined);
});
test('unsupported receiver gets no cookies and no legacy fallback', async () => {
    const { sync, sent } = fixture({ supports: async () => false, readCookies: async () => { throw Error('must not read'); } });
    assert.equal((await sync()).state, 'receiver_unsupported');
    assert.equal(sent.length, 0);
});
test('rejects account change during capture', async () => {
    let calls = 0;
    const { sync, sent } = fixture({ inspect: async () => ({ account: ++calls === 1 ? 'a@example.test' : 'b@example.test', tabId: 7, storeId: '0' }) });
    assert.equal((await sync()).state, 'session_changed');
    assert.equal(sent.length, 0);
});
test('rejects changed cookie even with same account', async () => {
    let calls = 0;
    const { sync, sent } = fixture({ readCookies: async () => cookies().map(cookie => ({ ...cookie, value: `${cookie.value}-${calls++ === 0 ? 'old' : 'new'}` })) });
    assert.equal((await sync()).state, 'session_changed');
    assert.equal(sent.length, 0);
});
test('rejects destination change or disabled toggle before send', async () => {
    for (const update of [{ serverUrl: 'https://different.example' }, { enabled: false }, { flowSessionSyncEnabled: false }]) {
        let reads = 0;
        const { sync, sent } = fixture({ getSettings: async () => ({ enabled: true, operationMode: 'cookie', flowSessionSyncEnabled: true, serverUrl: 'http://127.0.0.1:43210', ...(++reads > 1 ? update : {}) }) });
        assert.notEqual((await sync()).state, 'synced');
        assert.equal(sent.length, 0);
    }
});
test('coalesces concurrent runs', async () => {
    const { sync, sent } = fixture();
    await Promise.all([sync(), sync(), sync()]);
    assert.equal(sent.length, 1);
});
test('check-only returns metadata without sending or exposing values', async () => {
    const { sync, sent } = fixture();
    const result = await sync({ checkOnly: true });
    assert.equal(result.state, 'captured');
    assert.equal(result.cookieCount, 16);
    assert.equal(JSON.stringify(result).includes('synthetic'), false);
    assert.equal(sent.length, 0);
});
test('receiver must explicitly acknowledge protocol acceptance', async () => {
    const { sync } = fixture({ send: async () => false });
    assert.equal((await sync()).state, 'rejected');
});
test('bounded timeout unlocks but late capture cannot send', async () => {
    let finish;
    const { sync, sent } = fixture({ timeoutMs: 10, inspect: () => new Promise(resolve => { finish = resolve; }) });
    assert.equal((await sync()).state, 'timeout');
    finish({ account: 'a@example.test', tabId: 7, storeId: '0' });
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(sent.length, 0);
});
test('safe failures contain no raw secrets', async () => {
    const { sync } = fixture({ readCookies: async () => { throw Error('SECRET'); } });
    assert.deepEqual(await sync(), { state: 'unavailable' });
});
test('rejects credential-bearing, insecure or path-based destination URLs', () => {
    for (const url of ['http://example.test', 'https://user:password@example.test', 'https://example.test/path', 'https://example.test/?token=secret', 'file:///tmp/data']) {
        assert.throws(() => flowCollectorOrigin(url));
    }
    assert.equal(flowCollectorOrigin('https://example.test/'), 'https://example.test');
});
test('does not confuse a check-only flight with an upload flight', async () => {
    let resume;
    const { sync, sent } = fixture({ inspect: async () => { if (!resume) await new Promise(resolve => { resume = resolve; }); return { account: 'flow@example.test', tabId: 7, storeId: '0' }; } });
    const check = sync({ checkOnly: true });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(await sync(), { state: 'busy' });
    resume();
    assert.equal((await check).state, 'captured');
    assert.equal(sent.length, 0);
});
