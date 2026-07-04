import os
import json
import time
import sys

if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.abspath(os.path.join(_BASE_DIR, ".."))
ACCOUNTS_FILE = os.path.join(_ROOT, "accounts.json")


import time
from typing import List, Dict, Any
from core import browser_config as bcfg


def _load_all() -> List[Dict[str, Any]]:
    """Load mảng từ browser_config.json (nơi extension tool get cookie lưu)."""
    data = bcfg._load()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _save_all(accounts: List[Dict[str, Any]]) -> bool:
    """Lưu lại mảng vào browser_config.json"""
    bcfg._cache = accounts
    bcfg.save()
    return True


def save_account(name: str, cookie_list: List[Dict[str, Any]]):
    """Lưu cookie thủ công (nếu xài import UI) vào bcfg index 0 (hoặc tạo cái mới)."""
    accounts = _load_all()
    if not accounts:
        accounts = [{}]

    parts = []
    expiration = None
    for c in cookie_list:
        if isinstance(c, dict) and c.get("name") and c.get("value"):
            parts.append(f"{c['name']}={c['value']}")
            if c.get("name") in ["__Secure-next-auth.session-token", "session_token"]:
                expiration = c.get("expirationDate") or c.get("expiration")

    full_cookie = "; ".join(parts)

    # Update acc có cùng tên hoặc tạo mới (thường lưu ở đầu)
    found = False
    for a in accounts:
        if a.get("cookie_account_name") == name or a.get("name") == name:
            a["full_cookie"] = full_cookie
            a["cookie_account_name"] = name
            a["name"] = name
            if expiration:
                a["cookie_expiration"] = expiration
                a["expiration"] = expiration
            found = True
            break

    if not found:
        accounts.insert(
            0,
            {
                "cookie_account_name": name,
                "name": name,
                "full_cookie": full_cookie,
                "cookie_expiration": expiration,
                "expiration": expiration,
            },
        )

    _save_all(accounts)


def get_accounts() -> List[Dict[str, Any]]:
    """Lấy danh sách cho UI list."""
    accounts = _load_all()
    res = []

    for i, a in enumerate(accounts):
        name = a.get("cookie_account_name") or a.get("name") or f"Account {i+1}"
        exp = a.get("cookie_expiration") or a.get("expiration")
        fc = a.get("full_cookie")

        if fc:
            res.append(
                {
                    "name": name,
                    "full_cookie": fc,
                    "expiration": exp,
                    "_origin_dict": a,  # giữ lại data gốc
                }
            )

    return res


def delete_account(name: str):
    """Xóa cookie khỏi browser_config."""
    accounts = _load_all()
    filtered = []
    for i, a in enumerate(accounts):
        acc_name = a.get("cookie_account_name") or a.get("name") or f"Account {i+1}"
        if acc_name != name:
            filtered.append(a)

    if not filtered:
        filtered = [{}]  # Giữ cấu trúc []

    _save_all(filtered)


def check_account_status(account: Dict[str, Any]) -> str:
    """Kiểm tra hết hạn."""
    exp = account.get("expiration")
    has_cookie = bool(account.get("full_cookie"))

    if not has_cookie:
        return "no_cookie"

    if exp:
        try:
            exp_ts = float(exp)
            now = time.time()
            if now > exp_ts:
                return "expired"
            days_left = int((exp_ts - now) / 86400)
            if days_left <= 3:
                return f"warning:{days_left}"
            return "ok"
        except Exception:
            pass

    return "ok"


def set_active_account(origin: Dict[str, Any]):
    """Đưa account này lên đầu list (index 0) để làm active."""
    accounts = _load_all()
    filtered = [a for a in accounts if a != origin]
    filtered.insert(0, origin)
    _save_all(filtered)
