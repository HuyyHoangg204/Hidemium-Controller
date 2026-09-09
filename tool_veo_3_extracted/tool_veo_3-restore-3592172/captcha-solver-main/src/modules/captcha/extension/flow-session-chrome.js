function readFlowPageIdentity() {
    if (location.origin !== 'https://flow.google.com' || location.pathname.startsWith('/about')) return null;
    const emails = [...new Set([...document.querySelectorAll('[aria-label]')]
        .flatMap(element => (element.getAttribute('aria-label') || '').match(/[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}/g) || [])
        .map(email => email.toLowerCase()))];
    return emails.length === 1 ? emails[0] : null;
}

function createChromeFlowCollector(getSettings, getCredential) {
    let activeKey = '';
    const sync = createFlowSessionSync({
        getSettings,
        inspect: async previous => {
            const tabs = previous ? [await chrome.tabs.get(previous.tabId)] : await chrome.tabs.query({ url: 'https://flow.google.com/*' });
            const stores = await chrome.cookies.getAllCookieStores();
            const identities = [];
            for (const tab of tabs) {
                if (tab.incognito || !tab.url || new URL(tab.url).origin !== 'https://flow.google.com') continue;
                const matches = stores.filter(store => store.tabIds.includes(tab.id));
                if (matches.length !== 1) continue;
                const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: readFlowPageIdentity });
                const account = results.find(result => result.frameId === 0)?.result;
                if (account) identities.push({ account, tabId: tab.id, storeId: matches[0].id });
            }
            if (!identities.length || new Set(identities.map(item => `${item.storeId}:${item.account}`)).size !== 1) return null;
            return identities.sort((left, right) => left.tabId - right.tabId)[0];
        },
        readCookies: storeId => chrome.cookies.getAll({ url: 'https://flow.google.com/', storeId }),
        supports: async (server, signal) => {
            activeKey = await getCredential(server);
            if (!activeKey) return false;
            const response = await fetch(`${server}/api/cookie-sync/flow`, {
                credentials: 'omit', cache: 'no-store', redirect: 'error', signal,
                headers: { 'X-Extension-Key': activeKey },
            });
            if (!response.ok) return false;
            const result = await response.json();
            return result.protocol === 'flow-session-v1';
        },
        send: async (server, payload, signal) => {
            if (!activeKey || activeKey !== await getCredential(server)) return false;
            const current = await getSettings();
            if (!current.enabled || !current.flowSessionSyncEnabled || current.operationMode !== 'cookie' || flowCollectorOrigin(current.serverUrl) !== server) return false;
            signal.throwIfAborted();
            const response = await fetch(`${server}/api/cookie-sync/flow`, {
                method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error', signal,
                headers: { 'Content-Type': 'application/json', 'X-Extension-Key': activeKey }, body: JSON.stringify(payload),
            });
            if (!response.ok) return false;
            const result = await response.json();
            return result.protocol === 'flow-session-v1' && result.accepted === true;
        },
    });
    return async options => {
        const result = await sync(options);
        await chrome.storage.local.set({ flowSessionStatus: { ...result, at: Date.now() } });
        return result;
    };
}
