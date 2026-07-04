// ===================================
// CONTENT SCRIPT - Extension context (SIMPLIFIED)
// Chỉ inject injected.js + idle reload.
// Toàn bộ captcha polling đã chuyển sang background.js Service Worker.
// ===================================

(function () {
    const log = (m) => console.log(`[CaptchaExt] ${m}`);

    // ── Step 0: Giữ Service Worker SỐNG bằng persistent port ─────────────
    // Chrome MV3 kill SW sau ~30s idle. Mở port từ content script → SW sống
    // mãi khi bất kỳ tab labs.google nào còn mở.
    let _keepAlivePort = null;
    function connectKeepAlive() {
        try {
            _keepAlivePort = chrome.runtime.connect({ name: 'keepalive' });
            _keepAlivePort.onDisconnect.addListener(() => {
                _keepAlivePort = null;
                // Reconnect sau 1s (SW có thể đang restart)
                setTimeout(connectKeepAlive, 1000);
            });
        } catch (e) {
            // Extension context bị invalidated (reload extension)
            setTimeout(connectKeepAlive, 5000);
        }
    }
    connectKeepAlive();
    // Safety: reconnect mỗi 250s (port tự chết sau 5 phút nếu ko có message)
    setInterval(() => {
        if (_keepAlivePort) {
            try { _keepAlivePort.postMessage({ type: 'ping' }); } catch (_) {
                _keepAlivePort = null;
                connectKeepAlive();
            }
        } else {
            connectKeepAlive();
        }
    }, 250000);

    // ── Step 1: Inject injected.js into page context ──────────────────────
    // Vẫn cần để page load reCAPTCHA API (grecaptcha.enterprise)
    function injectPageScript() {
        const script = document.createElement('script');
        script.src = chrome.runtime.getURL('injected.js');
        script.type = 'text/javascript';
        (document.head || document.documentElement).appendChild(script);
        script.onload = () => { log('injected.js loaded'); script.remove(); };
        script.onerror = () => console.error('[CaptchaExt] ❌ Failed to inject injected.js');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectPageScript);
    } else {
        injectPageScript();
    }

    // ── Step 2: Ngăn Chrome Memory Saver đóng tab này ──
    try { chrome.runtime.sendMessage({ type: 'PREVENT_TAB_DISCARD' }); } catch (_) {}

    // ── Step 3: Idle reload 10 phút ──────────────────────────────────────
    // Nếu tab này không nhận lệnh giải nào trong 10 phút, F5 để giữ session
    // (Background.js cũng có idle reload, đây là backup per-tab)
    let lastPageActivity = Date.now();

    // Listen cho events từ background.js inject (executeScript)
    // Khi background.js inject solveCaptcha, page sẽ active → reset timer
    const observer = new MutationObserver(() => {
        lastPageActivity = Date.now();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });

    setInterval(() => {
        const idleMins = (Date.now() - lastPageActivity) / 60000;
        if (idleMins >= 10) {
            log(`🔄 Idle ${idleMins.toFixed(1)} phút. F5 tải lại trang...`);
            window.location.reload();
        }
    }, 60000);

    // ── Step 4: Listen for RELOAD_PAGE from background.js ──
    chrome.runtime.onMessage.addListener((msg) => {
        if (msg?.type === 'RELOAD_PAGE') {
            log('🔄 Reload requested by background — reloading...');
            window.location.reload();
        }
    });

    // ── Step 5: Cookie account sync (giữ cho background.js hút cookie) ──
    const urlParams = new URLSearchParams(window.location.search);
    let account = urlParams.get('captcha_account') || null;
    if (!account && window.location.hash.startsWith('#captcha_account=')) {
        account = decodeURIComponent(window.location.hash.substring('#captcha_account='.length));
    }
    if (account) {
        try {
            chrome.runtime.sendMessage({ type: 'SET_COOKIE_ACCOUNT', account });
            log(`Cookie account synced: ${account}`);
        } catch (_) {}
    }

    log('✅ Content script ready (captcha polling handled by background.js)');
})();