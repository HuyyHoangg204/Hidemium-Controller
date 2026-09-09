function createCookieSync(options) {
    let inFlight = null;
    async function capture() {
        const settings = await options.getSettings();
        if (!settings.enabled || settings.operationMode !== 'cookie') return { state: 'disabled' };
        const cookie = await options.readCookie();
        if (!cookie) return { state: 'needs_login' };
        const identity = await options.resolveAccount();
        if (typeof identity !== 'string' || !identity.includes('@')) return { state: 'needs_login' };
        const account = identity.trim().toLowerCase();
        const grok = await options.readGrok();
        if (cookie !== await options.readCookie()) return { state: 'session_changed' };
        const current = await options.getSettings();
        if (!current.enabled || current.operationMode !== 'cookie') return { state: 'disabled' };
        const payload = { account, veo_cookie: cookie };
        if (grok) payload.grok_cookies = grok;
        const result = await options.send(payload);
        if (!result.ok) return { state: 'rejected', status: result.status };
        options.onIdentity?.(account);
        return { state: 'synced' };
    }
    return function sync() {
        if (inFlight) return inFlight;
        inFlight = capture().catch(() => ({ state: 'unavailable' })).finally(() => { inFlight = null; });
        return inFlight;
    };
}

if (typeof module !== 'undefined') module.exports = { createCookieSync };
