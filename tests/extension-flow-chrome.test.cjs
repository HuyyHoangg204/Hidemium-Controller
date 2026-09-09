const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { COOKIE_NAMES } = require('../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension/flow-session.js');
const root = path.resolve(__dirname, '../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension');

function fixture(overrides = {}) {
    const requests = [];
    const reads = [];
    const state = {};
    const tabs = overrides.tabs || [{ id: 7, url: 'https://flow.google.com/' }];
    const cookies = COOKIE_NAMES.map(name => ({ name, value: `fake-${name}`, domain: name.includes('OSID') ? 'flow.google.com' : '.google.com', path: '/', secure: true, httpOnly: true, hostOnly: name.includes('OSID'), sameSite: 'no_restriction', session: true, storeId: '0' }));
    const chrome = {
        tabs: { query: async () => tabs, get: async tabId => tabs.find(tab => tab.id === tabId) },
        cookies: { getAllCookieStores: async () => [{ id: '0', tabIds: tabs.map(tab => tab.id) }], getAll: async filter => { reads.push(filter); return cookies; } },
        scripting: { executeScript: async options => [{ frameId: 0, result: overrides.identities?.[options.target.tabId] || 'flow@example.test' }] },
        storage: { local: { set: async value => Object.assign(state, value) } },
    };
    const context = vm.createContext({ chrome, URL, AbortController, TextEncoder, setTimeout, clearTimeout,
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (overrides.fetch) return overrides.fetch(url, options);
            return { ok: true, json: async () => ({ protocol: 'flow-session-v1', accepted: true }) };
        },
    });
    for (const file of ['flow-session.js', 'flow-session-chrome.js']) vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), context);
    context.settings = { enabled: true, operationMode: 'cookie', flowSessionSyncEnabled: true, serverUrl: 'http://127.0.0.1:43210', flowCollectorKey: 'k'.repeat(32) };
    context.getCredential = overrides.getCredential || (async () => 'k'.repeat(32));
    const sync = vm.runInContext('createChromeFlowCollector(async () => settings, getCredential)', context);
    return { sync, requests, reads, state };
}

test('Chrome adapter reads one effective URL/store, emits v1 only, and saves no secrets', async () => {
    const { sync, requests, reads, state } = fixture();
    assert.equal((await sync()).state, 'synced');
    assert.equal(reads.length, 2);
    assert.ok(reads.every(filter => filter.url === 'https://flow.google.com/' && filter.storeId === '0'));
    assert.equal(requests.length, 2);
    assert.ok(requests.every(request => request.options.headers['X-Extension-Key'] === 'k'.repeat(32)));
    assert.ok(requests.every(request => request.url.endsWith('/api/cookie-sync/flow') && request.options.redirect === 'error' && request.options.credentials === 'omit'));
    assert.equal(JSON.parse(requests[1].options.body).session.cookies.length, 16);
    assert.equal(JSON.stringify(state).includes('fake-'), false);
});
test('multiple Flow accounts fail closed rather than pick the first', async () => {
    const { sync, requests, reads } = fixture({ tabs: [{ id: 7, url: 'https://flow.google.com/' }, { id: 8, url: 'https://flow.google.com/u/1/' }], identities: { 7: 'a@example.test', 8: 'b@example.test' } });
    assert.equal((await sync()).state, 'needs_flow_login');
    assert.equal(reads.length, 0);
    assert.equal(requests.length, 1);
});
test('incognito and wrong-origin tabs are not eligible for export', async () => {
    for (const tab of [{ id: 7, url: 'https://flow.google.com/', incognito: true }, { id: 7, url: 'https://accounts.google.com/' }]) {
        const { sync, reads } = fixture({ tabs: [tab] });
        assert.equal((await sync()).state, 'needs_flow_login');
        assert.equal(reads.length, 0);
    }
});
test('HTTP 200 with a legacy body does not authorize cookie upload', async () => {
    const { sync, requests, reads } = fixture({ fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }) });
    assert.equal((await sync()).state, 'receiver_unsupported');
    assert.equal(requests.length, 1);
    assert.equal(reads.length, 0);
});
test('POST must acknowledge v1 and accepted true', async () => {
    const { sync } = fixture({ fetch: async (url, options) => ({ ok: true, json: async () => options.method === 'POST' ? { ok: true } : { protocol: 'flow-session-v1' } }) });
    assert.equal((await sync()).state, 'rejected');
});
test('missing collector key blocks even capability discovery and cookie reads', async () => {
    const { sync, requests, reads } = fixture({ getCredential: async () => '' });
    assert.equal((await sync()).state, 'receiver_unsupported');
    assert.equal(requests.length, 0);
    assert.equal(reads.length, 0);
});
test('rotated collector key aborts upload after capture', async () => {
    let calls = 0;
    const { sync, requests } = fixture({ getCredential: async () => (++calls === 1 ? 'k' : 'j').repeat(32) });
    assert.equal((await sync()).state, 'rejected');
    assert.equal(requests.length, 1);
});
