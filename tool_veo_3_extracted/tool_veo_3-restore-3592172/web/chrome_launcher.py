"""
chrome_launcher.py — Tự động mở Chrome với extension captcha-solver.

Mỗi account có Chrome profile riêng → có thể đăng nhập nhiều Google account cùng lúc.
Extension được load tự động qua --load-extension flag, không cần cài tay.
"""

import os
import sys
import re
import subprocess
import threading
import logging

logger = logging.getLogger(__name__)

# ── Track các Chrome process đang chạy ────────────────────────────────────────
_lock = threading.Lock()
_chrome_processes: dict[str, subprocess.Popen] = {}   # account_email → Popen
_last_launch: dict[str, float] = {} # account_email → timestamp mở Chrome lần cuối

# ── Đường dẫn extension ───────────────────────────────────────────────────────
from core.paths import ROOT_DIR, CHROME_PROFILES_DIR
EXTENSION_DIR = os.path.join(ROOT_DIR, "captcha-solver-main", "src", "modules", "captcha", "extension")


def _safe_dirname(email: str) -> str:
    """Chuyển email thành tên thư mục hợp lệ."""
    return re.sub(r"[^\w\-.]", "_", email)


def is_chrome_running_for_profile(profile_dir: str) -> bool:
    """Kiểm tra OS xem có đang chạy chrome.exe với profile_dir này không."""
    import sys
    try:
        import psutil
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                name = proc.info.get('name', '')
                if name and 'chrome' in name.lower():
                    cmdline = proc.info.get('cmdline')
                    if cmdline:
                        for arg in cmdline:
                            if '--user-data-dir=' in arg and profile_dir in arg:
                                return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except ImportError:
        pass

    # Fallback dùng wmic cho Windows nếu không có psutil
    if sys.platform == "win32":
        import subprocess
        try:
            output = subprocess.check_output(
                'wmic process where "name=\'chrome.exe\'" get commandline',
                shell=True,
                text=True,
                errors='ignore'
            )
            for line in output.splitlines():
                if '--user-data-dir=' in line and profile_dir in line:
                    return True
        except Exception:
            pass
    return False


def find_chrome() -> str | None:
    """Tìm đường dẫn Chrome executable."""
    candidates = []
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe",
            r"C:\Program Files\Chromium\Application\chrome.exe",
        ]
        # Thử đọc từ registry
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
            path, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    for path in candidates:
        if os.path.exists(path):
            return path

    logger.error("[ChromeLauncher] Không tìm thấy Chrome. Hãy cài Google Chrome.")
    return None


def launch_chrome(account_email: str, captcha_server_port: int = 3001) -> dict:
    """
    Mở Chrome window mới cho account này với extension captcha-solver.
    
    Returns: { "success": bool, "pid": int|None, "error": str|None }
    """
    os.makedirs(CHROME_PROFILES_DIR, exist_ok=True)
    profile_dir = os.path.join(CHROME_PROFILES_DIR, _safe_dirname(account_email))

    if is_chrome_running_for_profile(profile_dir):
        logger.info(f"[ChromeLauncher] OS check: Chrome cho {account_email} đang chạy, bỏ qua gọi Popen.")
        return {"success": True, "pid": None, "already_running": True}

    with _lock:
        existing = _chrome_processes.get(account_email)
        if existing and existing.poll() is None:
            # Chrome process vẫn đang sống ngầm. Không gọi lệnh chrome.exe lần 2 
            # để tránh bị Windows tự động bật popup cửa sổ thu nhỏ lên.
            return {"success": True, "pid": existing.pid, "already_running": True}

        import time
        now = time.time()
        last = _last_launch.get(account_email, 0)
        if now - last < 60:
            return {"success": False, "pid": None, "error": f"Vừa mở Chrome cho {account_email} cánh đây vài giây, chặn spam tab."}
        _last_launch[account_email] = now

    chrome = find_chrome()
    if not chrome:
        return {"success": False, "pid": None, "error": "Chrome không tìm thấy trên máy này"}

    if not os.path.isdir(EXTENSION_DIR):
        return {"success": False, "pid": None, "error": f"Extension không tồn tại: {EXTENSION_DIR}"}

    import urllib.parse
    # URL labs.google Flow — trang đã login, extension sẽ chạy ngay
    target_url = f"https://labs.google/fx/vi/tools/flow#captcha_account={urllib.parse.quote(account_email)}"
    # Mở thêm tab grok.com để nuôi session Grok (bypass Cloudflare)
    grok_url = "https://grok.com"

    cmd = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--load-extension={EXTENSION_DIR}",
        "--new-window",
        # Kích thước cửa sổ nhỏ, không che UI chính
        "--window-size=900,700",
        "--disable-background-timer-throttling",  # CHỐNG NGỦ ĐÔNG TAB (Lý do 1)
        "--disable-renderer-backgrounding",
        target_url,
        grok_url,  # Tab 2: Grok — Extension sẽ tự hút cookie
    ]

    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE (Thu nhỏ, không cướp focus)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            startupinfo=startupinfo,
        )
        with _lock:
            _chrome_processes[account_email] = proc

        logger.info(f"[ChromeLauncher] Opened Chrome for {account_email} (pid={proc.pid})")
        return {"success": True, "pid": proc.pid}
    except Exception as e:
        logger.error(f"[ChromeLauncher] Lỗi mở Chrome: {e}")
        return {"success": False, "pid": None, "error": str(e)}


def close_chrome(account_email: str) -> dict:
    """Đóng Chrome process cho account này."""
    with _lock:
        proc = _chrome_processes.pop(account_email, None)

    if not proc:
        return {"success": False, "error": "Không có Chrome nào đang chạy cho account này"}

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    logger.info(f"[ChromeLauncher] Closed Chrome for {account_email}")
    return {"success": True}


def get_running_accounts() -> list[dict]:
    """Trả về danh sách accounts đang có Chrome mở."""
    result = []
    with _lock:
        for email, proc in list(_chrome_processes.items()):
            alive = proc.poll() is None
            if not alive:
                # Dọn dẹp process đã chết
                del _chrome_processes[email]
            else:
                result.append({"account": email, "pid": proc.pid})
    return result
