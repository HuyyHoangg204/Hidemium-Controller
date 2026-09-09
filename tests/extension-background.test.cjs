const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const extension = path.resolve(__dirname, '../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension');

function runtime(overrides = {}) {
    const state = { enabled: false, operationMode: 'cookie', serverUrl: 'http://127.0.0.1:3000', ...overrides };
    const local = {};
    const mutations = [];
    const fetches = [];
    const intervals = [];
    const events = {};
    const event = name => ({ addListener: handler => { (events[name] ||= []).push(handler); } });
    const storage = { get: async defaults => typeof defaults === 'object' && defaults ? { ...defaults, ...state } : { ...state }, set: async value => Object.assign(state, value) };
    const chrome = {
        storage: { sync: storage, local: { get: async defaults => ({ ...defaults, ...local }), set: async value => Object.assign(local, value) }, session: { get: (keys, done) => done({ captchaWorkerId: 'test' }), set: async () => {} } },
        runtime: { id: 'extension', getURL: value => `chrome-extension://extension/${value}`, onMessage: event('message'), onInstalled: event('installed'), onConnect: event('connect') },
        tabs: { query: async () => [{ id: 1, url: 'https://labs.google/fx/tools/flow' }], update: async (...args) => mutations.push(['update', ...args]), reload: async (...args) => mutations.push(['reload', ...args]), create: async (...args) => mutations.push(['create', ...args]), sendMessage: async () => {}, onUpdated: event('updated'), onRemoved: event('removed') },
        alarms: { clear: async () => {}, create: async () => {}, getAll: async () => [], onAlarm: event('alarm') },
        webRequest: { onSendHeaders: event('headers') },
        cookies: { getAll: async () => [] },
        scripting: { executeScript: async () => [] },
        windows: { update: async () => {} },
    };
    const context = vm.createContext({ chrome, URL, AbortSignal, AbortController, TextEncoder, console: { log() {}, warn() {}, error() {} }, setTimeout: () => 1, clearTimeout() {}, setInterval: (handler, delay) => { intervals.push({ handler, delay }); }, fetch: async (...args) => { fetches.push(args); return { ok: true, json: async () => ({}) }; } });
    context.importScripts = (...files) => files.forEach(file => vm.runInContext(fs.readFileSync(path.join(extension, file), 'utf8'), context));
    vm.runInContext(fs.readFileSync(path.join(extension, 'background.js'), 'utf8'), context);
    return { context, mutations, fetches, intervals, events, state, local };
}

test('disabled full service worker never sends headers, heartbeats or reloads idle tabs', async () => {
    const instance = runtime({ operationMode: 'captcha' });
    await new Promise(resolve => setImmediate(resolve));
    await instance.events.headers[0]({ requestHeaders: [{ name: 'x-client-data', value: 'private' }] });
    await instance.intervals.find(item => item.delay === 10000).handler();
    await vm.runInContext('_captchaLastActivity = 0; captchaCheckIdle()', instance.context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(instance.fetches.length, 0);
    assert.equal(instance.mutations.length, 0);
});
test('enabled cookie collector never runs idle CAPTCHA reload', async () => {
    const instance = runtime({ enabled: true });
    await vm.runInContext('_captchaLastActivity = 0; captchaCheckIdle()', instance.context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(instance.mutations.length, 0);
});
test('content script cannot trigger the sensitive Flow upload command', () => {
    const instance = runtime();
    let reply;
    instance.events.message[0]({ type: 'SYNC_FLOW_SESSION' }, { id: 'extension', url: 'https://flow.google.com/', tab: { id: 1 } }, value => { reply = value; });
    assert.equal(reply.state, 'forbidden');
});
test('popup receives settings validation failure rather than false success', async () => {
    const instance = runtime();
    const reply = await new Promise(resolve => instance.events.message[0]({ type: 'SAVE_SETTINGS', settings: { serverUrl: 'http://insecure.example' } }, { id: 'extension', url: 'chrome-extension://extension/popup.html' }, resolve));
    assert.equal(reply.success, false);
    assert.equal(instance.state.serverUrl, 'http://127.0.0.1:3000');
});
test('own popup opened as a tab can request a metadata-only check', async () => {
    const instance = runtime();
    const reply = await new Promise(resolve => instance.events.message[0]({ type: 'CHECK_FLOW_SESSION' }, { id: 'extension', url: 'chrome-extension://extension/popup.html', tab: { id: 9 } }, resolve));
    assert.equal(reply.state, 'disabled');
});
test('content script cannot change the credential destination or local consent', async () => {
    const instance = runtime();
    const reply = await new Promise(resolve => instance.events.message[0]({ type: 'SAVE_SETTINGS', settings: { serverUrl: 'https://untrusted.example', flowSessionSyncEnabled: true } }, { id: 'extension', url: 'https://labs.google/', tab: { id: 1 } }, resolve));
    assert.equal(reply.success, false);
    assert.equal(instance.state.serverUrl, 'http://127.0.0.1:3000');
});
test('Flow export consent is profile-local and cannot follow synced settings', async () => {
    const instance = runtime({ flowSessionSyncEnabled: true });
    assert.equal((await vm.runInContext('getSettings()', instance.context)).flowSessionSyncEnabled, false);
    await vm.runInContext('saveSettings({enabled:true,operationMode:"cookie",serverUrl:"http://127.0.0.1:3000",flowSessionSyncEnabled:true})', instance.context);
    assert.equal(instance.local.flowSessionConsentOrigin, 'http://127.0.0.1:3000');
    assert.equal((await vm.runInContext('getSettings()', instance.context)).flowSessionSyncEnabled, true);
    instance.state.serverUrl = 'https://another.example';
    assert.equal((await vm.runInContext('getSettings()', instance.context)).flowSessionSyncEnabled, false);
});
test('missing local consent never downgrades Flow mode into legacy upload', async () => {
    const instance = runtime({ enabled: true, flowSessionSyncEnabled: true, browserTasksEnabled: true });
    await vm.runInContext('_lastCookieAccount = "old@example.test"; btaskPollOnce()', instance.context);
    const result = await vm.runInContext('pushCookiesToServer()', instance.context);
    assert.equal(result.state, 'disabled');
    assert.equal(instance.fetches.length, 0);
    await instance.events.installed[0]({ reason: 'update' });
    assert.equal(instance.state.flowSessionSyncEnabled, true);
});
