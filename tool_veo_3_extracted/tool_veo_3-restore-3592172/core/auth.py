"""
License verification qua Supabase REST API.
Chỉ dùng requests — hoạt động trên mọi máy, kể cả exe.

Bảo mật:
  - Dev: đọc từ .env
  - Exe: build.bat inject giá trị vào code trước khi build,
         giá trị ẩn bên trong exe, không thể đọc từ bên ngoài.
"""

import os
import requests
import platform
import subprocess
import uuid
import hashlib
from datetime import datetime, timezone


# ============================================================
# SUPABASE CONFIG
# Dev: đọc từ .env qua os.environ
# Exe: build.bat thay thế __PLACEHOLDER__ bằng giá trị thật
# ============================================================
_SUPABASE_URL = "https://ncurpuawqbeknkbjwfer.supabase.co"
_SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5jdXJwdWF3cWJla25rYmp3ZmVyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjQ1MTk1MDIsImV4cCI6MjA4MDA5NTUwMn0.07VLBYPfkuzB5MjHeh8DT-e3sZ9pynmDSjQDUpu_40A"
TABLE = "licenses"
TOOL_TYPE = "tool_veo_3"


def _get_config():
    """Lấy Supabase URL + Key (ưu tiên env, fallback hardcode)."""
    url = os.environ.get("SUPABASE_URL", _SUPABASE_URL)
    key = os.environ.get("SUPABASE_KEY", _SUPABASE_KEY)
    return url, key


def _load_env():
    """Đọc .env file nếu có (cho dev)."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# Auto-load .env khi import
_load_env()


def get_machine_id():
    """Lấy ID duy nhất của máy tính (dựa trên hardware UUID hoặc MAC address)."""
    try:
        if platform.system() == "Windows":
            output = (
                subprocess.check_output(
                    "wmic csproduct get uuid", stderr=subprocess.DEVNULL
                )
                .decode()
                .split("\n")[1]
                .strip()
            )
            if output and output != "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF":
                return hashlib.sha256(output.encode()).hexdigest()
    except Exception:
        pass

    # Fallback: MAC address
    mac = uuid.getnode()
    return hashlib.sha256(str(mac).encode()).hexdigest()


def verify_license(license_key):
    """
    Xác thực license key qua Supabase REST API.
    Luôn gọi server — không có bypass.
    """
    if not license_key:
        return False, "Vui lòng nhập mã kích hoạt."

    url, key = _get_config()
    if "__SUPABASE_" in url or "__SUPABASE_" in key:
        return False, "Chưa cấu hình Supabase. Kiểm tra file .env"

    try:
        # GET /rest/v1/licenses?key=eq.xxx
        resp = requests.get(
            f"{url}/rest/v1/{TABLE}",
            params={"key": f"eq.{license_key}", "limit": "1"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
            },
            timeout=10,
        )

        if resp.status_code != 200:
            return False, f"Lỗi server: {resp.status_code}"

        rows = resp.json()
        if not rows:
            return False, "Mã kích hoạt không tồn tại!"

        lic = rows[0]
        if not lic.get("is_active", True):
            return False, "Mã kích hoạt đã bị khóa! Liên hệ Admin."

        # Kiểm tra Tool Type
        if lic.get("type") != TOOL_TYPE:
            return False, f"Mã kích hoạt này không dành cho AutoVoice (Veo 3)!"

        # Kiểm tra Machine ID
        current_machine_id = get_machine_id()
        db_machine_id = lic.get("machine_id")

        if not db_machine_id:
            # Chưa khóa vào máy nào -> Khóa vào máy hiện tại
            patch_resp = requests.patch(
                f"{url}/rest/v1/{TABLE}",
                params={"key": f"eq.{license_key}"},
                json={
                    "machine_id": current_machine_id,
                    "last_login": datetime.now(timezone.utc).isoformat(),
                },
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=10,
            )
            if patch_resp.status_code not in (200, 204):
                return (
                    False,
                    f"Không thể khóa thiết bị (Cần thêm cột 'machine_id' kiểu text trong db Supabase). Lỗi: {patch_resp.text}",
                )
        elif db_machine_id != current_machine_id:
            # Đã khóa vào máy khác
            return False, "Mã kích hoạt này đã được sử dụng trên máy tính khác!"
        else:
            # Update last_login (non-blocking) trên đúng máy
            try:
                requests.patch(
                    f"{url}/rest/v1/{TABLE}",
                    params={"key": f"eq.{license_key}"},
                    json={"last_login": datetime.now(timezone.utc).isoformat()},
                    headers={
                        "apikey": key,
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                    timeout=5,
                )
            except Exception:
                pass

        return True, "Xác minh bản quyền thành công!"

    except requests.exceptions.ConnectionError:
        return False, "Không thể kết nối máy chủ. Kiểm tra mạng."
    except Exception as e:
        return False, f"Lỗi: {str(e)}"


def mark_license_verified(key):
    """No-op — tương thích với login.py."""
    pass


def check_license_active(license_key):
    """
    Kiểm tra nhanh license key còn active không (dùng cho check định kỳ).
    Returns: True nếu còn active, False nếu bị khóa hoặc lỗi.
    """
    if not license_key:
        return False
    try:
        url, key = _get_config()
        if "__SUPABASE_" in url:
            return True  # Dev mode, bỏ qua

        resp = requests.get(
            f"{url}/rest/v1/{TABLE}",
            params={"key": f"eq.{license_key}", "select": "is_active", "limit": "1"},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return True  # Lỗi mạng, không khóa user

        rows = resp.json()
        if not rows:
            return False
        return rows[0].get("is_active", True)
    except Exception:
        return True  # Lỗi mạng, không khóa user

