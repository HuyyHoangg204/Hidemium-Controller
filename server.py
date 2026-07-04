"""
Hidemium Controller - REST API Server
Chạy server này để máy khác có thể gọi vào điều khiển Hidemium qua HTTP.

Chạy:
    .venv\\Scripts\\python.exe server.py

Mặc định lắng nghe: http://0.0.0.0:5000
"""
from __future__ import annotations

import logging
import os
from typing import Any

from flask import Flask, jsonify, request

from app_logging import setup_logging
from hidemium_client import HidemiumClient, HidemiumError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("HIDEMIUM_BASE_URL", "http://127.0.0.1:2222")
API_KEY  = os.getenv("API_KEY", "")          # Đặt API_KEY để bảo mật (tuỳ chọn)
HOST     = os.getenv("SERVER_HOST", "0.0.0.0")
PORT     = int(os.getenv("SERVER_PORT", "5000"))

# Folder filter mặc định (chỉ 2 folder veo3)
DEFAULT_FOLDER_IDS: list[int] = [3053, 3024]

app = Flask(__name__)
logger = setup_logging("hidemium_controller.server", console=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_client() -> HidemiumClient:
    url = request.args.get("base_url", BASE_URL).strip()
    return HidemiumClient(url, timeout=60, logger=logger)


def check_api_key() -> bool:
    if not API_KEY:
        return True  # Không bật bảo mật
    key = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    return key == API_KEY


def err(msg: str, code: int = 400) -> Any:
    return jsonify({"ok": False, "error": msg}), code


def ok(data: Any = None, **kwargs: Any) -> Any:
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(kwargs)
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
@app.before_request
def auth_check() -> Any:
    if not check_api_key():
        return err("Unauthorized - sai API key", 401)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> Any:
    """Kiểm tra server có sống không."""
    return ok(message="Hidemium Controller API Server đang chạy")


@app.get("/health")
def health() -> Any:
    """Health check."""
    return ok(status="ok")


@app.get("/profiles")
def list_profiles() -> Any:
    """
    Lấy danh sách profile.

    Query params:
        is_local  (bool, mặc định false)
        page      (int,  mặc định 1)
        limit     (int,  mặc định 100)
        search    (str,  mặc định "")
        folder_id (int, có thể truyền nhiều lần) — bỏ trống = dùng 2 folder veo3
        all       (bool) — nếu true, lấy tất cả folder (bỏ qua folder filter)
    """
    try:
        is_local = request.args.get("is_local", "false").lower() in {"1", "true", "yes"}
        page     = int(request.args.get("page", 1))
        limit    = int(request.args.get("limit", 100))
        search   = request.args.get("search", "")

        if request.args.get("all", "false").lower() in {"1", "true"}:
            folder_ids: list[int] = []
        else:
            raw_ids = request.args.getlist("folder_id")
            folder_ids = [int(x) for x in raw_ids] if raw_ids else DEFAULT_FOLDER_IDS

        client   = get_client()
        response = client.list_profiles(is_local, page, limit, search, folder_ids)

        # Trích xuất list profiles từ response
        profiles = _extract(response)
        return ok(profiles, total=len(profiles))
    except HidemiumError as e:
        return err(str(e), 502)
    except Exception as e:
        logger.exception("list_profiles error")
        return err(str(e), 500)


@app.post("/open/<uuid>")
def open_profile(uuid: str) -> Any:
    """
    Mở profile.

    Body JSON (tuỳ chọn):
        command  (str) — chrome flags
        proxy    (str) — format: HTTP|host|port|user|pass
    """
    try:
        body    = request.get_json(silent=True) or {}
        command = body.get("command", "")
        proxy   = body.get("proxy", "")
        result  = get_client().open_profile(uuid, command, proxy)
        return ok(result)
    except HidemiumError as e:
        return err(str(e), 502)
    except Exception as e:
        logger.exception("open_profile error")
        return err(str(e), 500)


@app.post("/close/<uuid>")
def close_profile(uuid: str) -> Any:
    """Đóng profile."""
    try:
        result = get_client().close_profile(uuid)
        return ok(result)
    except HidemiumError as e:
        return err(str(e), 502)
    except Exception as e:
        logger.exception("close_profile error")
        return err(str(e), 500)


@app.get("/profile/<uuid>")
def get_profile(uuid: str) -> Any:
    """Lấy chi tiết 1 profile."""
    try:
        is_local = request.args.get("is_local", "false").lower() in {"1", "true"}
        result   = get_client().get_profile(uuid, is_local)
        return ok(result)
    except HidemiumError as e:
        return err(str(e), 502)
    except Exception as e:
        logger.exception("get_profile error")
        return err(str(e), 500)


@app.get("/folders")
def list_folders() -> Any:
    """Lấy danh sách folder."""
    try:
        is_local = request.args.get("is_local", "false").lower() in {"1", "true"}
        result   = get_client().list_folders(is_local)
        folders  = _extract(result)
        return ok(folders, total=len(folders))
    except HidemiumError as e:
        return err(str(e), 502)
    except Exception as e:
        logger.exception("list_folders error")
        return err(str(e), 500)


@app.get("/user")
def get_user() -> Any:
    """Lấy thông tin user/token."""
    try:
        result = get_client().get_user_uuid()
        return ok(result)
    except HidemiumError as e:
        return err(str(e), 502)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract(response: Any) -> list[dict]:
    """Trích xuất list từ Hidemium response."""
    if isinstance(response, list):
        return [x for x in response if isinstance(x, dict)]
    if not isinstance(response, dict):
        return []
    candidates = [response]
    for key in ("data", "result", "rows", "items", "browsers", "profiles", "content"):
        val = response.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            candidates.append(val)
    for obj in candidates:
        for key in ("content", "data", "rows", "items", "browsers", "profiles", "docs"):
            val = obj.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Hidemium Controller API Server")
    logger.info("Listening on http://%s:%s", HOST, PORT)
    if API_KEY:
        logger.info("Bao mat: X-API-Key header bat buoc")
    else:
        logger.warning("Khong co API_KEY - ai cung goi duoc! Dat API_KEY=xxx de bao mat.")
    logger.info("=" * 60)
    app.run(host=HOST, port=PORT, debug=False)
