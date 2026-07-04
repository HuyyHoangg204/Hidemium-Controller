import os, sys, shutil

ROOT = 'd:/tool_veo_3/ui'
SRC_DIR = os.path.join(ROOT, 'src', 'partials')
DIST_DIR = os.path.join(ROOT, 'dist')
TEMPLATE_FILE = os.path.join(ROOT, 'src', 'index.template.html')
OUTPUT_FILE = os.path.join(DIST_DIR, 'index.html')
LOG_FILE = os.path.join(ROOT, 'build_result.txt')

EXTRACT_SECTIONS = [
    ('_head.html',              4,    63),
    ('_login.html',            66,   129),
    ('_sidebar.html',         132,   250),
    ('_header.html',          252,   306),
    ('_tab_create_image.html', 307,   500),
    ('_tab_create_i2v.html',   501,   773),
    ('_tab_create_video.html', 776,  1020),
    ('_tab_video_gallery.html',1022, 1486),
    ('_tab_users.html',       1488,  1574),
    ('_tab_veo_accounts.html', 1576, 1703),
    ('_modals.html',          1706,  1959),
    ('_app_script.html',      1962,  2750),
]

log = open(LOG_FILE, 'w', encoding='utf-8')

with open(TEMPLATE_FILE, 'r', encoding='utf-8') as f:
    template = f.read()

replaced = 0
missing = []
for fname, _, _ in EXTRACT_SECTIONS:
    placeholder = f"{{{{ include '{fname}' }}}}"
    if placeholder not in template:
        log.write(f'PLACEHOLDER NOT FOUND: {fname}\n')
        continue
    partial_path = os.path.join(SRC_DIR, fname)
    if not os.path.exists(partial_path):
        missing.append(fname)
        log.write(f'MISSING FILE: {fname}\n')
        continue
    with open(partial_path, 'r', encoding='utf-8') as f:
        content = f.read()
    template = template.replace(placeholder, content)
    replaced += 1
    log.write(f'OK: {fname}\n')

# Back up original
if os.path.exists(OUTPUT_FILE):
    shutil.copy(OUTPUT_FILE, OUTPUT_FILE + '.bak')

with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    f.write(template)

lines = template.count('\n') + 1
size_kb = len(template.encode('utf-8')) / 1024
log.write(f'\nBUILD OK - {replaced} partials replaced\n')
log.write(f'Lines: {lines} | Size: {size_kb:.1f} KB\n')
if missing:
    log.write(f'MISSING: {missing}\n')
log.close()
print('DONE')
