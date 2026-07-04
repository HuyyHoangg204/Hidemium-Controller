"""
Entry point để chạy API server.
Cách dùng:
    cd d:\tool_veo_3
    python web/run.py
    python web/run.py --port 9000
"""

import os
import sys

# ━━━ FIX: encoding cho VPS console ━━━
os.environ["PYTHONIOENCODING"] = "utf-8:replace"
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Thêm root dir vào path để import core/
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Compatibility alias: source folder hiện là web/ nhưng các module cũ vẫn import
# vietauto_api.*. Tạo namespace package trỏ vào web/ để chạy được cả:
#   python web/run.py
# mà không cần rename folder hay sửa hàng loạt import nội bộ.
if "vietauto_api" not in sys.modules:
    import types
    _pkg = types.ModuleType("vietauto_api")
    _pkg.__path__ = [WEB_DIR]
    sys.modules["vietauto_api"] = _pkg

# Load .env cho development
env_path = os.path.join(ROOT_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # Import server module-level app (triggers SafeStream + logging setup)
    from web.server import app

    # ── Init Captcha HTTP server (dùng Flask routes /captcha/*, không cần port riêng) ──
    # Chrome extension kết nối qua HTTP polling — bypass CSP hoàn toàn
    try:
        from core.captcha_server import start_captcha_server
        start_captcha_server()
        print("[STARTUP] ✅ Captcha HTTP polling server ready (port 8080 /captcha/*)")
    except Exception as _e:
        print(f"[STARTUP] Captcha server init failed: {_e}")

    # ── Init Captcha Token Pool (prefetch — producer fill kho ngay khi boot) ──
    # Mặc định bơm cho VIDEO_GENERATION (workload chính). _solve_captcha_smart
    # sẽ set_action() khi gặp task IMAGE_GENERATION → producer chuyển sub-pool.
    try:
        from core import browser_config as _bcfg
        _video_browser_runtime = bool(_bcfg.get("veo_browser_runtime_enabled", True))
        if _video_browser_runtime:
            print(
                "[STARTUP] ⏭️  Skip VIDEO_GENERATION captcha pool prefetch "
                "(browser runtime resolves reCAPTCHA in-page)"
            )
        else:
            from core.captcha_pool import (
                get_pool, TARGET_POOL_SIZE, TOKEN_TTL, EXTENSION_BATCH_MAX,
            )
            get_pool().start(action="VIDEO_GENERATION")
            print(
                f"[STARTUP] ✅ Captcha token pool prefetch started "
                f"(target={TARGET_POOL_SIZE}/action, ttl={TOKEN_TTL}s, "
                f"ext_batch_max={EXTENSION_BATCH_MAX}, default_action=VIDEO_GENERATION)"
            )
    except Exception as _e:
        print(f"[STARTUP] Captcha pool init failed: {_e}")

    try:
        try:
            from waitress import serve
            print(f"""
+------------------------------------------+
|     VietAuto API (Waitress / Windows)    |
+------------------------------------------+
|  Local : http://localhost:{args.port}
+------------------------------------------+
            """)
            # ── Waitress tuning ──
            # threads=128: chịu được 20+ captcha long-poll (giữ thread tới 5s/req)
            #              + UI admin polling (queue-monitor/logs-tail) + headroom
            # connection_limit=1000: với HTTP/1.1 keep-alive + nhiều tab Chrome
            #                        con số cũ 200 bị đạt trần quá nhanh
            # channel_timeout=30: không đóng connection giữa long-poll,
            #                     tránh reconnect storm
            # cleanup_interval=30: dọn dẹp channel idle định kỳ
            serve(app, host=args.host, port=args.port,
                  threads=128,
                  connection_limit=1000,
                  channel_timeout=30,
                  cleanup_interval=30,
                  asyncore_loop_timeout=1)
        except ImportError:
            print("[WARN] Waitress not installed, using Flask dev server")
            app.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Server stopped.")
        os._exit(0)
else:
    from web.server import app
