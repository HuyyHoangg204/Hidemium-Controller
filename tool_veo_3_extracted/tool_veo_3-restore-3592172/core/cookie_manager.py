"""Cookie manager — standalone version for tao_video_ai."""

import os
import json
from typing import List, Optional


def extract_session_token_cookie(cookies) -> str:
    """Tìm và trả về chuỗi '__Secure-next-auth.session-token=<value>' từ cookie."""
    if not cookies:
        return None
    if isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict):
                n = c.get("name", "")
                if n == "__Secure-next-auth.session-token":
                    v = c.get("value")
                    if v:
                        return f"__Secure-next-auth.session-token={v}"
            elif isinstance(c, str) and c.strip():
                t = c.strip()
                if t.startswith("__Secure-next-auth.session-token="):
                    return t
                if t.startswith("ey"):
                    return f"__Secure-next-auth.session-token={t}"
        return None
    if isinstance(cookies, dict):
        n = cookies.get("name", "")
        if n == "__Secure-next-auth.session-token":
            v = cookies.get("value")
            if v:
                return f"__Secure-next-auth.session-token={v}"
        return None
    if isinstance(cookies, str):
        t = cookies.strip()
        if ";" in t:
            for part in t.split(";"):
                part = part.strip()
                if part.startswith("__Secure-next-auth.session-token="):
                    return part
            return None
        if t.startswith("ey") and "=" not in t[:20]:
            return f"__Secure-next-auth.session-token={t}"
        if t.startswith("__Secure-next-auth.session-token="):
            return t
    return None


# Resolve cookies.json — EXE: cạnh file exe, Dev: project root
import sys as _sys

if getattr(_sys, "frozen", False):
    # Đang chạy từ EXE → lưu cạnh file exe
    ROOT_DIR = os.path.dirname(_sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.abspath(os.path.join(_BASE_DIR, ".."))
DEFAULT_COOKIES_PATH = os.path.join(ROOT_DIR, "cookies.json")


def load_cookies(path: Optional[str] = None) -> List[str]:
    """Load cookies list from JSON file."""
    p = path or DEFAULT_COOKIES_PATH
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [x for x in data if x]
    except Exception:
        pass
    return []


def save_cookies(cookies: List[str], path: Optional[str] = None) -> bool:
    """Save cookies list to JSON file."""
    p = path or DEFAULT_COOKIES_PATH
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def normalize_cookie(token: str) -> str:
    """Normalize cookie input."""
    if not token:
        return token
    t = token.strip()
    if "__Secure-next-auth.session-token=" in t:
        t = t.split("__Secure-next-auth.session-token=")[1].split(";")[0].strip()
    if ";" in t:
        t = t.split(";")[0].strip()
    return t
