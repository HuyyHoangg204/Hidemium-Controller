"""
Auto-updater: kiểm tra phiên bản mới và tự cập nhật exe.

version.json được bundle BÊN TRONG exe (qua PyInstaller datas).
Khi check update, so sánh version local (trong exe) với remote (trên GitHub).
"""

import os
import sys
import json
import requests
import subprocess


def _get_version_path():
    """Lấy đường dẫn version.json (trong exe hoặc thư mục source)."""
    if getattr(sys, "_MEIPASS", None):
        # Đang chạy từ exe → file nằm trong thư mục temp của PyInstaller
        return os.path.join(sys._MEIPASS, "version.json")
    else:
        # Đang chạy từ source
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "version.json"
        )


def get_local_version():
    """Đọc version hiện tại từ file bundled."""
    try:
        vpath = _get_version_path()
        if os.path.exists(vpath):
            with open(vpath, "r") as f:
                data = json.load(f)
            return data.get("version", "0.0")
    except Exception:
        pass
    return "0.0"


def _get_version_data():
    """Đọc toàn bộ version.json."""
    try:
        vpath = _get_version_path()
        if os.path.exists(vpath):
            with open(vpath, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def check_for_update():
    """
    Kiểm tra phiên bản mới trên bảng app_versions (Supabase Database).
    Returns: (has_update: bool, remote_version: str, download_url: str)
    """
    try:
        from core.auth import _get_config, _load_env

        _load_env()
        url, key = _get_config()
        if not url or not key or "__SUPABASE" in url:
            return False, "", ""

        endpoint = f"{url}/rest/v1/app_versions?select=version,download_url&order=id.desc&limit=1"
        resp = requests.get(
            endpoint,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return False, "", ""

        data = resp.json()
        if not data:
            return False, "", ""

        remote = data[0]
        remote_ver = remote.get("version", "0.0")
        download_url = remote.get("download_url", "")
        local_ver = get_local_version()

        try:
            rv = tuple(int(x) for x in remote_ver.split("."))
            lv = tuple(int(x) for x in local_ver.split("."))
            has_update = rv > lv
        except Exception:
            has_update = remote_ver != local_ver

        return has_update, remote_ver, download_url

    except Exception as e:
        print(f"[Updater] Check failed: {e}")
        return False, "", ""


def download_and_update(download_url, callback=None):
    """
    Tải exe mới và thay thế exe hiện tại.
    callback(progress_pct) để cập nhật UI.
    """
    if not download_url:
        return False, "Không có link tải."

    try:
        if not getattr(sys, "frozen", False):
            return False, "Chỉ hỗ trợ cập nhật từ exe."

        current_exe = sys.executable
        new_exe = current_exe + ".new"
        old_exe = current_exe + ".old"

        if callback:
            callback(0)

        resp = requests.get(download_url, stream=True, timeout=60)
        if resp.status_code != 200:
            return False, f"Tải thất bại: HTTP {resp.status_code}"

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0

        with open(new_exe, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if callback and total > 0:
                    callback(int(downloaded * 100 / total))

        if callback:
            callback(100)

        # Thay thế: current → old, new → current
        if os.path.exists(old_exe):
            os.remove(old_exe)
        os.rename(current_exe, old_exe)
        os.rename(new_exe, current_exe)

        return True, "Cập nhật thành công! Ứng dụng sẽ khởi động lại."

    except Exception as e:
        new_exe = (sys.executable + ".new") if getattr(sys, "frozen", False) else ""
        if new_exe and os.path.exists(new_exe):
            os.remove(new_exe)
        return False, f"Lỗi cập nhật: {str(e)}"


def restart_app():
    """Khởi động lại ứng dụng."""
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable])
        sys.exit(0)
