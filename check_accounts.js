const { spawnSync } = require('child_process');

const emails = [
    'hoangphambaolong20102003@gmail.com',
    'phuc71956@gmail.com',
    'thongytb19@gmail.com',
    'tranduc6883@gmail.com',
    'trangnguyen3017@gmail.com',
    'tuconghuy1610@gmail.com',
];

const MONGO_URI = 'mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/';

const pyCode = `
import json, pymongo
client = pymongo.MongoClient(${JSON.stringify(MONGO_URI)})
db = client['veo_db']
emails = ${JSON.stringify(emails)}
docs = list(db['veo_accounts'].find({'name': {'$in': emails}}, {
    '_id': 0, 'name': 1, 'cookie': 1, 'is_active': 1,
    'assigned_to_user_id': 1, 'folder': 1, 'api_session': 1
}))
print(json.dumps(docs, ensure_ascii=False, default=str))
client.close()
`;

const r = spawnSync(process.env.PYTHON || 'python', ['-c', pyCode], { encoding: 'utf8' });
if (!r.stdout || r.status !== 0) {
    console.error('Python error:', r.stderr);
    process.exit(1);
}
const docs = JSON.parse(r.stdout);

async function testSession(cookieHeader) {
    try {
        const res = await fetch('https://labs.google/fx/api/auth/session', {
            headers: {
                accept: '*/*',
                'content-type': 'application/json',
                referer: 'https://labs.google/fx/vi/tools/flow',
                origin: 'https://labs.google',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                cookie: cookieHeader,
            },
        });
        const text = await res.text();
        let data = {};
        try { data = JSON.parse(text); } catch (_) {}
        return { status: res.status, token: data.access_token || '', email: data.user?.email || '', expires: data.expires || '' };
    } catch (e) {
        return { status: 0, error: e.message };
    }
}

function normCookie(cookie) {
    if (!cookie) return '';
    if (typeof cookie !== 'string') cookie = JSON.stringify(cookie);
    cookie = cookie.trim();
    if (cookie.startsWith('ey') && !cookie.slice(0, 20).includes('='))
        return '__Secure-next-auth.session-token=' + cookie;
    return cookie;
}

async function main() {
    console.log(`\nKiem tra ${emails.length} emails — tim thay ${docs.length} trong MongoDB\n`);

    for (const email of emails) {
        const doc = docs.find(d => d.name === email);
        console.log('═'.repeat(65));
        console.log(`📧 ${email}`);

        if (!doc) {
            console.log('  ❌ KHONG TIM THAY trong MongoDB!');
            continue;
        }

        const ckRaw = doc.cookie || '';
        const ckStr = typeof ckRaw === 'string' ? ckRaw : JSON.stringify(ckRaw);
        const hasCookie = ckStr.length > 20;

        console.log(`  is_active : ${doc.is_active}`);
        console.log(`  folder    : ${doc.folder || '(chua co folder)'}`);
        console.log(`  has_cookie: ${hasCookie} (${ckStr.length} ky tu)`);
        console.log(`  cookie_preview: ${ckStr.slice(0, 70)}...`);

        if (!hasCookie) {
            console.log('  ⚠️ COOKIE TRONG DB TRONG/TRONG — chua login hoac chua cap nhat cookie');
            continue;
        }

        const cookie = normCookie(ckStr);
        process.stdout.write('  Test session... ');
        const sess = await testSession(cookie);
        console.log(`HTTP ${sess.status}`);

        if (sess.status === 200 && sess.token?.startsWith('ya29')) {
            console.log(`  ✅ Cookie HOAT DONG | email=${sess.email} | expires=${sess.expires}`);
        } else {
            console.log(`  ❌ Cookie LOI | status=${sess.status} | token=${sess.token?.slice(0,20) || 'NONE'}`);
            console.log(`     → Nguyen nhan: Cookie het han hoac chua dang nhap vao labs.google`);
        }

        await new Promise(r => setTimeout(r, 1500));
    }
    console.log('\n' + '═'.repeat(65));
}

main().catch(e => { console.error('Fatal:', e); process.exit(1); });
