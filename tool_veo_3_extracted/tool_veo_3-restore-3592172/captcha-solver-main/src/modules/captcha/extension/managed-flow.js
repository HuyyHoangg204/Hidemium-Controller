function createManagedFlowSetup({ config, readLocal, writeLocal, saveSettings }) {
    let initialization = null;
    async function enroll() {
        if (!config) return false;
        if (typeof config.deploymentId !== 'string' || !/^[a-zA-Z0-9._-]{1,100}$/.test(config.deploymentId) ||
            typeof config.collectorKey !== 'string' || !/^[a-zA-Z0-9_-]{32,512}$/.test(config.collectorKey)) {
            throw new Error('invalid_managed_flow_config');
        }
        const server = new URL(config.serverUrl);
        if (server.username || server.password || server.search || server.hash || server.pathname !== '/' ||
            (server.protocol !== 'https:' && !(server.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(server.hostname)))) {
            throw new Error('invalid_managed_flow_origin');
        }
        const state = await readLocal();
        if (state.managedFlowDeployment === config.deploymentId) return true;
        await saveSettings({ enabled: true, operationMode: 'cookie', flowSessionSyncEnabled: true,
            serverUrl: server.origin, flowCollectorKey: config.collectorKey, autoReload: false,
            browserTasksEnabled: false, clearGrecaptcha: false });
        await writeLocal({ managedFlowDeployment: config.deploymentId });
        return true;
    }
    return () => initialization || (initialization = enroll());
}

if (typeof module !== 'undefined') module.exports = { createManagedFlowSetup };
