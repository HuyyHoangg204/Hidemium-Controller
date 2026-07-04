"""
Centralized config — loads from settings.json, falls back to defaults.
Standalone version for tao_video_ai.

Usage:
    from core import config as cfg
    key = cfg.get("GOOGLE_API_KEY")
"""

import os
import json
import sys

# Resolve project root: go up from core/ to tao_video_ai/
if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.abspath(os.path.join(_DIR, ".."))
SETTINGS_FILE = os.path.join(_ROOT, "settings.json")

# ── defaults (used if settings.json missing or key absent) ──
_DEFAULTS = {
    "GEMINI_API_KEY": "",
    "GOOGLE_API_KEY": "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
    "LARRY_API_KEY": "nk8n_d98673b97b42629db5c1aac8b4e80ad7bba103742500c4b5547fe373e4d48925",
    "X_BROWSER_CHANNEL": "stable",
    "X_BROWSER_VALIDATION": "AKIAtsVHZoiKbPixy+qSK1BgKWo=",
    "X_CLIENT_DATA": "CK+1yQEIk7bJAQimtskBCKmdygEI0o3LAQiVocsBCIWgzQEI2qrPARi8qcoB",
}

# ── internal store ──
_cfg: dict = {}


def _load():
    global _cfg
    _cfg = dict(_DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _cfg.update(data)
        except Exception as e:
            print(f"[config] Lỗi đọc settings.json: {e}")


def save():
    """Persist current config to settings.json."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[config] Lỗi ghi settings.json: {e}")


def reload():
    """Re-read from disk."""
    _load()


def get(key: str) -> str:
    return _cfg.get(key, _DEFAULTS.get(key, ""))


def set_value(key: str, value: str):
    _cfg[key] = value


def get_all() -> dict:
    return dict(_cfg)


# ── Auto-load on import ──
_load()


def __getattr__(name):
    """Allow `from config import GOOGLE_API_KEY` to always read current value."""
    if name in _DEFAULTS or name in _cfg:
        return _cfg.get(name, _DEFAULTS.get(name, ""))
    raise AttributeError(f"module 'config' has no attribute {name!r}")


# Pre-export for static analysis / IDE autocomplete
GOOGLE_API_KEY: str = _cfg.get("GOOGLE_API_KEY", "")
LARRY_API_KEY: str = _cfg.get("LARRY_API_KEY", "")
X_BROWSER_CHANNEL: str = _cfg.get("X_BROWSER_CHANNEL", "")
X_BROWSER_VALIDATION: str = _cfg.get("X_BROWSER_VALIDATION", "")
X_CLIENT_DATA: str = _cfg.get("X_CLIENT_DATA", "")
