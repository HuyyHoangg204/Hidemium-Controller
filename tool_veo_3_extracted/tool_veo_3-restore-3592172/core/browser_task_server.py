"""
browser_task_server.py — HTTP bridge để Python dispatch task xuống Chrome
extension của 1 account cụ thể, extension thực thi fetch() trong tab
labs.google của account đó (cookie + TLS thật) rồi trả result về.

Pattern: long-poll giống `captcha_server.py` nhưng KEY khác biệt — dispatch
account-targeted: extension poll với `?account=<email>` chỉ nhận task của
chính account đó. Smart Queue khi pick acc=X → bridge.execute(account=X, …).

Flow:
  1. content.js → POST /browser-task/register   { account }
  2. background.js → GET /browser-task/poll?account=X (long-poll 25s)
  3. Python    → request_browser_task(X, action, payload) — block đợi
  4. background.js → executeScript fetch trong tab labs.google của X
  5. background.js → POST /browser-task/result   { requestId, status, body }
  6. Python    → unblock, trả result
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Global state ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
# account_email → last heartbeat (poll = heartbeat)
_registered: dict[str, float] = {}
# requestId → {account, action, payload, event, result}
_pending: dict[str, dict] = {}

HEARTBEAT_TIMEOUT = 30   # giây không poll → coi như Chrome chết
POLL_TIMEOUT = 25        # giây long-poll cho extension
DEFAULT_TASK_TIMEOUT = 180  # giây Python block đợi result (đủ cho 10 retry F5+UA loop trong extension)


def get_connected_accounts() -> list[str]:
    now = time.time()
    with _lock:
        return [a for a, ts in _registered.items() if now - ts < HEARTBEAT_TIMEOUT]


def is_account_connected(account: str) -> bool:
    now = time.time()
    with _lock:
        return (now - _registered.get(account, 0)) < HEARTBEAT_TIMEOUT


def request_browser_task(
    account: str,
    action: str,
    payload: dict,
    timeout: int = DEFAULT_TASK_TIMEOUT,
) -> dict:
    """Dispatch 1 task xuống Chrome của `account`, block tới khi result về.

    Returns:
        dict { ok: bool, status: int|None, body: str|None, error: str|None }
    """
    if not is_account_connected(account):
        logger.warning(f"[BrowserTask] Account {account} chưa kết nối (Chrome chưa mở/extension chưa register)")
        return {"ok": False, "status": None, "body": None,
                "error": f"Chrome of account {account} not connected"}

    req_id = f"btask_{uuid.uuid4().hex[:12]}"
    ev = threading.Event()
    with _lock:
        _pending[req_id] = {
            "account": account,
            "action": action,
            "payload": payload,
            "event": ev,
            "result": None,
            "sent": False,
            "created_at": time.time(),
        }

    logger.info(f"[BrowserTask] Dispatch {req_id} → {account} action={action}")
    waited = ev.wait(timeout)

    with _lock:
        entry = _pending.pop(req_id, None)

    if not waited or not entry:
        logger.error(f"[BrowserTask] ❌ Timeout {req_id} ({timeout}s)")
        return {"ok": False, "status": None, "body": None,
                "error": f"timeout after {timeout}s"}

    res = entry.get("result") or {}
    return {
        "ok": bool(res.get("status") == 200),
        "status": res.get("status"),
        "body": res.get("body"),
        "error": res.get("error"),
    }


# ── Flask routes ─────────────────────────────────────────────────────────────

def register_routes(app, require_api_key_func=None):
    """Đăng ký routes vào Flask app (gọi từ server.py)."""
    from flask import jsonify, request

    @app.route("/browser-task/register", methods=["POST"])
    def btask_register():
        data = request.get_json(silent=True) or {}
        acc = data.get("account") or "unknown"
        with _lock:
            _registered[acc] = time.time()
        return jsonify({"ok": True, "account": acc})

    @app.route("/browser-task/poll", methods=["GET"])
    def btask_poll():
        """Extension long-poll. Trả task của account hoặc 204."""
        acc = request.args.get("account")
        if not acc:
            return jsonify({"error": "account required"}), 400
        with _lock:
            _registered[acc] = time.time()  # heartbeat

        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            with _lock:
                for req_id, entry in _pending.items():
                    if entry.get("account") == acc and not entry.get("sent"):
                        entry["sent"] = True
                        return jsonify({
                            "requestId": req_id,
                            "action": entry["action"],
                            "payload": entry["payload"],
                        })
            time.sleep(0.2)
        return "", 204

    @app.route("/browser-task/result", methods=["POST"])
    def btask_result():
        """Extension POST result sau khi fetch xong."""
        data = request.get_json(silent=True) or {}
        req_id = data.get("requestId")
        if not req_id:
            return jsonify({"error": "requestId required"}), 400
        with _lock:
            entry = _pending.get(req_id)
        if not entry:
            return jsonify({"error": "unknown requestId"}), 404
        entry["result"] = {
            "status": data.get("status"),
            "body": data.get("body"),
            "error": data.get("error"),
        }
        entry["event"].set()
        logger.info(f"[BrowserTask] ← result {req_id} status={data.get('status')}")
        return jsonify({"ok": True})

    @app.route("/browser-task/unregister", methods=["POST"])
    def btask_unregister():
        data = request.get_json(silent=True) or {}
        acc = data.get("account")
        if acc:
            with _lock:
                _registered.pop(acc, None)
        return jsonify({"ok": True})

    @app.route("/browser-task/status", methods=["GET"])
    def btask_status():
        """Debug endpoint: list connected accounts + pending count."""
        with _lock:
            now = time.time()
            connected = {a: round(now - ts, 1) for a, ts in _registered.items()
                         if now - ts < HEARTBEAT_TIMEOUT}
            pending_by_acc: dict[str, int] = {}
            for e in _pending.values():
                a = e.get("account") or "?"
                pending_by_acc[a] = pending_by_acc.get(a, 0) + 1
        return jsonify({
            "connected": connected,  # {account: seconds_since_last_heartbeat}
            "pending": pending_by_acc,
            "total_pending": sum(pending_by_acc.values()),
        })

    logger.info("[BrowserTask] Routes registered: /browser-task/{register,poll,result,unregister,status}")
