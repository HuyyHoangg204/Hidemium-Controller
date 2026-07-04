#!/usr/bin/env python3
"""
Build Script - Chia nho va ghep HTML partials
=============================================
Cau truc:
  ui/src/partials/       - Cac file partial HTML
  ui/src/index.template.html - Template chinh (chua cac placeholder)
  ui/dist/index.html     - Output cuoi cung phuc vu Flask

Cac lenh:
  python ui/build.py          - Ghep partials -> dist/index.html
  python ui/build.py extract  - Extract partials tu dist/index.html (lan dau)
  python ui/build.py watch    - Watch + auto-rebuild khi file thay doi
"""
import os, sys, time, hashlib

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT, "src", "partials")
DIST_DIR = os.path.join(ROOT, "dist")
TEMPLATE_FILE = os.path.join(ROOT, "src", "index.template.html")
OUTPUT_FILE = os.path.join(DIST_DIR, "index.html")

# Cac section can extract tu file goc (1-indexed, inclusive)
EXTRACT_SECTIONS = [
    ("_head.html",              4,    63),
    ("_login.html",            66,   129),
    ("_sidebar.html",         132,   250),
    ("_header.html",          252,   306),
    ("_tab_create_image.html", 307,   500),
    ("_tab_create_i2v.html",   501,   773),
    ("_tab_create_video.html", 776,  1020),
    ("_tab_video_gallery.html",1022, 1486),
    ("_tab_gallery.html", 0, 0),
    ("_tab_media_studio.html", 0, 0),
    ("_tab_media_studio_script.html", 0, 0),
    ("_tab_users.html",       1488,  1574),
    ("_tab_veo_accounts.html", 1576, 1703),
    ("_tab_account_tokens.html", 0, 0),
    ("_tab_job_results.html", 0, 0),
    ("_tab_queue_monitor.html", 0, 0),
    ("_tab_machine_registry.html", 0, 0),
    ("_tab_docs.html",        1704, 1720),
    ("_modals.html",          1706,  1959),
    ("_app_script.html",      1962,  2747),
]


def build():
    """Ghep partials -> dist/index.html theo template"""
    if not os.path.exists(TEMPLATE_FILE):
        print(f"[ERROR] Template file not found: {TEMPLATE_FILE}")
        print("        Chay 'python ui/build.py extract' truoc de tao partials.")
        sys.exit(1)

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        template = f.read()

    replaced = 0
    for fname, _, _ in EXTRACT_SECTIONS:
        placeholder = f"{{{{ include '{fname}' }}}}"
        if placeholder not in template:
            continue
        partial_path = os.path.join(SRC_DIR, fname)
        if not os.path.exists(partial_path):
            partial_path = os.path.join(PARTIALS_DIR, fname)
        if not os.path.exists(partial_path):
            print(f"[WARN] Partial not found, skip: {fname}")
            continue
        with open(partial_path, "r", encoding="utf-8") as f:
            content = f.read()
        template = template.replace(placeholder, content)
        replaced += 1

    os.makedirs(DIST_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(template)

    lines = template.count("\n") + 1
    size_kb = len(template.encode("utf-8")) / 1024
    print(f"[BUILD] OK ({replaced} partials) -> {OUTPUT_FILE}")
    print(f"        Lines: {lines:,} | Size: {size_kb:.1f} KB")


def extract():
    """Extract sections tu dist/index.html thanh cac partial rieng"""
    src = OUTPUT_FILE
    if not os.path.exists(src):
        print(f"[ERROR] Source file not found: {src}")
        sys.exit(1)

    with open(src, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total = len(lines)
    print(f"[INFO] Nguon: {src} ({total} lines)")
    os.makedirs(SRC_DIR, exist_ok=True)

    for fname, start, end in EXTRACT_SECTIONS:
        out = os.path.join(SRC_DIR, fname)
        # Khong ghi de neu da ton tai (bao ve file da chinh sua)
        if os.path.exists(out):
            print(f"[SKIP] Already exists: {fname}")
            continue
        chunk = lines[start - 1 : end]
        with open(out, "w", encoding="utf-8") as f:
            f.writelines(chunk)
        print(f"[EXTRACT] {fname:45s} ({end - start + 1} lines)")

    # Tao template file neu chua co
    if not os.path.exists(TEMPLATE_FILE):
        _create_template(lines)

    print(f"\n[DONE] Partials in: {SRC_DIR}")


def _create_template(all_lines):
    """Tao index.template.html tu file goc bang cach thay cac section bang placeholder"""
    result = []
    section_map = {(s, e): fname for fname, s, e in EXTRACT_SECTIONS}

    # Build a set of line ranges to skip
    skip_ranges = {}
    for fname, start, end in EXTRACT_SECTIONS:
        for ln in range(start, end + 1):
            skip_ranges[ln] = (fname, start)

    current_inserted = set()
    for i, line in enumerate(all_lines, start=1):
        if i in skip_ranges:
            fname, section_start = skip_ranges[i]
            if section_start not in current_inserted:
                result.append(f"{{{{ include '{fname}' }}}}\n")
                current_inserted.add(section_start)
            # skip original line
        else:
            result.append(line)

    with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
        f.writelines(result)
    print(f"[TEMPLATE] Created: {TEMPLATE_FILE}")


def watch():
    """Watch mode: tu dong rebuild khi file thay doi"""
    print("[WATCH] Bat dau giam sat ... (Ctrl+C de dung)")

    def get_hash():
        h = hashlib.md5()
        paths = [TEMPLATE_FILE] + [
            os.path.join(SRC_DIR, fname) for fname, _, _ in EXTRACT_SECTIONS
        ]
        for p in paths:
            if os.path.exists(p):
                h.update(open(p, "rb").read())
        return h.hexdigest()

    last = get_hash()
    build()
    while True:
        time.sleep(1)
        cur = get_hash()
        if cur != last:
            last = cur
            print(f"\n[CHANGE] {time.strftime('%H:%M:%S')} - Rebuilding...")
            build()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "extract":
        extract()
    elif cmd == "watch":
        watch()
    else:
        build()
