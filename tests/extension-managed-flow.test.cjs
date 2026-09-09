const test = require('node:test');
const assert = require('node:assert/strict');
const { createManagedFlowSetup } = require('../tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension/managed-flow.js');

function fixture(config) {
    const local = {};
    const calls = [];
    const setup = createManagedFlowSetup({ config, readLocal: async () => local,
        writeLocal: async value => Object.assign(local, value),
        saveSettings: async settings => { calls.push(settings); },
    });
    return { setup, local, calls };
}
const config = { deploymentId: 'internal-v1', serverUrl: 'https://nathanai.xyz', collectorKey: 'k'.repeat(64) };
test('public package does not silently enroll any profile', async () => {
    const { setup, calls } = fixture(null);
    assert.equal(await setup(), false);
    assert.equal(calls.length, 0);
});
test('internal package configures destination, consent and key in one enrollment', async () => {
    const { setup, local, calls } = fixture(config);
    assert.equal(await setup(), true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], { enabled: true, operationMode: 'cookie', flowSessionSyncEnabled: true,
        serverUrl: 'https://nathanai.xyz', flowCollectorKey: config.collectorKey, autoReload: false,
        browserTasksEnabled: false, clearGrecaptcha: false });
    assert.equal(local.managedFlowDeployment, 'internal-v1');
});
test('parallel startup hooks enroll once and later starts preserve explicit opt-out', async () => {
    const { setup, calls, local } = fixture(config);
    await Promise.all([setup(), setup(), setup()]);
    assert.equal(calls.length, 1);
    const nextStart = createManagedFlowSetup({ config, readLocal: async () => local,
        writeLocal: async () => assert.fail('already enrolled'), saveSettings: async () => assert.fail('must preserve disabled state') });
    assert.equal(await nextStart(), true);
});
test('invalid managed credentials fail closed without applying settings', async () => {
    const { setup, calls } = fixture({ ...config, collectorKey: 'short' });
    await assert.rejects(setup());
    assert.equal(calls.length, 0);
});
