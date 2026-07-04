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
    } catch (_) {}
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
  try { data = JSON.parse(text); } catch (_) {}

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
  try { data = JSON.parse(text); } catch (_) {}
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

    const credits = await verifyCredits(session.token);
    if (!credits.ok) {
      return { ...base, stage: 'credits', email: session.email || '', expires: session.expires || '', token_sha8: sha8(session.token), credits_status: credits.status, error: credits.error, response: credits.response || '' };
    }

    return {
      ...base,
      ok: true,
      stage: 'ok',
      email: session.email || '',
      expires: session.expires || '',
      token_sha8: sha8(session.token),
      credits: credits.credits,
      tier: credits.tier || '',
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
  const candidates = [process.env.PYTHON || 'python', 'py'];
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
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
