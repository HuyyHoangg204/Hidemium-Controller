// ===================================
// INJECTED SCRIPT - Page context
// Purpose: expose grecaptcha.enterprise.execute() via postMessage
// Socket.IO is handled by content.js (extension context, no CSP issues)
// ===================================

(async function () {
    const log = (msg) => console.log(`[CaptchaPage] ${msg}`);
    const err = (msg) => console.error(`[CaptchaPage] ❌ ${msg}`);

    log('🚀 Starting...');

    // Read account email from URL param
    const urlParams = new URLSearchParams(window.location.search);
    let accountEmail = urlParams.get('captcha_account') || null;

    if (!accountEmail && window.location.hash.startsWith('#captcha_account=')) {
        accountEmail = decodeURIComponent(window.location.hash.substring('#captcha_account='.length));
    }

    const SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';

    // Notify content.js about account
    if (accountEmail) {
        window.postMessage({ type: 'CAPTCHA_ACCOUNT_SET', account: accountEmail }, '*');
        log(`🔑 Account: ${accountEmail}`);
    }

    // Wait for reCAPTCHA enterprise to load
    async function waitForRecaptcha(maxWaitMs = 30000) {
        const start = Date.now();
        while (Date.now() - start < maxWaitMs) {
            if (window.grecaptcha?.enterprise?.execute) {
                log('✅ reCAPTCHA ready!');
                return true;
            }
            await new Promise(r => setTimeout(r, 500));
        }
        throw new Error('reCAPTCHA not loaded after 30s');
    }

    // Solve captcha and return token
    async function solveCaptcha(requestId, action = 'IMAGE_GENERATION') {
        // Delay ngẫu nhiên 2-4s để giả lập hành vi người dùng, tránh bị Google phát hiện
        const delayMs = 2000 + Math.random() * 2000;
        log(`[SOLVE ${requestId}] ⏳ Chờ delay giả lập người dùng: ${Math.round(delayMs)}ms...`);
        await new Promise(r => setTimeout(r, delayMs));
        
        log(`[SOLVE ${requestId}] 🎯 CHÍNH THỨC GỌI window.grecaptcha.enterprise.execute (action=${action})...`);
        try {
            const token = await window.grecaptcha.enterprise.execute(SITE_KEY, { action });
            if (!token) {
                log(`[SOLVE ${requestId}] 🚨 GOOGLE TRẢ VỀ TOKEN RỖNG (NULL/UNDEFINED)!`);
                return null;
            }
            const tokenPreview = token.substring(0, 15) + '...' + token.slice(-10);
            log(`[SOLVE ${requestId}] ✅ GOOGLE TRẢ MÃ THÀNH CÔNG! (Độ dài: ${token.length}). Preview: ${tokenPreview}`);
            return token;
        } catch (e) {
            err(`[SOLVE ${requestId}] 🚨 LỖI TỪ PHÍA GOOGLE RECAPTCHA: ${e.message}`);
            throw e;
        }
    }

    // Listen for solve requests from content.js
    window.addEventListener('message', async (event) => {
        if (event.source !== window) return;
        const msg = event.data;

        if (msg?.type === 'CAPTCHA_SOLVE_REQUEST') {
            const { requestId, action } = msg;
            log(`[NHẬN LỆNH] 📥 Injected.js vừa tiếp nhận yêu cầu giải: reqId=${requestId}`);
            try {
                const token = await solveCaptcha(requestId, action);
                log(`[TRẢ KẾT QUẢ] 📤 Bắn gói hàng Token ngược về lại mạch content.js cho reqId=${requestId}`);
                window.postMessage({ type: 'CAPTCHA_SOLVE_RESPONSE', requestId, token }, '*');
            } catch (e) {
                err(`[TRẢ KẾT QUẢ - LỖI] ❌ Đứt gãy ở reqId=${requestId}: ${e.message}`);
                window.postMessage({ type: 'CAPTCHA_SOLVE_RESPONSE', requestId, token: null, error: e.message }, '*');
            }
        }
    });

    // Wait for reCAPTCHA then notify ready
    try {
        await waitForRecaptcha();
        log('🎯 Ready to solve captchas on request from content.js');

        // Expose for manual debugging
        window.captchaPage = { solveCaptcha, accountEmail, siteKey: SITE_KEY };
    } catch (e) {
        err(e.message);
    }
})();
