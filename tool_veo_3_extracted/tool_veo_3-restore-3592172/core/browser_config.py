"""Load browser_config.json — file cấu hình DUY NHẤT cho browser identity + API vars."""

import os
import json
import sys

if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.abspath(os.path.join(_BASE_DIR, ".."))
_CONFIG_PATH = os.path.join(_ROOT, "browser_config.json")

_cache = None
# Giá trị được set dynamically tại runtime (ví dụ từ Chrome extension)
# Ưu tiên cao nhất, không bị mất khi file cache được reload
_runtime_overrides: dict = {}


def _load():
    global _cache
    if _cache is None:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception as e:
            print(f"[BrowserConfig] Cannot load {_CONFIG_PATH}: {e}")
            _cache = [{}]
    return _cache


def reload():
    """Xóa file cache, buộc đọc lại. Giữ nguyên _runtime_overrides."""
    global _cache
    _cache = None


def get(key, default=None, index=0):
    """Lấy giá trị từ browser_config.json. None/null trong JSON → trả về default.
    _runtime_overrides (từ Chrome extension) được ưu tiên cao nhất.
    """
    # 1. Runtime override từ Chrome extension (cao nhất)
    if key in _runtime_overrides:
        return _runtime_overrides[key]

    data = _load()
    val = None
    if isinstance(data, list) and data:
        # Ưu tiên lấy ở index được chỉ định
        if len(data) > index:
            val = data[index].get(key)

        if val is None:
            # Nếu không có hoặc index chỉ định không hợp lệ, duyệt tìm giá trị này ở các index khác (fallback)
            for cfg in data:
                if isinstance(cfg, dict):
                    v = cfg.get(key)
                    if v is not None:
                        val = v
                        break

    elif isinstance(data, dict):
        val = data.get(key)

    if val is not None:
        return val

    # FALLBACK cứng cho các param trọng yếu của trình duyệt
    FALLBACK_CONFIG = {
        "x_browser_copyright": "Copyright 2026 Google LLC. All Rights reserved.",
        "x_browser_year": "2026",
        # x-browser-validation: dùng per-instance random BVAL trong SelfCaptchaSolver,
        # KHÔNG đặt static ở đây vì cùng BVAL trên nhiều proxy IPs = bot signal.
        "x_browser_channel": "stable",
        "recaptcha_site_key": "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
        "recaptcha_co_param": "aHR0cHM6Ly9sYWJzLmdvb2dsZTo0NDM.",
        "recaptcha_action": "submit",
        "recaptcha_application_type": "RECAPTCHA_APPLICATION_TYPE_WEB",
        "tool": "PINHOLE",
        "user_paygate_tier": "PAYGATE_TIER_TWO",
        "email": "",
        "accept_language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
        "sec_ch_ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        # x-client-data: Chrome client data header — phải khớp với Chrome instance thực đang chạy
        # Lấy từ Chrome DevTools Network tab → Header của request bất kỳ đến Google
        "x_client_data": "CKmdygEIk6HLAQiFoM0BCOO2zwEImb/PAQjSwM8BCOLAzwEY+r/PAQ==",
        # x-browser-validation: BVAL header từ Chrome thực (captured từ curl)
        "x_browser_validation": "B2gM+WTW2xHE15IAjh8nDoMc5x0=",
    }

    if key in FALLBACK_CONFIG:
        return FALLBACK_CONFIG[key]

    return default


def set_value(key, value, index=0):
    """Lưu giá trị vào _runtime_overrides — persist qua cache reload."""
    global _runtime_overrides
    _runtime_overrides[key] = value


def save():
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[BrowserConfig] Cannot save: {e}")
