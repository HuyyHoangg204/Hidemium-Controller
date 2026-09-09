const COOKIE_NAMES = Object.freeze([
    'SID', '__Secure-1PSID', '__Secure-3PSID', 'HSID', 'SSID', 'APISID', 'SAPISID',
    '__Secure-1PAPISID', '__Secure-3PAPISID', '__Secure-1PSIDTS', '__Secure-3PSIDTS',
    'SIDCC', '__Secure-1PSIDCC', '__Secure-3PSIDCC', 'OSID', '__Secure-OSID',
]);

function selectFlowCookies(cookies, storeId, now = Date.now()) {
    const selected = cookies.filter(cookie => COOKIE_NAMES.includes(cookie.name));
    if (selected.length !== COOKIE_NAMES.length) throw new Error('invalid_cookie_set');
    const result = COOKIE_NAMES.map(name => {
        const matches = selected.filter(cookie => cookie.name === name);
        if (matches.length !== 1) throw new Error('ambiguous_cookie');
        const cookie = matches[0];
        const flowScoped = name === 'OSID' || name === '__Secure-OSID';
        if (cookie.domain !== (flowScoped ? 'flow.google.com' : '.google.com') ||
            cookie.path !== '/' || cookie.storeId !== storeId || cookie.partitionKey ||
            cookie.hostOnly !== flowScoped || typeof cookie.value !== 'string' || !cookie.value ||
            typeof cookie.secure !== 'boolean' || typeof cookie.httpOnly !== 'boolean' ||
            typeof cookie.session !== 'boolean' ||
            !['no_restriction', 'lax', 'strict', 'unspecified'].includes(cookie.sameSite) ||
            (name.startsWith('__Secure-') && !cookie.secure) ||
            (!cookie.session && (!Number.isFinite(cookie.expirationDate) || cookie.expirationDate <= now / 1000 + 60))) {
            throw new Error('invalid_cookie_attributes');
        }
        const value = { name, value: cookie.value, domain: cookie.domain, path: cookie.path,
            secure: cookie.secure, httpOnly: cookie.httpOnly, sameSite: cookie.sameSite,
            hostOnly: cookie.hostOnly, session: cookie.session, storeId: cookie.storeId };
        if (!cookie.session) value.expirationDate = cookie.expirationDate;
        return value;
    });
    if (JSON.stringify(result).length > 60000) throw new Error('cookie_set_too_large');
    return result;
}

function flowCollectorOrigin(value) {
    const server = new URL(value);
    if (server.username || server.password || server.search || server.hash || server.pathname !== '/' ||
        (server.protocol !== 'https:' && !(server.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(server.hostname)))) {
        throw new Error('invalid_collector_origin');
    }
    return server.origin;
}

function createFlowSessionSync(options) {
    let inFlight = null;
    let flightCheckOnly = false;
    const enabled = settings => settings.enabled && settings.operationMode === 'cookie' && settings.flowSessionSyncEnabled === true;
    const identity = value => {
        if (!value || typeof value.account !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.account.trim()) ||
            !Number.isInteger(value.tabId) || typeof value.storeId !== 'string') return null;
        return { account: value.account.trim().toLowerCase(), tabId: value.tabId, storeId: value.storeId };
    };
    async function capture(checkOnly, signal) {
        const settings = await options.getSettings();
        if (!enabled(settings)) return { state: 'disabled' };
        const server = flowCollectorOrigin(settings.serverUrl);
        if (!checkOnly && !await options.supports(server, signal)) return { state: 'receiver_unsupported' };
        signal.throwIfAborted();
        const before = identity(await options.inspect());
        signal.throwIfAborted();
        if (!before) return { state: 'needs_flow_login' };
        const cookies = selectFlowCookies(await options.readCookies(before.storeId), before.storeId, (options.now || Date.now)());
        signal.throwIfAborted();
        const after = identity(await options.inspect(before));
        signal.throwIfAborted();
        if (JSON.stringify(before) !== JSON.stringify(after)) return { state: 'session_changed' };
        const rechecked = selectFlowCookies(await options.readCookies(before.storeId), before.storeId, (options.now || Date.now)());
        signal.throwIfAborted();
        if (JSON.stringify(cookies) !== JSON.stringify(rechecked)) return { state: 'session_changed' };
        const current = await options.getSettings();
        signal.throwIfAborted();
        if (!enabled(current)) return { state: 'disabled' };
        if (server !== flowCollectorOrigin(current.serverUrl)) return { state: 'settings_changed' };
        const status = { cookieCount: cookies.length, flowReady: true };
        if (checkOnly) return { state: 'captured', ...status };
        const payload = { protocol: 'flow-session-v1', session: {
            account: before.account, origin: 'https://flow.google.com',
            capturedAt: new Date((options.now || Date.now)()).toISOString(), cookies,
        } };
        const accepted = await options.send(server, payload, signal);
        signal.throwIfAborted();
        return accepted ? { state: 'synced', ...status } : { state: 'rejected' };
    }
    return function sync({ checkOnly = false } = {}) {
        if (inFlight) return flightCheckOnly === checkOnly ? inFlight : Promise.resolve({ state: 'busy' });
        flightCheckOnly = checkOnly;
        const controller = new AbortController();
        let timer;
        const deadline = new Promise(resolve => {
            timer = setTimeout(() => { controller.abort(); resolve({ state: 'timeout' }); }, options.timeoutMs || 20000);
        });
        inFlight = Promise.race([capture(checkOnly, controller.signal).catch(() => ({ state: controller.signal.aborted ? 'timeout' : 'unavailable' })), deadline])
            .finally(() => { clearTimeout(timer); inFlight = null; });
        return inFlight;
    };
}

if (typeof module !== 'undefined') module.exports = { COOKIE_NAMES, selectFlowCookies, createFlowSessionSync, flowCollectorOrigin };
