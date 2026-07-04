"""
captcha_server.py — HTTP-based captcha coordination server.

Không dùng Socket.IO (bị CSP chặn trong Chrome extension).
Dùng HTTP endpoints + polling — content.js fetch() bypass CSP hoàn toàn.

Flow:
  1. content.js: POST /captcha/register  { account: "abc@gmail.com" }
  2. content.js: GET  /captcha/poll?account=abc@gmail.com  (long-poll 25s)
  3. Python:     POST /captcha/request   { account, action }  → nhận token
  4. content.js: POST /captcha/result    { account, requestId, token }
"""

import threading
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# ── Global state ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_registered: dict[str, float] = {}       # account_email → last_heartbeat timestamp
_pending_requests: dict[str, dict] = {}  # requestId → {account, action, event, token}
_server_started = False
_rr_index = 0  # Round-robin counter cho multi-tab dispatch
# Track trạng thái thực tế của mỗi captcha worker (solving hay idle)
_worker_activity: dict[str, dict] = {}   # worker_id → {"phase": "SOLVING"|"IDLE", "request_id": str, "since": float}


HEARTBEAT_TIMEOUT = 30   # giây không có poll → coi là disconnected
POLL_TIMEOUT = 5           # giây long-poll (1 worker/browser, không lo chiếm connection)
CAPTCHA_TIMEOUT = 15  # 15 giây — fail fast, chuyển account nhanh


def get_connected_accounts() -> list[str]:
    """Trả về danh sách accounts có Chrome đang active (heartbeat < 30s)."""
    now = time.time()
    with _lock:
        return [acc for acc, ts in _registered.items() if now - ts < HEARTBEAT_TIMEOUT]


def is_account_connected(account: str) -> bool:
    now = time.time()
    with _lock:
        ts = _registered.get(account, 0)
        return (now - ts) < HEARTBEAT_TIMEOUT


def request_captcha(action: str = "IMAGE_GENERATION", account: Optional[str] = None,
                    timeout: int = CAPTCHA_TIMEOUT) -> Optional[str]:
    """
    Yêu cầu extension giải captcha. Đặt job vào pool chung, bất kì worker nào rảnh sẽ nhận.
    Blocking call.

    Returns:
        token string hoặc None nếu timeout/lỗi
    """
    connected = get_connected_accounts()
    if not connected:
        logger.warning("[CaptchaServer] Không có Chrome nào đang kết nối để giải captcha!")
        return None

    req_id = f"req_{uuid.uuid4().hex[:12]}"
    
    with _lock:
        # Gắn account=None để đánh dấu là "việc chưa có ai nhận"
        _pending_requests[req_id] = {
            "action": action, 
            "account": None, 
            "event": threading.Event(),
            "token": None,
        }

    logger.info(f"[CaptchaServer] Đăng job mới {req_id} (action={action}) chờ Worker hái...")

    # Chờ kết quả hoặc timeout (truy cập event từ dict để đảm bảo không bị xoá)
    req = _pending_requests[req_id]
    waited_ok = req["event"].wait(timeout)

    with _lock:
        req = _pending_requests.pop(req_id, None)

    if not waited_ok or not req:
        logger.error(f"[CaptchaServer] ❌ Timeout/Hủy khi chờ token cho {req_id}")
        return None

    token = req.get("token")
    if not token:
        logger.error(f"[CaptchaServer] ❌ Extension giải thất bại/trả về null cho {req_id}")
    return token


# ── Flask HTTP endpoints (đăng ký vào Flask app từ server.py) ─────────────────

def register_routes(app, require_api_key_func=None):
    """
    Đăng ký các HTTP endpoints vào Flask app.
    Gọi từ server.py sau khi create_app().
    """
    from flask import request, jsonify

    @app.route("/captcha/register", methods=["POST"])
    def captcha_register():
        """content.js gọi khi extension load để đăng ký account."""
        data = request.get_json(silent=True) or {}
        account = data.get("account") or "unknown"
        with _lock:
            _registered[account] = time.time()
        logger.info(f"[CaptchaServer] Chrome registered: account={account}")
        return jsonify({"ok": True, "account": account})

    @app.route("/captcha/heartbeat", methods=["POST"])
    def captcha_heartbeat():
        """content.js gọi mỗi 10s để báo Chrome còn sống."""
        data = request.get_json(silent=True) or {}
        account = data.get("account") or "unknown"
        with _lock:
            _registered[account] = time.time()
        return jsonify({"ok": True})

    @app.route("/captcha/poll", methods=["GET"])
    def captcha_poll():
        """
        Long-poll: background.js gọi liên tục, server giữ kết nối tối đa POLL_TIMEOUT giây.
        Trả về pending request nếu có, hoặc 204 nếu timeout.
        """
        account = request.args.get("account")
        if not account:
            return jsonify({"error": "account required"}), 400

        # Update heartbeat
        with _lock:
            _registered[account] = time.time()

        # Tìm pending request — rút ra khỏi queue chung
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            with _lock:
                for req_id, req in _pending_requests.items():
                    if req.get("account") is None and "sent" not in req:
                        # Gán việc cho worker này
                        req["account"] = account
                        req["sent"] = True
                        return jsonify({
                            "requestId": req_id,
                            "action": req["action"],
                        })
            time.sleep(0.2)

        return "", 204  # No Content — không có gì, poll lại

    @app.route("/captcha/result", methods=["POST"])
    def captcha_result():
        """background.js POST token sau khi giải xong."""
        data = request.get_json(silent=True) or {}
        request_id = data.get("requestId")
        token = data.get("token")
        account = data.get("account", "?")

        if not request_id:
            return jsonify({"error": "requestId required"}), 400

        with _lock:
            pending = _pending_requests.get(request_id)

        if pending:
            pending["token"] = token
            pending["event"].set()
            if not token:
                logger.warning(f"[CaptchaServer] ⚠️ Received NULL token from {account} for {request_id}. Unlocking wait early.")
            else:
                logger.info(f"[CaptchaServer] ✅ Token received from {account} for {request_id} (len={len(token)})")
            return jsonify({"ok": True})
        else:
            logger.warning(f"[CaptchaServer] Unknown requestId or expired: {request_id}")
            return jsonify({"error": "unknown requestId"}), 404

    @app.route("/captcha/unregister", methods=["POST"])
    def captcha_unregister():
        """content.js gọi khi Chrome đóng."""
        data = request.get_json(silent=True) or {}
        account = data.get("account")
        if account:
            with _lock:
                _registered.pop(account, None)
            logger.info(f"[CaptchaServer] Chrome unregistered: account={account}")
        return jsonify({"ok": True})

    @app.route("/api/browser-headers", methods=["POST"])
    def receive_browser_headers():
        """
        Chrome extension gửi x-client-data và x-browser-validation headers thực từ Chrome.
        Lưu vào browser_config dynamically — luôn dùng giá trị mới nhất từ Chrome thực.
        """
        data = request.get_json(silent=True) or {}
        updated = []

        from core import browser_config as bcfg_mod
        for key in ("x_client_data", "x_browser_validation", "x_browser_channel"):
            val = data.get(key)
            if val:
                bcfg_mod.set_value(key, val)
                updated.append(key)

        if updated:
            bcfg_mod.reload()  # clear cache để lần get() tiếp theo dùng giá trị mới
            logger.info(f"[BrowserHeaders] ✅ Updated from Chrome extension: {updated}")

        return jsonify({"ok": True, "updated": updated})

    logger.info("[CaptchaServer] HTTP routes registered: /captcha/register, /captcha/poll, /captcha/result")


def start_captcha_server(port: int = 3001):
    """Không cần Socket.IO nữa — dùng Flask HTTP endpoints."""
    global _server_started
    _server_started = True  # mark as started (routes registered separately)
    logger.info("[CaptchaServer] HTTP-based captcha server ready (no separate port needed)")
