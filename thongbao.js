#!/usr/bin/env node
/*
  Test cookie Veo hiện đang lưu trong MongoDB account nào còn dùng được.

  Chạy:
    node test_current_cookies.js
    node test_current_cookies.js --active-only
    node test_current_cookies.js --limit=20

  Kết quả:
    - In bảng OK/FAIL ra console
    - Ghi file cookie_test_results.json ở root project
*/

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const MONGO_URI = process.env.MONGO_URI || 'mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/';
const DB_NAME = process.env.DB_NAME || 'veo_db';
const SESSION_URL = 'https://labs.google/fx/api/auth/session';
const CREDIT_URL = 'https://aisandbox-pa.googleapis.com/v1/credits?key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY';
const OUT_FILE = path.join(__dirname, 'cookie_test_results.json');

function argValue(name, fallback = null) {
    const item = process.argv.find((x) => x === `--${name}` || x.startsWith(`--${name}=`));
    if (!item) return fallback;
    const idx = item.indexOf('=');
    return idx >= 0 ? item.slice(idx + 1) : true;
}

function sha8(value) {
    return crypto.createHash('sha1').update(String(value || '')).digest('hex').slice(0, 8);
}

function normalizeCookie(cookie) {
    if (!cookie) return '';
    if (typeof cookie !== 'string') {
        if (Array.isArray(cookie)) {
            return cookie
                .filter((c) => c && c.name && c.value)
                .map((c) => `${c.name}=${c.value}`)
                .join('; ');
        }
        if (cookie.value) return String(cookie.value);
        return String(cookie);
    }
    const raw = cookie.trim();
    if (!raw) return '';
    if ((raw.startsWith('[') || raw.startsWith('{'))) {
        try {
            return normalizeCookie(JSON.parse(raw));
        } catch (_) { }
    }
    if (raw.includes(';') || raw.startsWith('__Secure-next-auth.session-token=')) return raw;
    if (raw.startsWith('ey') && !raw.slice(0, 20).includes('=')) {
        return `__Secure-next-auth.session-token=${raw}`;
    }
    return raw;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(timer);
    }
}

async function getAccessToken(cookieHeader) {
    const res = await fetchWithTimeout(SESSION_URL, {
        method: 'GET',
        headers: {
            accept: '*/*',
            'accept-language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
            'content-type': 'application/json',
            referer: 'https://labs.google/fx/vi/tools/flow',
            origin: 'https://labs.google',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
            cookie: cookieHeader,
        },
    }, 30000);

    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_) { }

    if (!res.ok) {
        return { ok: false, status: res.status, error: `session_http_${res.status}`, response: text.slice(0, 300) };
    }

    const token = data && data.access_token;
    const email = data && data.user && data.user.email;
    const expires = data && data.expires;
    if (!token || !String(token).startsWith('ya29')) {
        return { ok: false, status: res.status, error: 'missing_or_invalid_access_token', email, expires, response: text.slice(0, 300) };
    }

    return { ok: true, token, email, expires };
}

async function verifyCredits(token) {
    const res = await fetchWithTimeout(CREDIT_URL, {
        method: 'GET',
        headers: {
            authorization: `Bearer ${token}`,
            accept: '*/*',
            origin: 'https://labs.google',
            referer: 'https://labs.google/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
            'x-browser-channel': 'stable',
            'x-browser-year': '2026',
        },
    }, 30000);

    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_) { }
    if (!res.ok) {
        return { ok: false, status: res.status, error: `credits_http_${res.status}`, response: text.slice(0, 300) };
    }
    return {
        ok: true,
        status: res.status,
        credits: data && data.credits,
        tier: data && (data.userPaygateTier || data.serviceTier),
    };
}

async function testAccount(account, index, total) {
    const name = account.name || account.email || account.id || '<unknown>';
    const cookieHeader = normalizeCookie(account.cookie);
    const base = {
        index,
        total,
        account_id: account.id,
        account_name: name,
        is_active: account.is_active !== false,
        assigned_to_user_id: account.assigned_to_user_id || '',
        cookie_present: !!cookieHeader,
        cookie_sha8: cookieHeader ? sha8(cookieHeader) : '',
        ok: false,
    };

    if (!cookieHeader) {
        return { ...base, stage: 'cookie', error: 'missing_cookie' };
    }

    try {
        const session = await getAccessToken(cookieHeader);
        if (!session.ok) {
            return { ...base, stage: 'session', session_status: session.status, email: session.email || '', expires: session.expires || '', error: session.error, response: session.response || '' };
        }

        // Có access_token hợp lệ (ya29...) → OK luôn, không cần check credits
        return {
            ...base,
            ok: true,
            stage: 'ok',
            email: session.email || '',
            expires: session.expires || '',
            token_sha8: sha8(session.token),
        };
    } catch (err) {
        return { ...base, stage: 'exception', error: `${err.name || 'Error'}: ${err.message || err}` };
    }
}

function loadAccountsFromPython({ activeOnly, limit }) {
    const pyCode = `
import json, os, sys
import pymongo
MONGO_URI = os.environ.get('MONGO_URI') or ${JSON.stringify(MONGO_URI)}
DB_NAME = os.environ.get('DB_NAME') or ${JSON.stringify(DB_NAME)}
active_only = os.environ.get('COOKIE_TEST_ACTIVE_ONLY') == '1'
limit = int(os.environ.get('COOKIE_TEST_LIMIT') or '0')
query = {'is_active': {'$ne': False}} if active_only else {}
client = pymongo.MongoClient(MONGO_URI)
db = client[DB_NAME]
cur = db['veo_accounts'].find(query, {'_id': 0, 'id': 1, 'name': 1, 'cookie': 1, 'is_active': 1, 'assigned_to_user_id': 1, 'api_session': 1}).sort('name', 1)
if limit > 0:
    cur = cur.limit(limit)
print(json.dumps(list(cur), ensure_ascii=False, default=str))
client.close()
`;
    const env = {
        ...process.env,
        COOKIE_TEST_ACTIVE_ONLY: activeOnly ? '1' : '0',
        COOKIE_TEST_LIMIT: String(limit || 0),
    };
    const candidates = [
        process.env.PYTHON,                                                              // env var (uu tien nhat)
        path.join(__dirname, '.venv', 'Scripts', 'python.exe'),                          // Windows venv
        path.join(__dirname, '.venv', 'bin', 'python'),                                  // Linux/Mac venv
        path.join(__dirname, 'venv', 'Scripts', 'python.exe'),                           // Windows venv (ten khac)
        path.join(__dirname, 'venv', 'bin', 'python'),                                   // Linux venv
        'python', 'python3', 'py',                                                       // system python
    ].filter(Boolean);
    let lastError = '';
    for (const exe of candidates) {
        const result = spawnSync(exe, ['-c', pyCode], { encoding: 'utf8', env });
        if (result.status === 0 && result.stdout) {
            return JSON.parse(result.stdout);
        }
        lastError = result.stderr || result.error?.message || `exit=${result.status}`;
    }
    throw new Error(`Cannot load accounts via Python/pymongo: ${lastError}`);
}

async function main() {
    const activeOnly = process.argv.includes('--active-only');
    const limitRaw = argValue('limit', null);
    const limit = limitRaw ? Math.max(1, Number(limitRaw) || 0) : 0;

    let accounts = loadAccountsFromPython({ activeOnly, limit });

    console.log(`Testing ${accounts.length} cookies from MongoDB ${DB_NAME}.veo_accounts...`);
    const results = [];
    for (let i = 0; i < accounts.length; i++) {
        const result = await testAccount(accounts[i], i + 1, accounts.length);
        results.push(result);
        const icon = result.ok ? 'OK ' : 'FAIL';
        console.log(`[${i + 1}/${accounts.length}] ${icon} ${result.account_name} stage=${result.stage} email=${result.email || '-'} credits=${result.credits ?? '-'} err=${result.error || '-'}`);
        // Delay 1.5s giua cac account de tranh rate limit Google API
        if (i < accounts.length - 1) await new Promise(r => setTimeout(r, 1500));
    }

    const summary = {
        tested_at: new Date().toISOString(),
        total: results.length,
        ok: results.filter((r) => r.ok).length,
        failed: results.filter((r) => !r.ok).length,
        by_stage: results.reduce((acc, r) => {
            acc[r.stage] = (acc[r.stage] || 0) + 1;
            return acc;
        }, {}),
    };
    const payload = { summary, results };
    fs.writeFileSync(OUT_FILE, JSON.stringify(payload, null, 2), 'utf8');
    console.log('\nSummary:', summary);
    console.log(`Saved: ${OUT_FILE}`);

    // ── Telegram notification ──
    await sendTelegramSummary(summary, results);
}

// ─────────────────────────────────────────────────────────────
// Telegram
// ─────────────────────────────────────────────────────────────
const TELEGRAM_TOKEN   = process.env.TELEGRAM_TOKEN   || '8975004750:AAG3Eb-cAjGR9-yODiBNeRHnH9Ir2ns8X9o';
// Danh sach chat se gui thong bao (ca nhan + nhom)
const TELEGRAM_TARGETS = (process.env.TELEGRAM_CHAT_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
if (!TELEGRAM_TARGETS.length) {
    TELEGRAM_TARGETS.push('-5295419189');  // Nhom: Luong Veo3
}

const INTERVAL_MINUTES = parseInt(process.env.INTERVAL_MINUTES || '60', 10); // mỗi 60 phút

function tgRequest(method, body) {
    return new Promise((resolve, reject) => {
        const data = JSON.stringify(body);
        const req = require('https').request({
            hostname: 'api.telegram.org',
            path: `/bot${TELEGRAM_TOKEN}/${method}`,
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
        }, (res) => {
            let raw = '';
            res.on('data', (c) => raw += c);
            res.on('end', () => { try { resolve(JSON.parse(raw)); } catch { resolve(raw); } });
        });
        req.on('error', reject);
        req.write(data);
        req.end();
    });
}

async function detectGroupChatId() {
    try {
        const upd = await tgRequest('getUpdates', { limit: 50, timeout: 3 });
        if (!upd.ok || !upd.result) return '';
        // Ưu tiên group/supergroup
        for (let i = upd.result.length - 1; i >= 0; i--) {
            const msg = upd.result[i].message || upd.result[i].channel_post;
            if (!msg) continue;
            if (['group', 'supergroup', 'channel'].includes(msg.chat.type)) {
                console.log(`[Telegram] Phat hien nhom: "${msg.chat.title}" (${msg.chat.id})`);
                return String(msg.chat.id);
            }
        }
        // Fallback private
        const last = upd.result[upd.result.length - 1];
        const msg  = last && (last.message || last.channel_post);
        return msg ? String(msg.chat.id) : '';
    } catch (_) { return ''; }
}

async function sendTelegramSummary(summary, results) {
    const targets = TELEGRAM_TARGETS;
    if (!targets.length) {
        console.warn('[Telegram] Chua co CHAT_ID nao duoc cau hinh.');
        return;
    }

    const now    = new Date(summary.tested_at).toLocaleString('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh' });
    const icon   = summary.failed === 0 ? '✅' : (summary.ok === 0 ? '🔴' : '⚠️');

    // Danh sách email chi tiết
    const okItems   = results.filter((r) => r.ok);
    const failItems = results.filter((r) => !r.ok);

    const okLines = okItems.map((r) => {
        const email   = r.email   || r.account_name || '-';
        return `✅ <code>${email}</code>`;
    });

    const failLines = failItems.map((r) => {
        const email = r.email || r.account_name || '-';
        const reason = r.error || r.stage || '-';
        return `❌ <code>${email}</code> — ${reason}`;
    });

    const lines = [
        `${icon} <b>Báo cáo Cookie Veo3</b>`,
        `🕐 <b>${now}</b>`,
        ``,
        `📊 Tổng <b>${summary.total}</b> tài khoản`,
        `   ✅ Hoạt động: <b>${summary.ok}</b>`,
        `   ❌ Lỗi:       <b>${summary.failed}</b>`,
        ``,
    ];

    if (okLines.length) {
        lines.push(`<b>── EMAIL HOẠT ĐỘNG (${okLines.length}) ──</b>`);
        lines.push(...okLines);
        lines.push('');
    }

    if (failLines.length) {
        lines.push(`<b>── EMAIL LỖI / HẾT HẠN (${failLines.length}) ──</b>`);
        lines.push(...failLines);
    }

    const text   = lines.join('\n');
    const chunks = [];
    for (let i = 0; i < text.length; i += 4000) chunks.push(text.slice(i, i + 4000));

    // Gui den tat ca targets (ca nhan + nhom)
    for (const chatId of targets) {
        for (const chunk of chunks) {
            const res = await tgRequest('sendMessage', {
                chat_id:    chatId,
                text:       chunk,
                parse_mode: 'HTML',
            });
            if (res.ok) {
                console.log(`[Telegram] Gui OK → chat_id ${chatId}`);
            } else {
                console.error(`[Telegram] Loi chat ${chatId}:`, res.description || JSON.stringify(res));
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Daemon: chạy mỗi INTERVAL_MINUTES phút
// ─────────────────────────────────────────────────────────────
const isDaemon = process.argv.includes('--daemon');

async function runOnce() {
    const activeOnly = process.argv.includes('--active-only');
    const limitRaw   = argValue('limit', null);
    const limit      = limitRaw ? Math.max(1, Number(limitRaw) || 0) : 0;

    console.log(`\n${'─'.repeat(55)}`);
    console.log(`[${new Date().toLocaleString('vi-VN')}] Bat dau kiem tra...`);

    let accounts;
    try {
        accounts = loadAccountsFromPython({ activeOnly, limit });
    } catch (err) {
        console.error('Load accounts failed:', err.message);
        return;
    }

    console.log(`Testing ${accounts.length} cookies...`);
    const results = [];
    for (let i = 0; i < accounts.length; i++) {
        const result = await testAccount(accounts[i], i + 1, accounts.length);
        results.push(result);
        const ico = result.ok ? 'OK ' : 'FAIL';
        console.log(`[${i + 1}/${accounts.length}] ${ico} ${result.account_name} email=${result.email || '-'} credits=${result.credits ?? '-'} err=${result.error || '-'}`);
        if (i < accounts.length - 1) await new Promise(r => setTimeout(r, 1500));
    }

    const summary = {
        tested_at: new Date().toISOString(),
        total:     results.length,
        ok:        results.filter((r) => r.ok).length,
        failed:    results.filter((r) => !r.ok).length,
        by_stage:  results.reduce((acc, r) => { acc[r.stage] = (acc[r.stage] || 0) + 1; return acc; }, {}),
    };
    fs.writeFileSync(OUT_FILE, JSON.stringify({ summary, results }, null, 2), 'utf8');
    console.log('Summary:', summary);

    await sendTelegramSummary(summary, results);
}

async function daemonLoop() {
    console.log(`[Daemon] Khoi dong — chay moi ${INTERVAL_MINUTES} phut`);
    // Chạy ngay lần đầu
    await runOnce().catch((e) => console.error('runOnce error:', e));

    setInterval(async () => {
        await runOnce().catch((e) => console.error('runOnce error:', e));
    }, INTERVAL_MINUTES * 60 * 1000);
}

if (isDaemon) {
    daemonLoop();
} else {
    main().catch((err) => {
        console.error('Fatal:', err);
        process.exit(1);
    });
}
