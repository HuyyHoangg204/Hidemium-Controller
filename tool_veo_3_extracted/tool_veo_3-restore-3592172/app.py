import os
import sys

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import json

from PySide6.QtCore import Qt, QTimer, QEvent, Slot
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QGridLayout,
    QFrame,
    QLabel,
    QPushButton,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QHeaderView,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QSizePolicy,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QLineEdit,
    QStackedWidget,
    QListWidget,
)

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from core.paths import ROOT_DIR

from core.cookie_manager import load_cookies, save_cookies, extract_session_token_cookie
from workers.veo_worker import VeoWorker
from workers.imagen_worker import ImagenWorker
from ui.login import LoginDialog


def shadow(blur=20, dx=0, dy=4, color="#18000000"):
    e = QGraphicsDropShadowEffect()
    e.setBlurRadius(blur)
    e.setOffset(dx, dy)
    e.setColor(QColor(color))
    return e


def hex_to_rgb(h):
    h = h.lstrip("#")
    return f"{int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)}"


class GlassCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("glass_card")


class PromptEdit(QTextEdit):
    """QTextEdit bắt Enter → gọi flush_callback, Shift+Enter xuống dòng bình thường."""

    def __init__(self, flush_callback, parent=None):
        super().__init__(parent)
        self._flush = flush_callback

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._flush()
                self.clear()  # chắc chắn xoá trước khi Qt xử lý
                event.accept()
                return
        super().keyPressEvent(event)



class ChromeSettingsDialog(QDialog):
    """Dialog cấu hình Chrome profile để giải captcha tự động."""

    from core.paths import CAPTCHA_WORKER_PROFILE as PROFILE_DIR

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ Cài đặt Chrome — Đăng nhập tài khoản")
        self.setFixedSize(520, 400)
        self.setStyleSheet("background:#141728; color:#e2e8f0;")
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(14)
        lay.setContentsMargins(28, 24, 28, 24)

        title = QLabel("🌐  Cấu hình Chrome để tự giải Captcha")
        title.setStyleSheet(
            "font-size:16px; font-weight:800; color:#f1f2f6; margin-bottom:4px;"
        )
        lay.addWidget(title)

        desc = QLabel(
            "1. Bấm <b>Mở Chrome để Đăng nhập</b><br>"
            "2. Đăng nhập tài khoản Google (Veo3 Ultra)<br>"
            "3. Đảm bảo vào được <b>labs.google/fx/tools/flow</b><br>"
            "4. Đóng Chrome lại → Bấm <b>Xác nhận đã đăng nhập</b>"
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet(
            "font-size:12px; color:#94a3b8; line-height:1.8; "
            "background:#0d0f1a; border-radius:10px; padding:12px 16px; "
            "border:1px solid #1e2540;"
        )
        lay.addWidget(desc)

        profile_exists = os.path.exists(self.PROFILE_DIR)
        self.status_lbl = QLabel(
            "✅  Đã có profile Chrome — Sẵn sàng giải captcha"
            if profile_exists
            else "⚠️  Chưa có profile — Cần đăng nhập lần đầu"
        )
        self.status_lbl.setStyleSheet(
            f"font-size:12px; font-weight:600; "
            f"color:{'#22c55e' if profile_exists else '#f59e0b'}; "
            "background:#0d0f1a; border-radius:8px; padding:10px 16px; "
            "border:1px solid #1e2540;"
        )
        lay.addWidget(self.status_lbl)

        lay.addStretch()

        btn_open = QPushButton("🌐   Mở Chrome để Đăng nhập")
        btn_open.setFixedHeight(44)
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.setStyleSheet(
            "QPushButton { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #3b82f6,stop:1 #6c63ff); color:#fff; font-size:13px; "
            "font-weight:700; border-radius:10px; border:none; }"
            "QPushButton:hover { opacity:0.9; }"
            "QPushButton:pressed { opacity:0.8; }"
        )
        btn_open.clicked.connect(self._open_chrome_for_login)
        lay.addWidget(btn_open)

        btn_verify = QPushButton("✅   Xác nhận đã đăng nhập")
        btn_verify.setFixedHeight(40)
        btn_verify.setCursor(Qt.PointingHandCursor)
        btn_verify.setStyleSheet(
            "QPushButton { background:rgba(34,197,94,0.1); color:#22c55e; "
            "font-size:12px; font-weight:600; border-radius:10px; "
            "border:1px solid rgba(34,197,94,0.3); }"
            "QPushButton:hover { background:rgba(34,197,94,0.2); }"
        )
        btn_verify.clicked.connect(self._verify_login)
        lay.addWidget(btn_verify)

        btn_close = QPushButton("Đóng")
        btn_close.setFixedHeight(34)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet(
            "QPushButton { background:#1a1f35; color:#94a3b8; font-size:12px; "
            "border-radius:8px; border:1px solid #1e2540; }"
            "QPushButton:hover { background:#1e2540; }"
        )
        btn_close.clicked.connect(self.accept)
        lay.addWidget(btn_close)

    def _open_chrome_for_login(self):
        import subprocess
        import sys as _sys

        chrome_exe = self._find_chrome()
        if not chrome_exe:
            QMessageBox.warning(
                self,
                "Không tìm thấy Chrome",
                "Không tìm thấy Chrome trên máy!\n"
                "Vui lòng cài Google Chrome và thử lại.",
            )
            return

        # Hỏi user có muốn đóng Chrome đang chạy không (bắt buộc để --user-data-dir có tác dụng)
        if _sys.platform == "win32":
            check = subprocess.run(
                "tasklist /FI \"IMAGENAME eq chrome.exe\" /NH",
                capture_output=True, text=True, shell=True
            )
            chrome_running = "chrome.exe" in check.stdout
        else:
            check = subprocess.run(["pgrep", "-x", "google-chrome"], capture_output=True)
            chrome_running = check.returncode == 0

        if chrome_running:
            reply = QMessageBox.question(
                self,
                "Chrome đang chạy",
                "Chrome đang mở!\n\n"
                "⚠️  Cần đóng Chrome trước để profile riêng có tác dụng.\n\n"
                "Bấm 'Yes' để tự động đóng Chrome và mở lại với profile mới.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                if _sys.platform == "win32":
                    subprocess.run("taskkill /F /IM chrome.exe", shell=True,
                                   capture_output=True)
                else:
                    subprocess.run(["pkill", "-x", "google-chrome"], capture_output=True)
                import time; time.sleep(1.5)  # chờ Chrome đóng hẳn
            else:
                return

        os.makedirs(self.PROFILE_DIR, exist_ok=True)
        subprocess.Popen(
            [
                chrome_exe,
                f"--user-data-dir={self.PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "https://labs.google/fx/tools/flow",
            ]
        )
        QMessageBox.information(
            self,
            "Chrome đã mở",
            f"Chrome đã mở với profile:\n{self.PROFILE_DIR}\n\n"
            "Hãy:\n"
            "  1. Đăng nhập tài khoản Google (Veo3 Ultra)\n"
            "  2. Vào được labs.google/fx/tools/flow\n"
            "  3. Đóng Chrome lại\n\n"
            "Sau đó bấm '✅ Xác nhận đã đăng nhập'.",
        )

    def _verify_login(self):
        if not os.path.exists(self.PROFILE_DIR):
            QMessageBox.warning(
                self, "Chưa có profile", "Hãy bấm 'Mở Chrome để Đăng nhập' trước!"
            )
            return

        self.status_lbl.setText("🔍  Đang kiểm tra...")
        self.status_lbl.setStyleSheet(
            "font-size:12px; font-weight:600; color:#3b82f6; "
            "background:#0d0f1a; border-radius:8px; padding:10px 16px; "
            "border:1px solid #1e2540;"
        )
        self.repaint()

        # Chrome 96+: Default/Network/Cookies | Chrome cũ: Default/Cookies
        cookies_path_new = os.path.join(self.PROFILE_DIR, "Default", "Network", "Cookies")
        cookies_path_old = os.path.join(self.PROFILE_DIR, "Default", "Cookies")
        cookies_path = cookies_path_new if os.path.exists(cookies_path_new) else cookies_path_old

        if not os.path.exists(cookies_path):
            # Liệt kê xem trong profile có gì không
            profile_contents = ""
            if os.path.exists(self.PROFILE_DIR):
                try:
                    items = os.listdir(self.PROFILE_DIR)
                    profile_contents = f"\nProfile có: {', '.join(items[:8]) or '(trống)'}"
                    # Kiểm tra Default folder
                    default_dir = os.path.join(self.PROFILE_DIR, "Default")
                    if os.path.exists(default_dir):
                        default_items = os.listdir(default_dir)
                        profile_contents += f"\nDefault/: {', '.join(default_items[:8])}"
                except Exception:
                    profile_contents = "\n(Không đọc được profile)"
            self.status_lbl.setText(
                f"⚠️  Không tìm thấy Cookies:\n{cookies_path}\n"
                f"{profile_contents}\n\n"
                "→ Đóng Chrome trước → Mở lại → Đăng nhập → Đóng Chrome"
            )
            self.status_lbl.setStyleSheet(
                "font-size:11px; font-weight:600; color:#f59e0b; "
                "background:#0d0f1a; border-radius:8px; padding:10px 16px; "
                "border:1px solid #1e2540;"
            )
            return

        # Kiểm tra có cookie Google trong DB SQLite
        try:
            import sqlite3
            # Copy tạm để tránh lock (Chrome có thể đang dùng)
            import shutil, tempfile
            tmp = tempfile.mktemp(suffix=".db")
            shutil.copy2(cookies_path, tmp)
            try:
                con = sqlite3.connect(tmp)
                cur = con.execute(
                    "SELECT name FROM cookies WHERE host_key LIKE '%google%' LIMIT 5"
                )
                google_cookies = [r[0] for r in cur.fetchall()]
                con.close()
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

            # Xác nhận có cookie Google quan trọng (SAPISID, __Secure-3PSID,...)
            has_login = any(
                c in google_cookies
                for c in ["SAPISID", "APISID", "__Secure-3PSID", "SID", "HSID"]
            )

            if has_login:
                self.status_lbl.setText("✅  Đã đăng nhập Google — Sẵn sàng giải captcha tự động!")
                self.status_lbl.setStyleSheet(
                    "font-size:12px; font-weight:600; color:#22c55e; "
                    "background:#0d0f1a; border-radius:8px; padding:10px 16px; "
                    "border:1px solid #1e2540;"
                )
            else:
                self.status_lbl.setText(
                    "⚠️  Có profile nhưng chưa đăng nhập Google.\n"
                    "Hãy mở Chrome, đăng nhập rồi đóng Chrome lại."
                )
                self.status_lbl.setStyleSheet(
                    "font-size:12px; font-weight:600; color:#f59e0b; "
                    "background:#0d0f1a; border-radius:8px; padding:10px 16px; "
                    "border:1px solid #1e2540;"
                )

        except Exception as e:
            # Nếu không đọc được SQLite (Chrome đang mở) → báo user đóng Chrome
            self.status_lbl.setText(
                "⚠️ Chrome vẫn đang chạy ngầm! Vui lòng mở Task Manager (Ctrl+Shift+Esc)\n"
                "tắt mọi tiến trình 'chrome.exe', sau đó bấm Xác nhận lại.\n"
                f"(Lỗi: {str(e)[:120]})"
            )
            self.status_lbl.setStyleSheet(
                "font-size:12px; font-weight:600; color:#f59e0b; "
                "background:#0d0f1a; border-radius:8px; padding:10px 16px; "
                "border:1px solid #1e2540;"
            )

    def _find_chrome(self):
        import sys as _sys

        if _sys.platform == "win32":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expanduser(
                    r"~\AppData\Local\Google\Chrome\Application\chrome.exe"
                ),
            ]
        else:
            candidates = [
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
        return next((p for p in candidates if os.path.exists(p)), None)


class CookieDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quản lý Cookie")
        self.setFixedSize(560, 460)
        self.setStyleSheet("background:#141728; color:#e2e8f0;")

        from core.account_manager import (
            get_accounts,
            check_account_status,
            delete_account,
            save_account,
        )

        self._get_accounts = get_accounts
        self._check_status = check_account_status
        self._delete_account = delete_account
        self._save_account = save_account

        from PySide6.QtWidgets import QTabWidget

        tabs = QTabWidget()
        tabs.setStyleSheet(
            """
            QTabWidget::pane { border:none; background:#141728; }
            QTabBar::tab { background:#0d0f1a; color:#94a3b8; padding:8px 20px;
                           border:1px solid #1e2540; border-bottom:none; border-radius:6px 6px 0 0; }
            QTabBar::tab:selected { background:#141728; color:#e2e8f0; border-bottom:2px solid #6c63ff; }
        """
        )

        import_tab = QWidget()
        self._build_import_tab(import_tab)
        tabs.addTab(import_tab, "  Import  ")

        list_tab = QWidget()
        self._build_list_tab(list_tab)
        tabs.addTab(list_tab, "  Danh sách  ")
        self._list_tab = list_tab

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(tabs)

    def keyPressEvent(self, event):
        """Chặn Enter tự đóng dialog."""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            return  # Bỏ qua, không đóng
        super().keyPressEvent(event)

    def _build_import_tab(self, tab):
        lay = QVBoxLayout(tab)
        lay.setSpacing(12)
        lay.setContentsMargins(24, 18, 24, 18)

        desc = QLabel("Dán JSON cookie từ Cookie Editor extension vào đây:")
        desc.setStyleSheet("font-size:12px; color:#64748b;")
        lay.addWidget(desc)

        name_row = QHBoxLayout()
        name_lbl = QLabel("Tên tài khoản:")
        name_lbl.setStyleSheet("font-size:11px; color:#94a3b8; min-width:100px;")
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("VD: Account 1, Gmail chính...")
        self.name_input.setStyleSheet(
            "QLineEdit { background:#0d0f1a; color:#e2e8f0; border:1px solid #1e2540; "
            "border-radius:6px; padding:5px 10px; font-size:12px; }"
            "QLineEdit:focus { border-color:#6c63ff; }"
        )
        name_row.addWidget(name_lbl)
        name_row.addWidget(self.name_input, 1)
        lay.addLayout(name_row)

        self.text = QTextEdit()
        self.text.setStyleSheet(
            "QTextEdit { background:#0d0f1a; color:#e2e8f0; border:1px solid #1e2540; "
            "border-radius:8px; padding:10px; font-family:'Consolas',monospace; font-size:11px; }"
        )
        self.text.setPlaceholderText(
            '[{"name":"__Secure-next-auth.session-token","value":"eyJ..."}]'
        )
        lay.addWidget(self.text)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Huỷ")
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet(
            "QPushButton { background:#1a1f35; color:#e2e8f0; border:1px solid #1e2540; "
            "border-radius:8px; padding:7px 18px; }"
            "QPushButton:hover { background:#1e2540; }"
        )
        save = QPushButton("Lưu Cookie")
        save.clicked.connect(self.save)
        save.setStyleSheet(
            "QPushButton { background:#6c63ff; color:#fff; border:none; "
            "border-radius:8px; padding:7px 18px; font-weight:600; }"
            "QPushButton:hover { background:#5a52e0; }"
        )
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _build_list_tab(self, tab):
        lay = QVBoxLayout(tab)
        lay.setSpacing(8)
        lay.setContentsMargins(16, 14, 16, 14)

        self._account_list_layout = QVBoxLayout()
        self._account_list_layout.setSpacing(6)

        scroll_w = QWidget()
        scroll_w.setLayout(self._account_list_layout)
        from PySide6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_w)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        lay.addWidget(scroll)
        self._refresh_account_list()

    def _refresh_account_list(self):
        while self._account_list_layout.count():
            item = self._account_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        accounts = self._get_accounts()
        if not accounts:
            empty = QLabel("Chưa có cookie nào. Vào tab Import để thêm.")
            empty.setStyleSheet("color:#64748b; font-size:12px; padding:20px;")
            empty.setAlignment(Qt.AlignCenter)
            self._account_list_layout.addWidget(empty)
            return

        for acc in accounts:
            row = QFrame()
            row.setStyleSheet(
                "QFrame { background:#0d0f1a; border:1px solid #1e2540; border-radius:8px; padding:4px; }"
            )
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 6, 10, 6)

            name_lbl = QLabel(acc.get("name", "Account"))
            name_lbl.setStyleSheet("font-size:13px; font-weight:600; color:#e2e8f0;")

            st = self._check_status(acc)
            if st == "ok":
                badge_text, badge_color = "● Hợp lệ", "#22c55e"
            elif st == "expired":
                badge_text, badge_color = "● Không hợp lệ", "#ef4444"
            elif st.startswith("warning:"):
                days = st.split(":")[1]
                badge_text, badge_color = f"● Còn {days} ngày", "#f59e0b"
            else:
                badge_text, badge_color = "● Không rõ", "#94a3b8"

            badge = QLabel(badge_text)
            badge.setStyleSheet(
                f"color:{badge_color}; background:rgba({','.join(str(int(badge_color.lstrip('#')[i:i+2],16)) for i in (0,2,4))},0.15); "
                f"font-size:11px; font-weight:600; border-radius:10px; padding:3px 10px; border:1px solid {badge_color}44;"
            )

            use_btn = QPushButton("Dùng")
            use_btn.setFixedWidth(72)
            use_btn.setStyleSheet(
                "QPushButton { background:#6c63ff; color:#fff; border:none; border-radius:6px; padding:4px 8px; font-size:11px; }"
                "QPushButton:hover { background:#5a52e0; }"
            )
            acc_name = acc.get("name")
            use_btn.clicked.connect(lambda _, a=acc: self._use_account(a))

            del_btn = QPushButton("Xoá")
            del_btn.setFixedWidth(50)
            del_btn.setStyleSheet(
                "QPushButton { background:#1a1f35; color:#ef4444; border:1px solid #ef444444; border-radius:6px; padding:4px 6px; font-size:11px; }"
                "QPushButton:hover { background:#ef444422; }"
            )
            del_btn.clicked.connect(lambda _, n=acc_name: self._delete_acc(n))

            rl.addWidget(name_lbl, 1)
            rl.addWidget(badge)
            rl.addSpacing(8)
            rl.addWidget(use_btn)
            rl.addWidget(del_btn)
            self._account_list_layout.addWidget(row)

        self._account_list_layout.addStretch()

    def _use_account(self, acc):
        st = self._check_status(acc)
        if st == "expired":
            QMessageBox.warning(
                self,
                "Không hợp lệ",
                f"Cookie '{acc.get('name')}' không hợp lệ! Vui lòng import lại.",
            )
            return

        full_cookie = acc.get("full_cookie", "")
        if not full_cookie:
            return

        try:
            from core.account_manager import set_active_account

            origin = acc.get("_origin_dict")
            if origin:
                set_active_account(origin)
            else:
                # Fallback như cũ nếu không có (trường hợp import thủ công chưa apply)
                from core import browser_config as bcfg

                bcfg.set_value("full_cookie", full_cookie)
                bcfg.set_value("cookie_account_name", acc.get("name", "Account"))
                if acc.get("expiration"):
                    bcfg.set_value("cookie_expiration", acc.get("expiration"))
                bcfg.save()

        except Exception as e:
            print(f"Error set active account: {e}")

        QMessageBox.information(
            self, "OK", f"Đã chọn '{acc.get('name')}' làm tài khoản hoạt động."
        )
        self.accept()

    def _delete_acc(self, name):
        self._delete_account(name)
        self._refresh_account_list()

    def save(self):
        raw = self.text.toPlainText().strip()
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except Exception:
            QMessageBox.critical(self, "Lỗi", "JSON không hợp lệ!")
            return

        account_name = self.name_input.text().strip() or "Account"
        if isinstance(parsed, list):
            self._save_account(account_name, parsed)
            parts = []
            exp_date = None
            for c in parsed:
                if isinstance(c, dict) and c.get("name") and c.get("value"):
                    parts.append(f"{c['name']}={c['value']}")
                    if c.get("name") == "__Secure-next-auth.session-token":
                        exp_date = c.get("expirationDate")
            if parts:
                full_cookie = "; ".join(parts)
                try:
                    from core import browser_config as bcfg

                    bcfg.set_value("full_cookie", full_cookie)
                    bcfg.set_value("cookie_account_name", account_name)
                    if exp_date:
                        bcfg.set_value("cookie_expiration", exp_date)
                    bcfg.save()
                except Exception:
                    pass
        save_cookies(parsed)
        self.accept()


class AutoVoiceApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Veo3")
        self.resize(1280, 820)
        self.setMinimumSize(960, 600)

        self.active_workers = {}
        self._aspect = "portrait"
        self._session_token = None

        # Queue-based parallel dispatcher
        self._pending_queue = []  # list of row indices waiting
        self._retry_counts = {}  # row -> retry attempt count
        self._shared_project_id = None  # reusable project for all rows

        root = QWidget()
        root.setObjectName("root")
        ml = QHBoxLayout(root)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        ml.addWidget(self._build_rail())

        # Stacked widget for page switching
        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_main())  # page 0: Veo/Imagen
        self._pages.addWidget(self._build_merger())  # page 1: Video Merger
        self._pages.addWidget(self._build_imagen_page())  # page 2: Image Generation
        self._pages.setCurrentIndex(0)
        ml.addWidget(self._pages, 1)

        self.setCentralWidget(root)
        self._apply_styles()
        self._load_cookie()

    def _build_rail(self):
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(192)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(12, 20, 12, 20)
        rl.setSpacing(4)
        rl.setAlignment(Qt.AlignTop)

        # Logo + App Name
        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        logo_icon = QLabel("◆")
        logo_icon.setFixedSize(36, 36)
        logo_icon.setAlignment(Qt.AlignCenter)
        logo_icon.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #7c5cfc,stop:1 #3b82f6);"
            "color:#fff; font-size:16px; font-weight:900; border-radius:10px;"
        )
        logo_row.addWidget(logo_icon)
        logo_txt = QVBoxLayout()
        logo_txt.setSpacing(0)
        logo_name = QLabel("AutoVoice")
        logo_name.setStyleSheet("color:#f1f2f6; font-size:13px; font-weight:800; letter-spacing:0.3px;")
        logo_sub = QLabel("AI Studio")
        logo_sub.setStyleSheet("color:#6b7280; font-size:9px; font-weight:600; letter-spacing:1px;")
        logo_txt.addWidget(logo_name)
        logo_txt.addWidget(logo_sub)
        logo_row.addLayout(logo_txt)
        logo_row.addStretch()
        rl.addLayout(logo_row)
        rl.addSpacing(20)

        # Section header
        def _section_header(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "color:#4b5563; font-size:9px; font-weight:700; "
                "letter-spacing:1.5px; padding:0 4px; margin-top:6px;"
            )
            return lbl

        def _nav_btn(icon, label, idx):
            btn = QPushButton(f"  {icon}   {label}")
            btn.setFixedHeight(40)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._rail_btn_style(False))
            btn.clicked.connect(lambda checked, i=idx: self._switch_page(i))
            return btn

        rl.addWidget(_section_header("ĐIỀU KHIỂN STUDIO"))
        rl.addSpacing(4)

        self.rail_btns = []
        nav_items = [
            ("🎬", "Tạo Video Mới", 0),
            ("🖼", "Tạo Ảnh",       2),
        ]
        for icon, label, idx in nav_items:
            btn = _nav_btn(icon, label, idx)
            btn.setStyleSheet(self._rail_btn_style(idx == 0))
            rl.addWidget(btn)
            self.rail_btns.append(btn)

        rl.addSpacing(12)
        rl.addWidget(_section_header("CÔNG CỤ"))
        rl.addSpacing(4)

        merger_btn = _nav_btn("✂️", "Nối Video", 1)
        rl.addWidget(merger_btn)
        self.rail_btns.append(merger_btn)

        rl.addStretch()

        # Cookie button at the bottom
        self.rail_chrome_btn = QPushButton("⚙️  Chrome Settings")
        self.rail_chrome_btn.setFixedHeight(38)
        self.rail_chrome_btn.setCursor(Qt.PointingHandCursor)
        self.rail_chrome_btn.setToolTip("Cấu hình Chrome để tự giải Captcha")
        self.rail_chrome_btn.clicked.connect(self._open_chrome_settings)
        self.rail_chrome_btn.setStyleSheet(
            "QPushButton { background:rgba(59,130,246,0.08); color:#3b82f6; font-size:12px; "
            "font-weight:600; border-radius:10px; border:1px solid rgba(59,130,246,0.2); "
            "text-align:left; padding:0 12px; }"
            "QPushButton:hover { background:rgba(59,130,246,0.18); }"
        )
        rl.addWidget(self.rail_chrome_btn)

        self.rail_cookie_btn = QPushButton("🔑  Import Cookie")
        self.rail_cookie_btn.setFixedHeight(38)
        self.rail_cookie_btn.setCursor(Qt.PointingHandCursor)
        self.rail_cookie_btn.setToolTip("Import Cookie")
        self.rail_cookie_btn.clicked.connect(self._open_cookie)
        self.rail_cookie_btn.setStyleSheet(
            "QPushButton { background:rgba(239,68,68,0.08); color:#ef4444; font-size:12px; "
            "font-weight:600; border-radius:10px; border:1px solid rgba(239,68,68,0.2); "
            "text-align:left; padding:0 12px; }"
            "QPushButton:hover { background:rgba(239,68,68,0.18); }"
        )
        self.rail_cookie_btn.setVisible(False)  # Ẩn khỏi UI
        rl.addWidget(self.rail_cookie_btn)
        return rail


    def _switch_page(self, idx):
        """Chuyển trang. rail_btns[0]=Video(p0), rail_btns[1]=Ảnh(p2), rail_btns[2]=Merger(p1)."""
        # idx là page index trực tiếp (0=Video, 1=Merger, 2=Ảnh)
        page_map = {0: 0, 1: 1, 2: 2}
        if idx in page_map:
            self._pages.setCurrentIndex(page_map[idx])
        # rail_btns: 0->page0 (Video), 1->page2 (Ảnh), 2->page1 (Merger)
        rail_page_map = [0, 2, 1]  # rail_btn index -> page index
        for i, btn in enumerate(self.rail_btns):
            is_active = (rail_page_map[i] == idx) if i < len(rail_page_map) else False
            btn.setStyleSheet(self._rail_btn_style(is_active))

    def _rail_btn_style(self, active=False):
        base = (
            "font-size:12px; font-weight:600; text-align:left; "
            "border-radius:10px; border:none; padding:0 12px;"
        )
        if active:
            return (
                f"QPushButton {{ background:rgba(108,99,255,0.15); color:#a78bfa; {base} }}"
                "QPushButton:hover { background:rgba(108,99,255,0.22); }"
            )
        return (
            f"QPushButton {{ background:transparent; color:#9ca3af; {base} }}"
            "QPushButton:hover { background:rgba(255,255,255,0.06); color:#e8eaed; }"
        )

    def _build_main(self):
        content = QWidget()
        content.setObjectName("content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(28, 20, 28, 16)
        cl.setSpacing(14)

        cl.addLayout(self._build_header())
        cl.addLayout(self._build_stat_cards())
        cl.addWidget(self._build_prompt_area())
        cl.addLayout(self._build_toolbar())
        cl.addWidget(self._build_table(), 1)

        return content

    def _build_header(self):
        hdr = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(2)
        h1 = QLabel("AutoVoice Studio")
        h1.setStyleSheet(
            "color:#f1f2f6; font-size:22px; font-weight:800; letter-spacing:0.5px;"
        )
        h2 = QLabel("Text & Image  →  1080p AI Video")
        h2.setStyleSheet("color:#6b7280; font-size:12px;")
        left.addWidget(h1)
        left.addWidget(h2)
        hdr.addLayout(left)
        hdr.addStretch()

        self.conn_chip = QPushButton("● Chưa kết nối")
        self.conn_chip.setFixedHeight(32)
        self.conn_chip.setCursor(Qt.PointingHandCursor)
        self.conn_chip.clicked.connect(self._open_cookie)
        self.conn_chip.setStyleSheet(self._chip_style(False))
        self.conn_chip.setVisible(False)  # Ẩn khỏi UI
        hdr.addWidget(self.conn_chip)

        self.btn_start = QPushButton("▶  Bắt đầu")
        self.btn_start.setFixedSize(140, 38)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setGraphicsEffect(shadow(15, 0, 4, "#407c5cfc"))
        self.btn_start.setStyleSheet(
            "QPushButton { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7c5cfc,stop:1 #a78bfa); "
            "color:#fff; font-size:13px; font-weight:700; border-radius:10px; border:none; }"
            "QPushButton:hover { opacity:0.85; }"
        )
        self.btn_start.clicked.connect(self._start_selected)
        hdr.addWidget(self.btn_start)

        btn_stop = QPushButton("■")
        btn_stop.setFixedSize(38, 38)
        btn_stop.setCursor(Qt.PointingHandCursor)
        btn_stop.setStyleSheet(
            "QPushButton { background:rgba(239,68,68,0.12); color:#ef4444; font-size:14px; "
            "font-weight:700; border-radius:10px; border:1px solid rgba(239,68,68,0.3); }"
            "QPushButton:hover { background:#ef4444; color:#fff; }"
        )
        btn_stop.clicked.connect(self._stop_all)
        hdr.addWidget(btn_stop)
        return hdr

    def _chip_style(self, connected):
        if connected:
            return (
                "QPushButton { background:rgba(34,197,94,0.12); color:#22c55e; font-size:11px; "
                "font-weight:600; border:1px solid rgba(34,197,94,0.3); border-radius:16px; padding:0 18px; }"
                "QPushButton:hover { background:rgba(34,197,94,0.2); }"
            )
        return (
            "QPushButton { background:rgba(239,68,68,0.12); color:#ef4444; font-size:11px; "
            "font-weight:600; border:1px solid rgba(239,68,68,0.3); border-radius:16px; padding:0 18px; }"
            "QPushButton:hover { background:rgba(239,68,68,0.2); }"
        )

    def _spin(self, v, mn, mx, sfx=""):
        s = QSpinBox()
        s.setValue(v)
        s.setMinimum(mn)
        s.setMaximum(mx)
        if sfx:
            s.setSuffix(f" {sfx}")
        s.setFixedSize(70, 30)
        s.setStyleSheet(
            "QSpinBox { background:#1e2235; color:#e8eaed; font-size:13px; font-weight:700; "
            "border:1px solid #2d3140; border-radius:6px; padding:2px 8px; }"
            "QSpinBox:hover { border-color:#7c5cfc; }"
            "QSpinBox::up-button, QSpinBox::down-button { width:16px; border:none; background:#2d3140; }"
        )
        return s

    def _stat_card(self, icon, title, subtitle, widget):
        card = GlassCard()
        card.setGraphicsEffect(shadow(12, 0, 3, "#10000000"))
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card.setFixedHeight(72)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        ic = QLabel(icon)
        ic.setFixedSize(36, 36)
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet(
            "background:rgba(108,99,255,0.1); font-size:16px; border-radius:10px;"
        )
        lay.addWidget(ic)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("color:#e8eaed; font-size:12px; font-weight:700;")
        s = QLabel(subtitle)
        s.setStyleSheet("color:#6b7280; font-size:9px;")
        txt.addWidget(t)
        txt.addWidget(s)
        lay.addLayout(txt)
        lay.addStretch()
        lay.addWidget(widget)
        return card

    def _build_stat_cards(self):
        # Dùng QGridLayout 4 cột → mọi card cùng chiều rộng
        grid = QGridLayout()
        grid.setSpacing(12)
        for col in range(4):
            grid.setColumnStretch(col, 1)  # mọi cột giãn đều nhau

        # Hàng 0: Luồng, Video/Prompt, Retry, Delay
        self.luong_spin = self._spin(1, 1, 10)
        grid.addWidget(
            self._stat_card("⚡", "Luồng", "Concurrent prompts", self.luong_spin), 0, 0
        )

        self.count_spin = self._spin(1, 1, 4)
        grid.addWidget(
            self._stat_card("🎬", "Video/Prompt", "Videos per prompt", self.count_spin),
            0,
            1,
        )

        self.retry_spin = self._spin(2, 0, 5)
        grid.addWidget(
            self._stat_card("🔁", "Retry", "Auto retry on fail", self.retry_spin), 0, 2
        )

        self.delay_spin = self._spin(30, 0, 300, "s")
        grid.addWidget(
            self._stat_card("⏱", "Delay", "Between batches", self.delay_spin), 0, 3
        )

        # Hàng 1: Tỉ lệ, Model (2 ô đầu, 2 ô cuối trống)
        ratio_w = QWidget()
        ratio_w.setStyleSheet("background:transparent;")
        ratio_lay = QHBoxLayout(ratio_w)
        ratio_lay.setContentsMargins(0, 0, 0, 0)
        ratio_lay.setSpacing(4)
        self.btn_portrait = QPushButton("9:16")
        self.btn_landscape = QPushButton("16:9")
        for b in [self.btn_portrait, self.btn_landscape]:
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(52, 28)
        self.btn_portrait.clicked.connect(lambda: self._set_aspect("portrait"))
        self.btn_landscape.clicked.connect(lambda: self._set_aspect("landscape"))
        self._update_aspect_btns()
        ratio_lay.addWidget(self.btn_portrait)
        ratio_lay.addWidget(self.btn_landscape)
        grid.addWidget(self._stat_card("📐", "Tỉ lệ", "Aspect ratio", ratio_w), 1, 0)

        self.model_combo = QComboBox()
        self.model_combo.addItems(["veo3_fast (auto)", "veo_3_1_t2v_fast_ultra"])
        self.model_combo.setCursor(Qt.PointingHandCursor)
        self.model_combo.setStyleSheet(
            "QComboBox { background:#1e2235; color:#e8eaed; font-size:10px; font-weight:600; "
            "border:1px solid #2d3140; border-radius:6px; padding:4px 8px; }"
            "QComboBox:hover { border-color:#7c5cfc; }"
            "QComboBox::drop-down { border:none; width:14px; }"
            "QComboBox QAbstractItemView { background:#1a1d2a; color:#e8eaed; border:1px solid #2d3140; "
            "selection-background-color:#7c5cfc; selection-color:#fff; outline:none; font-size:11px; }"
        )
        grid.addWidget(
            self._stat_card("🤖", "Model", "Video model", self.model_combo), 1, 1
        )

        self.img_count_spin = self._spin(1, 1, 4)
        grid.addWidget(
            self._stat_card("🖼", "Số ảnh", "Images per prompt", self.img_count_spin),
            1,
            2,
        )

        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("http://ip:port")
        self.proxy_input.setStyleSheet(
            "QLineEdit { background:#1e2235; color:#e8eaed; font-size:10px; font-weight:600; "
            "border:1px solid #2d3140; border-radius:6px; padding:4px 8px; }"
            "QLineEdit:focus { border-color:#7c5cfc; }"
        )
        grid.addWidget(
            self._stat_card("🌐", "Proxy", "Optional HTTP proxy", self.proxy_input),
            1,
            3,
        )

        return grid

    def _set_aspect(self, val):
        self._aspect = val
        self._update_aspect_btns()

    def _update_aspect_btns(self):
        active = (
            "QPushButton { background:#7c5cfc; color:#fff; font-size:11px; font-weight:700; "
            "border-radius:6px; border:none; }"
        )
        inactive = (
            "QPushButton { background:#1e2235; color:#6b7280; font-size:11px; border-radius:6px; "
            "border:1px solid #2d3140; }"
            "QPushButton:hover { border-color:#7c5cfc; color:#a78bfa; }"
        )
        self.btn_portrait.setStyleSheet(
            active if self._aspect == "portrait" else inactive
        )
        self.btn_landscape.setStyleSheet(
            active if self._aspect == "landscape" else inactive
        )

    def _build_prompt_area(self):
        card = GlassCard()
        card.setGraphicsEffect(shadow(15, 0, 4, "#10000000"))
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        icon = QLabel("✏️")
        icon.setFixedSize(36, 36)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            "background:rgba(108,99,255,0.12); font-size:16px; border-radius:10px;"
        )
        lay.addWidget(icon)

        self.prompt_text = PromptEdit(flush_callback=self._flush_prompt_box)
        self.prompt_text.setPlaceholderText(
            "Nhập prompt — mỗi dòng 1 prompt, Enter để thêm vào bảng..."
        )
        self.prompt_text.setFixedHeight(52)
        self.prompt_text.setStyleSheet(
            "QTextEdit { background:transparent; color:#e8eaed; border:none; font-size:13px; padding:4px; }"
        )
        lay.addWidget(self.prompt_text, 1)

        btn_import = QPushButton("📂 Import .txt")
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_import.setFixedSize(100, 32)
        btn_import.clicked.connect(self._import_txt)
        btn_import.setStyleSheet(
            "QPushButton { background:rgba(108,99,255,0.12); color:#7c5cfc; font-size:11px; "
            "font-weight:600; border-radius:8px; border:none; }"
            "QPushButton:hover { background:rgba(108,99,255,0.25); }"
        )
        lay.addWidget(btn_import)
        return card

    def _action_pill(self, text, color):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(28)
        rgb = hex_to_rgb(color)
        b.setStyleSheet(
            f"QPushButton {{ background:rgba({rgb},0.12); color:{color}; font-weight:600; font-size:11px; "
            f"padding:0 14px; border-radius:14px; border:1px solid rgba({rgb},0.25); }}"
            f"QPushButton:hover {{ background:{color}; color:#fff; border-color:{color}; }}"
        )
        return b

    def _build_toolbar(self):
        tb = QHBoxLayout()
        tb.setSpacing(10)

        t_title = QLabel("Prompt Queue")
        t_title.setStyleSheet("color:#e8eaed; font-size:14px; font-weight:700;")
        tb.addWidget(t_title)

        self.queue_badge = QLabel("0")
        self.queue_badge.setFixedSize(28, 20)
        self.queue_badge.setAlignment(Qt.AlignCenter)
        self.queue_badge.setStyleSheet(
            "background:#7c5cfc; color:#fff; font-size:10px; font-weight:700; border-radius:10px;"
        )
        tb.addWidget(self.queue_badge)

        mode_lbl = QLabel("Chế độ:")
        mode_lbl.setStyleSheet("color:#6b7280; font-size:11px; font-weight:600;")
        tb.addWidget(mode_lbl)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Text to Video", "Image to Video"])
        self.mode_combo.setFixedSize(150, 28)
        self.mode_combo.setCursor(Qt.PointingHandCursor)
        self.mode_combo.setStyleSheet(
            "QComboBox { background:#1e2235; color:#e8eaed; font-size:11px; font-weight:600; "
            "border:1px solid #2d3140; border-radius:6px; padding:4px 10px; }"
            "QComboBox:hover { border-color:#7c5cfc; }"
            "QComboBox::drop-down { border:none; width:20px; }"
            "QComboBox QAbstractItemView { background:#1a1d2a; color:#e8eaed; border:1px solid #2d3140; "
            "selection-background-color:#7c5cfc; selection-color:#fff; outline:none; }"
        )
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        tb.addWidget(self.mode_combo)

        tb.addStretch()

        btn_del = self._action_pill("✕ Xoá", "#ef4444")
        btn_del.clicked.connect(self._delete_selected)

        self.chk_all = QCheckBox(" Chọn tất cả")
        self.chk_all.setStyleSheet(
            """
            QCheckBox { color: #e8eaed; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 12px; background: rgba(255,255,255,0.05); }
            QCheckBox:hover { background: rgba(255,255,255,0.1); }
            QCheckBox::indicator { width: 14px; height: 14px; }
        """
        )
        self.chk_all.setCursor(Qt.PointingHandCursor)
        self.chk_all.stateChanged.connect(self._select_all)

        btn_run = self._action_pill("▶ Chạy chọn", "#7c5cfc")
        btn_run.clicked.connect(self._start_selected)

        tb.addWidget(btn_del)
        tb.addWidget(self.chk_all)
        tb.addWidget(btn_run)
        return tb

    def _build_table(self):
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("queue_table")
        self.table.setHorizontalHeaderLabels(
            ["", "PROMPT", "ẢNH", "TRẠNG THÁI", "KẾT QUẢ"]
        )
        self.table.setColumnWidth(0, 36)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 260)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnHidden(2, True)
        self.table.cellClicked.connect(self._on_cell_clicked)
        return self.table

    def _on_cell_clicked(self, row, col):
        if col in (0, 1):  # Click vào checkbox hoặc prompt → toggle
            item = self.table.item(row, 0)
            if item:
                state = item.checkState()
                item.setCheckState(
                    Qt.Checked if state == Qt.Unchecked else Qt.Unchecked
                )

    def _on_mode_changed(self, mode):
        is_i2v = mode == "Image to Video"
        self.table.setColumnHidden(2, not is_i2v)
        self.table.verticalHeader().setDefaultSectionSize(80 if is_i2v else 52)

    def _apply_styles(self):
        self.setStyleSheet(
            """
            QMainWindow, #root { background:#0f1117; }
            #rail { background:#13151d; border-right:1px solid #1e2235; }
            #content { background:#0f1117; }
            #glass_card {
                background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 rgba(30,34,53,0.95), stop:1 rgba(26,29,42,0.9));
                border:1px solid #2d3140;
                border-radius:14px;
            }
            #queue_table {
                background:#161923;
                alternate-background-color:#1a1d2a;
                border:1px solid #2d3140;
                border-radius:0px;
                color:#e8eaed;
                font-size:12px;
                selection-background-color:rgba(108,99,255,0.15);
                selection-color:#e8eaed;
            }
            #queue_table::item { padding:8px 12px; border-bottom:1px solid #1e2235; }
            QHeaderView::section {
                background:#161923;
                color:#6b7280;
                font-weight:700; font-size:10px; letter-spacing:1.5px;
                border:none;
                border-bottom:2px solid #7c5cfc;
                padding:12px 14px;
            }
            QScrollBar:vertical { width:4px; background:transparent; }
            QScrollBar::handle:vertical { background:#2d3140; border-radius:2px; min-height:40px; }
            QScrollBar::handle:vertical:hover { background:#7c5cfc; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """
        )

    def _load_cookie(self):
        import time as _time
        from core import browser_config as bcfg

        cookies = load_cookies()
        tok = extract_session_token_cookie(cookies)
        self._session_token = tok
        self._account_name = bcfg.get("cookie_account_name", "Account")

        if tok:
            exp = bcfg.get("cookie_expiration")
            if exp:
                try:
                    exp_ts = float(exp)
                    now_ts = _time.time()
                    if now_ts > exp_ts:
                        days_ago = int((now_ts - exp_ts) / 86400)
                        QMessageBox.warning(
                            self,
                            "Cookie hết hạn",
                            f"Cookie '{self._account_name}' đã hết hạn {days_ago} ngày trước.\n"
                            "Vui lòng Import Cookie mới để tiếp tục.",
                        )
                        self._session_token = None
                    else:
                        days_left = int((exp_ts - now_ts) / 86400)
                        if days_left <= 3:
                            QMessageBox.warning(
                                self,
                                "Cookie sắp hết hạn",
                                f"Cookie '{self._account_name}' còn {days_left} ngày (hết: {__import__('datetime').datetime.fromtimestamp(exp_ts).strftime('%d/%m/%Y')}).",
                            )
                except Exception:
                    pass

            # Check access_token trong background (không block UI khởi động)
            try:
                from core.veo_client import VeoClient
                _client = VeoClient(cookies)
                if not _client.access_token:
                    QMessageBox.warning(
                        self, "Không lấy được token",
                        f"Không thể lấy access token từ cookie '{self._account_name}'.\n"
                        "Vui lòng Import Cookie mới.",
                    )
                    self._session_token = None
                else:
                    # Verify token trong background → không block startup
                    _tok = _client.access_token
                    _name = self._account_name
                    def _bg_verify(_token=_tok, _n=_name):
                        try:
                            import requests as _rq
                            _info = _rq.get(
                                f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={_token}",
                                timeout=5,
                            )
                            if _info.status_code != 200:
                                from PySide6.QtCore import QMetaObject, Qt as _Qt
                                QMetaObject.invokeMethod(
                                    self, "_on_token_invalid", _Qt.QueuedConnection
                                )
                        except Exception:
                            pass  # mạng yếu / offline → bỏ qua
                    import threading
                    threading.Thread(target=_bg_verify, daemon=True).start()
            except Exception:
                pass

        self._update_conn_status()

    def _update_conn_status(self):
        connected = bool(self._session_token)
        account = getattr(self, "_account_name", "Account")
        self.conn_chip.setText(f"● {account}" if connected else "● Chưa kết nối")
        self.conn_chip.setStyleSheet(self._chip_style(connected))
        self.rail_cookie_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{'#22c55e' if connected else '#ef4444'}; font-size:18px; "
            f"border-radius:10px; border:none; margin-left:8px; }}"
            f"QPushButton:hover {{ background:rgba({'34,197,94' if connected else '239,68,68'},0.15); }}"
        )

    def _open_cookie(self):
        dlg = CookieDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self._load_cookie()

    def _open_chrome_settings(self):
        """Mở dialog Chrome Settings để cấu hình profile đăng nhập."""
        dlg = ChromeSettingsDialog(self)
        dlg.exec()

    @Slot()
    def _on_token_invalid(self):
        """Gọi từ background thread khi token hết hạn."""
        self._session_token = None
        self._update_conn_status()
        QMessageBox.warning(
            self,
            "Token hết hạn",
            f"Access token của '{getattr(self, '_account_name', 'Account')}' đã hết hạn.\n"
            "Vui lòng vào labs.google, đăng nhập lại và Import Cookie mới.",
        )


    def _add_table_row(self, prompt, mode):
        row = self.table.rowCount()
        self.table.insertRow(row)

        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk.setCheckState(Qt.Unchecked)
        self.table.setItem(row, 0, chk)

        item = QTableWidgetItem(prompt)
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        item.setData(
            Qt.UserRole, {"mode": mode, "image_path": None, "end_image_path": None}
        )
        self.table.setItem(row, 1, item)

        # Always create the image pickup cell (column 2 might just be hidden in other modes)
        cell_w = QWidget()
        cell_w.setObjectName("img_picker_container")
        cell_w.setStyleSheet("#img_picker_container { background: transparent; }")
        cell_lay = QHBoxLayout(cell_w)
        cell_lay.setContentsMargins(0, 0, 0, 0)
        cell_lay.setSpacing(4)
        cell_lay.setAlignment(Qt.AlignCenter)

        btn_start = QPushButton("Bắt đầu")
        btn_start.setToolTip("Chọn Ảnh Đầu")
        btn_start.setCursor(Qt.PointingHandCursor)
        btn_start.setFixedSize(48, 48)
        btn_start.setStyleSheet(
            "QPushButton { background:transparent; color:#9ca3af; font-size:10px; font-weight:bold; "
            "border:1px solid #3f3f46; border-radius:8px; }"
            "QPushButton:hover { background:rgba(255,255,255,0.05); border-color:#52525b; color:#e4e4e7; }"
        )
        btn_start.clicked.connect(lambda _, r=row: self._select_image(r))

        lbl_arr = QLabel("⇄")
        lbl_arr.setStyleSheet("color:#52525b; font-size:14px; font-weight:bold;")
        lbl_arr.setAlignment(Qt.AlignCenter)

        btn_end = QPushButton("Kết thúc")
        btn_end.setToolTip("Chọn Ảnh Cuối (Tuỳ chọn)")
        btn_end.setCursor(Qt.PointingHandCursor)
        btn_end.setFixedSize(48, 48)
        btn_end.setStyleSheet(
            "QPushButton { background:transparent; color:#9ca3af; font-size:10px; font-weight:bold; "
            "border:1px solid #3f3f46; border-radius:8px; }"
            "QPushButton:hover { background:rgba(255,255,255,0.05); border-color:#52525b; color:#e4e4e7; }"
        )
        btn_end.clicked.connect(lambda _, r=row: self._select_end_image(r))

        cell_lay.addWidget(btn_start)
        cell_lay.addWidget(lbl_arr)
        cell_lay.addWidget(btn_end)
        self.table.setCellWidget(row, 2, cell_w)

        status = QLabel("—")
        status.setObjectName(f"status_{row}")
        status.setAlignment(Qt.AlignCenter)
        status.setStyleSheet(
            "background:#1e2235; color:#6b7280; font-size:11px; font-weight:600; "
            "border-radius:12px; padding:5px 12px;"
        )
        self.table.setCellWidget(row, 3, status)

        res_w = QWidget()
        res_w.setObjectName(f"result_{row}")
        res_lay = QHBoxLayout(res_w)
        res_lay.setContentsMargins(4, 4, 4, 4)
        res_lay.setSpacing(6)
        res_lay.setAlignment(Qt.AlignCenter)
        res_w.buttons = []

        for i in range(4):
            b = QPushButton(f"{i+1}")
            b.setFixedSize(32, 32)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton { background:#1e2235; color:#4b5563; font-weight:bold; border-radius:6px; }"
            )
            b.setEnabled(False)
            res_lay.addWidget(b)
            res_w.buttons.append(b)

        self.table.setCellWidget(row, 4, res_w)

        self.queue_badge.setText(str(self.table.rowCount()))

    def _select_image(self, row):
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Chọn ảnh đầu cho prompt {row+1}",
            "",
            "Images (*.png *.jpg *.jpeg *.webp);;All (*)",
        )
        if not path:
            return
        item = self.table.item(row, 1)
        if item:
            d = item.data(Qt.UserRole) or {}
            d["image_path"] = path
            item.setData(Qt.UserRole, d)
        cell = self.table.cellWidget(row, 2)
        if cell:
            import os

            btn = (
                cell.findChildren(QPushButton)[0]
                if cell.findChildren(QPushButton)
                else None
            )
            if btn:
                # Update button style to show image
                path_fixed = path.replace("\\", "/")
                btn.setText("")
                btn.setStyleSheet(
                    f"""
                    QPushButton {{
                        border: 1px solid #3f3f46;
                        border-radius: 8px;
                        border-image: url("{path_fixed}") 0 0 0 0 stretch stretch;
                    }}
                """
                )
                btn.setToolTip(f'<img src="{path_fixed}" width="350">')

    def _select_end_image(self, row):
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Chọn ảnh cuối cho prompt {row+1}",
            "",
            "Images (*.png *.jpg *.jpeg *.webp);;All (*)",
        )
        if not path:
            return
        item = self.table.item(row, 1)
        if item:
            d = item.data(Qt.UserRole) or {}
            d["end_image_path"] = path
            item.setData(Qt.UserRole, d)
        cell = self.table.cellWidget(row, 2)
        if cell:
            import os

            btns = cell.findChildren(QPushButton)
            if len(btns) > 1:
                path_fixed = path.replace("\\", "/")
                btns[1].setText("")
                btns[1].setStyleSheet(
                    f"""
                    QPushButton {{
                        border: 1px solid #3f3f46;
                        border-radius: 8px;
                        border-image: url("{path_fixed}") 0 0 0 0 stretch stretch;
                    }}
                """
                )
                btns[1].setToolTip(f'<img src="{path_fixed}" width="350">')

    def _import_txt(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file", "", "Text (*.txt);;All (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            mode_map = {
                "Text to Video": "t2v",
                "Image to Video": "i2v",
                "Text to Image": "t2i",
            }
            mode = mode_map.get(self.mode_combo.currentText(), "t2v")
            for line in lines:
                self._add_table_row(line, mode)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", str(e))

    def eventFilter(self, obj, event):
        """Bắt Enter trong prompt_text để thêm prompt vào bảng."""
        from PySide6.QtCore import QEvent

        if obj is self.prompt_text and event.type() == QEvent.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key_Return, Qt.Key_Enter) and not (mods & Qt.ShiftModifier):
                self._flush_prompt_box()
                return True  # consume event, không xuống dòng
        return super().eventFilter(obj, event)

    def _flush_prompt_box(self):
        """Lấy toàn bộ dòng trong prompt_text, add từng dòng vào bảng."""
        raw = self.prompt_text.toPlainText()
        mode_map = {
            "Text to Video": "t2v",
            "Image to Video": "i2v",
            "Text to Image": "t2i",
        }
        mode = mode_map.get(self.mode_combo.currentText(), "t2v")
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if lines:
            for line in lines:
                self._add_table_row(line, mode)
            self.prompt_text.clear()

    def _update_row_status(self, row, step, color):

        lbl = self.table.cellWidget(row, 3)
        if not isinstance(lbl, QLabel):
            return
        labels = {
            "AUTH": "🔐 Auth",
            "PROJECT": "📁 Project",
            "CAPTCHA": "🧩 Captcha",
            "UPLOAD": "⬆ Upload",
            "VIDEO": "🎥 Tạo video",
            "IMAGE_GEN": "🖼 Tạo ảnh",
            "UPSAMPLE": "🔍 Upscale",
            "POLL": "🔄 Chờ video",
            "DOWNLOAD": "⬇ Tải về",
            "DONE": "✔ Xong",
            "ERROR": "✕ Lỗi",
            "WAIT": "⏳ Chờ...",
            "Thieu anh": "⚠ Thiếu ảnh",
        }
        # Handle dynamic status like 'Upscale 1/4', 'DOWNLOAD 2/4'
        if step.startswith("UPSAMPLE"):
            text = (
                f"🔍 Nâng cấp {step.split(' ', 1)[1]}" if " " in step else "🔍 Nâng cấp"
            )
        elif step.startswith("DOWNLOAD") and "/" in step:
            text = f"⬇ Tải {step.split(' ', 1)[1]}" if " " in step else "⬇ Tải về"
        else:
            text = labels.get(step, step)
        bg_map = {
            "#22c55e": ("rgba(34,197,94,0.15)", "#22c55e"),
            "#3b82f6": ("rgba(59,130,246,0.15)", "#3b82f6"),
            "#6c63ff": ("rgba(108,99,255,0.15)", "#a78bfa"),
            "#f59e0b": ("rgba(245,158,11,0.15)", "#f59e0b"),
            "#ef4444": ("rgba(239,68,68,0.15)", "#ef4444"),
            "#a855f7": ("rgba(168,85,247,0.15)", "#a855f7"),
            "#22d3ee": ("rgba(34,211,238,0.15)", "#22d3ee"),
        }
        bg, fg = bg_map.get(color, ("rgba(100,116,139,0.15)", "#94a3b8"))
        lbl.setText(text)
        lbl.setStyleSheet(
            f"color:{fg}; background:{bg}; font-size:11px; font-weight:600; "
            f"border-radius:12px; padding:5px 14px; border:1px solid {fg}44;"
        )

    def _on_video_ready(self, row, idx, path_or_url):
        res_w = self.table.cellWidget(row, 4)
        if not res_w or not hasattr(res_w, "buttons"):
            return

        if idx < 0 or idx >= len(res_w.buttons):
            return

        btn = res_w.buttons[idx]
        is_local = path_or_url and os.path.exists(path_or_url)

        if is_local:
            btn.setStyleSheet(
                "QPushButton { background:#10b981; color:#ffffff; font-weight:bold; border-radius:6px; border:1px solid #059669; }"
                "QPushButton:hover { background:#059669; }"
            )
        else:
            btn.setStyleSheet(
                "QPushButton { background:#8b5cf6; color:#ffffff; font-weight:bold; border-radius:6px; border:1px solid #7c3aed; }"
                "QPushButton:hover { background:#7c3aed; }"
            )

        btn.setEnabled(True)
        try:
            btn.clicked.disconnect()
        except RuntimeError:
            pass

        if is_local:
            btn.clicked.connect(lambda _, p=path_or_url: os.startfile(p))
        else:
            # For network URLs
            btn.clicked.connect(
                lambda _, p=path_or_url: QDesktopServices.openUrl(QUrl(p))
            )

    def _on_image_ready(self, row, idx, path):
        res_w = self.table.cellWidget(row, 4)
        if not res_w or not hasattr(res_w, "buttons"):
            return

        if idx < 0 or idx >= len(res_w.buttons):
            return

        btn = res_w.buttons[idx]
        is_local = path and os.path.exists(path)

        if is_local:
            btn.setStyleSheet(
                "QPushButton { background:#10b981; color:#ffffff; font-weight:bold; border-radius:6px; border:1px solid #059669; }"
                "QPushButton:hover { background:#059669; }"
            )
        else:
            btn.setStyleSheet(
                "QPushButton { background:#8b5cf6; color:#ffffff; font-weight:bold; border-radius:6px; border:1px solid #7c3aed; }"
                "QPushButton:hover { background:#7c3aed; }"
            )

        btn.setEnabled(True)
        try:
            btn.clicked.disconnect()
        except RuntimeError:
            pass

        if is_local:
            btn.clicked.connect(lambda _, p=path: os.startfile(p))
        else:
            btn.clicked.connect(lambda _, p=path: QDesktopServices.openUrl(QUrl(p)))

    def _on_project_created(self, row, project_id):
        """Cache project_id for reuse by subsequent workers."""
        item = self.table.item(row, 1)
        if item:
            d = item.data(Qt.UserRole) or {}
            d["project_id"] = project_id
            item.setData(Qt.UserRole, d)
        if not self._shared_project_id:
            self._shared_project_id = project_id
            print(f"[App] Shared project_id set to: {project_id!r}")

    def _get_model_key(self):
        """Return model_key to pass to worker. None = use veo client default."""
        txt = self.model_combo.currentText()
        if txt == "veo_3_1_t2v_fast_ultra":
            return "veo_3_1_t2v_fast_ultra"
        return None  # auto (portrait/landscape variant)

    def _make_worker(self, row):
        if not self._session_token:
            QMessageBox.warning(self, "Loi", "Vui long Import Cookie truoc!")
            return None
        item = self.table.item(row, 1)
        if not item:
            return None
        prompt = item.text().strip().rstrip("\\")
        if not prompt:
            return None

        settings_data = item.data(Qt.UserRole) or {}
        # Lấy cố định mode lúc thêm bảng, thay vì đọc lại Combobox dễ bị người dùng đổi sang Text vô tình
        mode = settings_data.get("mode", "t2v")

        settings = {
            "aspect": self._aspect,
            "count": self.count_spin.value(),
            "delay": self.delay_spin.value(),
            "img_count": self.img_count_spin.value(),
            "proxy": self.proxy_input.text().strip(),
        }

        if mode == "t2i":
            w = ImagenWorker(
                row,
                prompt,
                settings,
                self._session_token,
                os.path.join(ROOT_DIR, "outputs"),
                start_delay=0,
                project_id=self._shared_project_id,
            )
            w.status_changed.connect(self._update_row_status)
            w.image_ready.connect(self._on_image_ready)
            w.project_created.connect(self._on_project_created)
            w.finished.connect(lambda r=row: self._on_worker_finished(r))
            return w
        else:
            img_path = settings_data.get("image_path") if mode == "i2v" else None
            end_img_path = (
                settings_data.get("end_image_path") if mode == "i2v" else None
            )
            if mode == "i2v" and not img_path:
                self._update_row_status(row, "Thieu anh", "#f59e0b")
                return None
            stt_idx = getattr(self, "_row_stt_map", {}).get(row, row + 1)
            w = VeoWorker(
                row,
                prompt,
                settings,
                self._session_token,
                os.path.join(ROOT_DIR, "outputs"),
                start_delay=0,
                mode="image" if mode == "i2v" else "text",
                image_path=img_path,
                end_image_path=end_img_path,
                project_id=self._shared_project_id,  # reuse project
                model_key=self._get_model_key(),
                stt_index=stt_idx,
            )
            w.status_changed.connect(self._update_row_status)
            w.video_ready.connect(self._on_video_ready)
            w.project_created.connect(self._on_project_created)
            w.retry_needed.connect(self._on_retry_needed)
            w.finished.connect(lambda r=row: self._on_worker_finished(r))
            return w

    def _on_worker_finished(self, row):
        """Called when a worker finishes (success or fail). Dispatch next pending row."""
        self.active_workers.pop(row, None)
        self._dispatch_next()

    def _on_retry_needed(self, row):
        """Worker signaled failure. Check if we should auto-retry."""
        max_retries = self.retry_spin.value()
        attempt = self._retry_counts.get(row, 0)
        if attempt < max_retries:
            self._retry_counts[row] = attempt + 1
            label = f"🔁 Retry {attempt + 1}/{max_retries}"
            lbl = self.table.cellWidget(row, 3)
            if isinstance(lbl, QLabel):
                lbl.setText(label)
                lbl.setStyleSheet(
                    "color:#f59e0b; background:rgba(245,158,11,0.15); font-size:11px; "
                    "font-weight:600; border-radius:12px; padding:5px 14px; border:1px solid #f59e0b44;"
                )
            # schedule retry after delay_spin seconds
            import random

            delay_ms = max(
                3000, self.delay_spin.value() * 1000 + random.randint(0, 5000)
            )
            print(
                f"[AutoRetry] row={row} attempt={attempt+1}/{max_retries} delay={delay_ms}ms"
            )
            QTimer.singleShot(delay_ms, lambda r=row: self._retry_row(r))
        else:
            print(f"[AutoRetry] row={row} max retries reached, giving up")

    def _retry_row(self, row):
        """Restart worker for a row (retry)."""
        if row in self.active_workers:
            return  # already running somehow
        w = self._make_worker(row)
        if w:
            self.active_workers[row] = w
            w.start()

    def _dispatch_next(self):
        """Start next pending row if we have capacity."""
        max_concurrent = self.luong_spin.value()
        while self._pending_queue and len(self.active_workers) < max_concurrent:
            next_row = self._pending_queue.pop(0)
            if next_row in self.active_workers:
                continue
            w = self._make_worker(next_row)
            if w:
                self.active_workers[next_row] = w
                w.start()

    def _start_row(self, row):
        if row in self.active_workers:
            return
        max_concurrent = self.luong_spin.value()
        if len(self.active_workers) < max_concurrent:
            w = self._make_worker(row)
            if w:
                self.active_workers[row] = w
                w.start()
        else:
            if row not in self._pending_queue:
                self._pending_queue.append(row)
                self._update_row_status(row, "WAIT", "#f59e0b")

    def _start_all(self):
        if not self._session_token:
            QMessageBox.warning(self, "Loi", "Vui long Import Cookie truoc!")
            return
        self._pending_queue.clear()
        self._retry_counts.clear()
        self._shared_project_id = None  # reset shared project for new run
        self._row_stt_map = {}
        for i, r in enumerate(range(self.table.rowCount())):
            self._row_stt_map[r] = i + 1
            self._start_row(r)

    def _start_selected(self):
        if not self._session_token:
            QMessageBox.warning(self, "Loi", "Vui long Import Cookie truoc!")
            return
        rows = [
            r
            for r in range(self.table.rowCount())
            if self.table.item(r, 0)
            and self.table.item(r, 0).checkState() == Qt.Checked
        ]
        if not rows:
            # Không tick gì → chạy tất cả
            self._start_all()
            return
        self._pending_queue.clear()
        self._retry_counts.clear()
        self._row_stt_map = {}
        for i, r in enumerate(rows):
            self._row_stt_map[r] = i + 1
            self._start_row(r)

    def _stop_all(self):
        # Dừng queue chờ
        self._pending_queue.clear()
        for row, w in list(self.active_workers.items()):
            try:
                if hasattr(w, "stop"):
                    w.stop()  # Set flag _stopped
                else:
                    w.requestInterruption()
                w.quit()
                if not w.wait(3000):  # chờ 3s
                    w.terminate()
                    w.wait(1000)
            except Exception:
                pass
            self._update_row_status(row, "STOPPED", "#f59e0b")
        self.active_workers.clear()
        print("[App] All workers stopped")

    def _delete_selected(self):
        rows = [
            r
            for r in range(self.table.rowCount())
            if self.table.item(r, 0)
            and self.table.item(r, 0).checkState() == Qt.Checked
        ]
        if not rows:
            self.table.setRowCount(0)
        else:
            for r in sorted(rows, reverse=True):
                self.table.removeRow(r)
        self.queue_badge.setText(str(self.table.rowCount()))

    def _select_all(self, state):
        # PySide6 stateChanged passes an integer. Qt.Checked is equivalent to 2.
        st = Qt.Checked if state == 2 else Qt.Unchecked
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(st)

    def eventFilter(self, obj, event):
        if obj is self.prompt_text and event.type() == QEvent.KeyPress:
            # Shift+Enter = submit prompts vào bảng
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (
                event.modifiers() & Qt.ShiftModifier
            ):
                text = self.prompt_text.toPlainText().strip()
                if text:
                    mode_map = {
                        "Text to Video": "t2v",
                        "Image to Video": "i2v",
                        "Text to Image": "t2i",
                    }
                    mode = mode_map.get(self.mode_combo.currentText(), "t2v")
                    for line in text.split("\n"):
                        if line.strip():
                            self._add_table_row(line.strip(), mode)
                    self.prompt_text.clear()
                return True
            # Enter = xuống dòng (mặc định, không cần xử lý)
        return super().eventFilter(obj, event)

    # ═══════════════════════════════════════════════════════════
    # PAGE 2: Tạo Ảnh (Image Generation)
    # ═══════════════════════════════════════════════════════════

    def _build_imagen_page(self):
        """Trang Tạo Ảnh riêng biệt với queue và gallery thumbnail."""
        page = QWidget()
        page.setObjectName("imagen_page")
        pl = QVBoxLayout(page)
        pl.setContentsMargins(28, 20, 28, 16)
        pl.setSpacing(14)

        # — Header —
        hdr = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(2)
        h1 = QLabel("🖼  Tạo Ảnh AI")
        h1.setStyleSheet("color:#f1f2f6; font-size:22px; font-weight:800; letter-spacing:0.5px;")
        h2 = QLabel("NARWHAL • GEM_PIX • Multi-Reference Image Generation")
        h2.setStyleSheet("color:#6b7280; font-size:12px;")
        left.addWidget(h1)
        left.addWidget(h2)
        hdr.addLayout(left)
        hdr.addStretch()

        self.img_btn_start = QPushButton("▶  Bắt đầu")
        self.img_btn_start.setFixedSize(140, 38)
        self.img_btn_start.setCursor(Qt.PointingHandCursor)
        self.img_btn_start.setStyleSheet(
            "QPushButton { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #ec4899,stop:1 #a855f7); "
            "color:#fff; font-size:13px; font-weight:700; border-radius:10px; border:none; }"
            "QPushButton:hover { opacity:0.85; }"
        )
        self.img_btn_start.clicked.connect(self._img_start_selected)
        hdr.addWidget(self.img_btn_start)

        btn_stop_img = QPushButton("■")
        btn_stop_img.setFixedSize(38, 38)
        btn_stop_img.setCursor(Qt.PointingHandCursor)
        btn_stop_img.setStyleSheet(
            "QPushButton { background:rgba(239,68,68,0.12); color:#ef4444; font-size:14px; "
            "font-weight:700; border-radius:10px; border:1px solid rgba(239,68,68,0.3); }"
            "QPushButton:hover { background:#ef4444; color:#fff; }"
        )
        btn_stop_img.clicked.connect(self._img_stop_all)
        hdr.addWidget(btn_stop_img)
        pl.addLayout(hdr)

        # — Settings Row —
        settings_grid = QGridLayout()
        settings_grid.setSpacing(10)
        for _col in range(3):
            settings_grid.setColumnStretch(_col, 1)  # 3 cột giãn đều nhau

        # Model
        def _sc(icon, lbl, sub, widget):
            card = GlassCard()
            card.setFixedHeight(68)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            lay = QHBoxLayout(card)
            lay.setContentsMargins(12, 8, 12, 8)
            lay.setSpacing(10)
            ic = QLabel(icon)
            ic.setFixedSize(34, 34)
            ic.setAlignment(Qt.AlignCenter)
            ic.setStyleSheet("background:rgba(236,72,153,0.1); font-size:15px; border-radius:9px;")
            lay.addWidget(ic)
            txt = QVBoxLayout()
            txt.setSpacing(1)
            t = QLabel(lbl)
            t.setStyleSheet("color:#e8eaed; font-size:11px; font-weight:700;")
            s = QLabel(sub)
            s.setStyleSheet("color:#6b7280; font-size:9px;")
            txt.addWidget(t)
            txt.addWidget(s)
            lay.addLayout(txt)
            lay.addStretch()
            lay.addWidget(widget)
            return card

        self.img_model_combo = QComboBox()
        self.img_model_combo.addItems(["NARWHAL", "GEM_PIX_2"])
        self.img_model_combo.setFixedSize(110, 28)
        self.img_model_combo.setStyleSheet(
            "QComboBox { background:#1e2235; color:#e8eaed; font-size:11px; font-weight:600; "
            "border:1px solid #2d3140; border-radius:6px; padding:4px 8px; }"
            "QComboBox::drop-down { border:none; width:14px; }"
            "QComboBox QAbstractItemView { background:#1a1d2a; color:#e8eaed; "
            "selection-background-color:#ec4899; outline:none; }"
        )
        settings_grid.addWidget(_sc("🤖", "Model", "Imagen model", self.img_model_combo), 0, 0)

        # Aspect
        ratio_w = QWidget()
        ratio_w.setStyleSheet("background:transparent;")
        ratio_lay = QHBoxLayout(ratio_w)
        ratio_lay.setContentsMargins(0, 0, 0, 0)
        ratio_lay.setSpacing(4)
        self.img_btn_portrait = QPushButton("9:16")
        self.img_btn_landscape = QPushButton("16:9")
        self._img_aspect = "portrait"
        active_s = ("QPushButton { background:#ec4899; color:#fff; font-size:11px; "
                    "font-weight:700; border-radius:6px; border:none; }")
        inactive_s = ("QPushButton { background:#1e2235; color:#6b7280; font-size:11px; "
                      "border-radius:6px; border:1px solid #2d3140; }"
                      "QPushButton:hover { border-color:#ec4899; color:#f9a8d4; }")
        for b, val in [(self.img_btn_portrait, "portrait"), (self.img_btn_landscape, "landscape")]:
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(50, 26)
            b.clicked.connect(lambda _, v=val: self._img_set_aspect(v))
        self.img_btn_portrait.setStyleSheet(active_s)
        self.img_btn_landscape.setStyleSheet(inactive_s)
        ratio_lay.addWidget(self.img_btn_portrait)
        ratio_lay.addWidget(self.img_btn_landscape)
        settings_grid.addWidget(_sc("📐", "Tỉ lệ", "Aspect ratio", ratio_w), 0, 1)

        # Count
        self.img_count_img_spin = QSpinBox()
        self.img_count_img_spin.setRange(1, 4)
        self.img_count_img_spin.setValue(1)
        self.img_count_img_spin.setFixedSize(60, 28)
        self.img_count_img_spin.setStyleSheet(
            "QSpinBox { background:#1e2235; color:#e8eaed; font-size:13px; font-weight:700; "
            "border:1px solid #2d3140; border-radius:6px; padding:2px 6px; }"
        )
        settings_grid.addWidget(_sc("🖼", "Số ảnh", "Per prompt", self.img_count_img_spin), 0, 2)

        # Resolution
        self.img_res_combo = QComboBox()
        self.img_res_combo.addItems(["4K", "2K", "1080p"])
        self.img_res_combo.setFixedSize(80, 28)
        self.img_res_combo.setStyleSheet(
            "QComboBox { background:#1e2235; color:#e8eaed; font-size:11px; font-weight:600; "
            "border:1px solid #2d3140; border-radius:6px; padding:4px 8px; }"
            "QComboBox::drop-down { border:none; width:14px; }"
            "QComboBox QAbstractItemView { background:#1a1d2a; color:#e8eaed; "
            "selection-background-color:#ec4899; outline:none; }"
        )
        settings_grid.addWidget(_sc("✨", "Độ phân giải", "Upscale output", self.img_res_combo), 1, 0)

        # Proxy
        self.img_proxy_input = QLineEdit()
        self.img_proxy_input.setPlaceholderText("http://ip:port (optional)")
        self.img_proxy_input.setStyleSheet(
            "QLineEdit { background:#1e2235; color:#e8eaed; font-size:10px; "
            "border:1px solid #2d3140; border-radius:6px; padding:4px 8px; }"
            "QLineEdit:focus { border-color:#ec4899; }"
        )
        settings_grid.addWidget(_sc("🌐", "Proxy", "Optional", self.img_proxy_input), 1, 1)
        pl.addLayout(settings_grid)

        # — Reference Images Card —
        ref_card = GlassCard()
        ref_card.setFixedHeight(80)
        ref_lay = QHBoxLayout(ref_card)
        ref_lay.setContentsMargins(16, 10, 16, 10)
        ref_lay.setSpacing(12)

        ref_icon = QLabel("🔗")
        ref_icon.setFixedSize(36, 36)
        ref_icon.setAlignment(Qt.AlignCenter)
        ref_icon.setStyleSheet("background:rgba(236,72,153,0.1); font-size:16px; border-radius:10px;")
        ref_lay.addWidget(ref_icon)

        ref_txt = QVBoxLayout()
        ref_txt.setSpacing(2)
        ref_lbl = QLabel("Ảnh tham chiếu (Reference Images) — tuỳ chọn")
        ref_lbl.setStyleSheet("color:#e8eaed; font-size:12px; font-weight:700;")
        ref_sub = QLabel("Tải lên 1–3 ảnh tham chiếu để guide style và nhân vật trong ảnh được tạo ra.")
        ref_sub.setStyleSheet("color:#6b7280; font-size:10px;")
        ref_txt.addWidget(ref_lbl)
        ref_txt.addWidget(ref_sub)
        ref_lay.addLayout(ref_txt, 1)

        self.img_ref_paths = []  # list of str paths
        self.img_ref_btn_bar = QHBoxLayout()
        self.img_ref_btn_bar.setSpacing(6)

        btn_add_ref = QPushButton("+ Thêm ảnh ref")
        btn_add_ref.setCursor(Qt.PointingHandCursor)
        btn_add_ref.setFixedHeight(30)
        btn_add_ref.setStyleSheet(
            "QPushButton { background:rgba(236,72,153,0.12); color:#f472b6; font-size:11px; "
            "font-weight:600; border-radius:8px; border:1px solid rgba(236,72,153,0.3); padding:0 12px; }"
            "QPushButton:hover { background:#ec4899; color:#fff; }"
        )
        btn_add_ref.clicked.connect(self._img_add_ref)
        self.img_ref_btn_bar.addWidget(btn_add_ref)

        self.img_ref_clear_btn = QPushButton("🗑 Xoá hết")
        self.img_ref_clear_btn.setCursor(Qt.PointingHandCursor)
        self.img_ref_clear_btn.setFixedHeight(30)
        self.img_ref_clear_btn.setStyleSheet(
            "QPushButton { background:rgba(239,68,68,0.1); color:#ef4444; font-size:11px; "
            "border-radius:8px; border:1px solid rgba(239,68,68,0.25); padding:0 10px; }"
            "QPushButton:hover { background:#ef4444; color:#fff; }"
        )
        self.img_ref_clear_btn.clicked.connect(self._img_clear_refs)
        self.img_ref_btn_bar.addWidget(self.img_ref_clear_btn)

        self.img_ref_count_lbl = QLabel("Chưa có ảnh ref")
        self.img_ref_count_lbl.setStyleSheet("color:#6b7280; font-size:11px;")
        self.img_ref_btn_bar.addWidget(self.img_ref_count_lbl)
        self.img_ref_btn_bar.addStretch()

        ref_lay.addLayout(self.img_ref_btn_bar)
        pl.addWidget(ref_card)

        # — Prompt Input —
        prompt_card = GlassCard()
        prompt_lay = QHBoxLayout(prompt_card)
        prompt_lay.setContentsMargins(16, 10, 16, 10)
        prompt_lay.setSpacing(12)

        p_icon = QLabel("✏️")
        p_icon.setFixedSize(34, 34)
        p_icon.setAlignment(Qt.AlignCenter)
        p_icon.setStyleSheet("background:rgba(236,72,153,0.1); font-size:15px; border-radius:9px;")
        prompt_lay.addWidget(p_icon)

        self.img_prompt_text = PromptEdit(flush_callback=self._img_flush_prompt)
        self.img_prompt_text.setPlaceholderText("Nhập prompt tạo ảnh — Enter để thêm hàng chờ...")
        self.img_prompt_text.setFixedHeight(48)
        self.img_prompt_text.setStyleSheet(
            "QTextEdit { background:transparent; color:#e8eaed; border:none; font-size:13px; padding:4px; }"
        )
        prompt_lay.addWidget(self.img_prompt_text, 1)

        btn_txt_import = QPushButton("📂 Import .txt")
        btn_txt_import.setCursor(Qt.PointingHandCursor)
        btn_txt_import.setFixedSize(100, 30)
        btn_txt_import.clicked.connect(self._img_import_txt)
        btn_txt_import.setStyleSheet(
            "QPushButton { background:rgba(236,72,153,0.12); color:#ec4899; font-size:11px; "
            "font-weight:600; border-radius:8px; border:none; }"
            "QPushButton:hover { background:rgba(236,72,153,0.25); }"
        )
        prompt_lay.addWidget(btn_txt_import)
        pl.addWidget(prompt_card)

        # — Queue Toolbar —
        qtb = QHBoxLayout()
        qtb.setSpacing(10)
        qlbl = QLabel("Image Queue")
        qlbl.setStyleSheet("color:#e8eaed; font-size:14px; font-weight:700;")
        qtb.addWidget(qlbl)
        self.img_queue_badge = QLabel("0")
        self.img_queue_badge.setFixedSize(28, 20)
        self.img_queue_badge.setAlignment(Qt.AlignCenter)
        self.img_queue_badge.setStyleSheet(
            "background:#ec4899; color:#fff; font-size:10px; font-weight:700; border-radius:10px;"
        )
        qtb.addWidget(self.img_queue_badge)
        qtb.addStretch()

        self.img_chk_all = QCheckBox(" Chọn tất cả")
        self.img_chk_all.setStyleSheet(
            "QCheckBox { color: #e8eaed; font-size: 11px; font-weight: 600; padding: 4px 10px; "
            "border-radius: 12px; background: rgba(255,255,255,0.05); }"
            "QCheckBox:hover { background: rgba(255,255,255,0.1); }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        self.img_chk_all.stateChanged.connect(self._img_select_all)
        qtb.addWidget(self.img_chk_all)

        btn_img_del = QPushButton("✕ Xoá")
        btn_img_del.setCursor(Qt.PointingHandCursor)
        btn_img_del.setFixedHeight(28)
        btn_img_del.setStyleSheet(
            "QPushButton { background:rgba(239,68,68,0.12); color:#ef4444; font-weight:600; "
            "font-size:11px; padding:0 14px; border-radius:14px; border:1px solid rgba(239,68,68,0.25); }"
            "QPushButton:hover { background:#ef4444; color:#fff; }"
        )
        btn_img_del.clicked.connect(self._img_delete_selected)
        qtb.addWidget(btn_img_del)
        pl.addLayout(qtb)

        # — Prompt Table —
        self.img_table = QTableWidget(0, 4)
        self.img_table.setObjectName("queue_table")
        self.img_table.setHorizontalHeaderLabels(["", "PROMPT", "TRẠNG THÁI", "KẾT QUẢ (ẢNH)"])
        self.img_table.setColumnWidth(0, 36)
        self.img_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.img_table.setColumnWidth(2, 150)
        self.img_table.setColumnWidth(3, 280)
        self.img_table.verticalHeader().setDefaultSectionSize(56)
        self.img_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.img_table.setShowGrid(False)
        self.img_table.verticalHeader().setVisible(False)
        self.img_table.setAlternatingRowColors(True)
        pl.addWidget(self.img_table, 1)

        # State
        self._img_active_workers = {}
        self._img_pending_queue = []

        return page

    # — Imagen Page helpers —

    def _img_set_aspect(self, val):
        self._img_aspect = val
        active_s = ("QPushButton { background:#ec4899; color:#fff; font-size:11px; "
                    "font-weight:700; border-radius:6px; border:none; }")
        inactive_s = ("QPushButton { background:#1e2235; color:#6b7280; font-size:11px; "
                      "border-radius:6px; border:1px solid #2d3140; }"
                      "QPushButton:hover { border-color:#ec4899; color:#f9a8d4; }")
        self.img_btn_portrait.setStyleSheet(active_s if val == "portrait" else inactive_s)
        self.img_btn_landscape.setStyleSheet(active_s if val == "landscape" else inactive_s)

    def _img_add_ref(self):
        if len(self.img_ref_paths) >= 3:
            QMessageBox.information(self, "Giới hạn", "Tối đa 3 ảnh reference.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Chọn ảnh tham chiếu", "",
            "Images (*.png *.jpg *.jpeg *.webp);;All (*)"
        )
        for p in paths:
            if p and p not in self.img_ref_paths and len(self.img_ref_paths) < 3:
                self.img_ref_paths.append(p)
        count = len(self.img_ref_paths)
        self.img_ref_count_lbl.setText(
            f"✓ {count} ảnh ref đã chọn" if count else "Chưa có ảnh ref"
        )

    def _img_clear_refs(self):
        self.img_ref_paths.clear()
        self.img_ref_count_lbl.setText("Chưa có ảnh ref")

    def _img_flush_prompt(self):
        raw = self.img_prompt_text.toPlainText()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        for line in lines:
            self._img_add_row(line)
        self.img_prompt_text.clear()

    def _img_import_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file", "", "Text (*.txt);;All (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            for line in lines:
                self._img_add_row(line)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", str(e))

    def _img_add_row(self, prompt: str):
        row = self.img_table.rowCount()
        self.img_table.insertRow(row)

        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk.setCheckState(Qt.Unchecked)
        self.img_table.setItem(row, 0, chk)

        item = QTableWidgetItem(prompt)
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.img_table.setItem(row, 1, item)

        status_lbl = QLabel("—")
        status_lbl.setAlignment(Qt.AlignCenter)
        status_lbl.setStyleSheet(
            "background:#1e2235; color:#6b7280; font-size:11px; font-weight:600; "
            "border-radius:12px; padding:5px 12px;"
        )
        self.img_table.setCellWidget(row, 2, status_lbl)

        # Kết quả: 4 thumbnail slots
        res_w = QWidget()
        res_lay = QHBoxLayout(res_w)
        res_lay.setContentsMargins(4, 4, 4, 4)
        res_lay.setSpacing(4)
        res_lay.setAlignment(Qt.AlignCenter)
        res_w.buttons = []
        for i in range(4):
            b = QPushButton(f"{i+1}")
            b.setFixedSize(44, 44)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton { background:#1e2235; color:#4b5563; font-weight:bold; border-radius:8px; }"
            )
            b.setEnabled(False)
            res_lay.addWidget(b)
            res_w.buttons.append(b)
        self.img_table.setCellWidget(row, 3, res_w)

        self.img_queue_badge.setText(str(self.img_table.rowCount()))

    def _img_update_status(self, row, step, color):
        lbl = self.img_table.cellWidget(row, 2)
        if not isinstance(lbl, QLabel):
            return
        labels = {
            "AUTH": "🔐 Auth", "PROJECT": "📁 Project",
            "CAPTCHA": "🧩 Captcha", "IMAGE_GEN": "🖼 Tạo ảnh",
            "DONE": "✔ Xong", "ERROR": "✕ Lỗi",
            "WAIT": "⏳ Chờ...", "STOPPED": "⏹ Dừng",
        }
        if step.startswith("Upscale"):
            text = f"✨ {step}"
        elif step.startswith("DOWNLOAD"):
            text = f"⬇ Tải {step.split(' ', 1)[-1]}"
        else:
            text = labels.get(step, step)
        bg_map = {
            "#22c55e": ("rgba(34,197,94,0.15)", "#22c55e"),
            "#3b82f6": ("rgba(59,130,246,0.15)", "#3b82f6"),
            "#6c63ff": ("rgba(108,99,255,0.15)", "#a78bfa"),
            "#f59e0b": ("rgba(245,158,11,0.15)", "#f59e0b"),
            "#ef4444": ("rgba(239,68,68,0.15)", "#ef4444"),
            "#a855f7": ("rgba(168,85,247,0.15)", "#a855f7"),
            "#22d3ee": ("rgba(34,211,238,0.15)", "#22d3ee"),
            "#ec4899": ("rgba(236,72,153,0.15)", "#ec4899"),
        }
        bg, fg = bg_map.get(color, ("rgba(100,116,139,0.15)", "#94a3b8"))
        lbl.setText(text)
        lbl.setStyleSheet(
            f"color:{fg}; background:{bg}; font-size:11px; font-weight:600; "
            f"border-radius:12px; padding:5px 14px; border:1px solid {fg}44;"
        )

    def _img_on_image_ready(self, row, idx, path):
        res_w = self.img_table.cellWidget(row, 3)
        if not res_w or not hasattr(res_w, "buttons"):
            return
        if idx < 0 or idx >= len(res_w.buttons):
            return
        btn = res_w.buttons[idx]
        is_local = path and os.path.exists(path)
        if is_local:
            # Show thumbnail via border-image
            path_fixed = path.replace("\\", "/")
            btn.setText("")
            btn.setStyleSheet(
                f"QPushButton {{ border:2px solid #22c55e; border-radius:8px; "
                f"border-image: url(\"{path_fixed}\") 0 0 0 0 stretch stretch; }}"
                f"QPushButton:hover {{ border-color:#4ade80; }}"
            )
            btn.setToolTip(f'<img src="{path_fixed}" width="300">')
            btn.clicked.connect(lambda _, p=path: os.startfile(p))
        else:
            btn.setStyleSheet(
                "QPushButton { background:#8b5cf6; color:#fff; font-weight:bold; border-radius:8px; }"
            )
        btn.setEnabled(True)
        try:
            btn.clicked.disconnect()
        except RuntimeError:
            pass
        if is_local:
            btn.clicked.connect(lambda _, p=path: os.startfile(p))

    def _img_on_worker_finished(self, row):
        self._img_active_workers.pop(row, None)
        self._img_dispatch_next()

    def _img_dispatch_next(self):
        while self._img_pending_queue and len(self._img_active_workers) < 3:
            next_row = self._img_pending_queue.pop(0)
            if next_row not in self._img_active_workers:
                w = self._img_make_worker(next_row)
                if w:
                    self._img_active_workers[next_row] = w
                    w.start()

    def _img_make_worker(self, row):
        if not self._session_token:
            QMessageBox.warning(self, "Lỗi", "Vui lòng Import Cookie trước!")
            return None
        item = self.img_table.item(row, 1)
        if not item:
            return None
        prompt = item.text().strip()
        if not prompt:
            return None

        aspect_map = {
            "portrait": "IMAGE_ASPECT_RATIO_PORTRAIT",
            "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        }

        settings = {
            "aspect": aspect_map.get(self._img_aspect, "IMAGE_ASPECT_RATIO_PORTRAIT"),
            "img_count": self.img_count_img_spin.value(),
            "resolution": self.img_res_combo.currentText(),
            "proxy": self.img_proxy_input.text().strip(),
            "image_refs": list(self.img_ref_paths),
            "model": self.img_model_combo.currentText(),
        }

        from workers.imagen_worker import ImagenWorker

        w = ImagenWorker(
            row, prompt, settings,
            self._session_token, os.path.join(ROOT_DIR, "outputs"),
            start_delay=0,
            project_id=self._shared_project_id,
        )
        w.status_changed.connect(self._img_update_status)
        w.image_ready.connect(self._img_on_image_ready)
        w.project_created.connect(self._on_project_created)
        w.finished.connect(lambda r=row: self._img_on_worker_finished(r))
        return w

    def _img_start_row(self, row):
        if row in self._img_active_workers:
            return
        if len(self._img_active_workers) < 3:
            w = self._img_make_worker(row)
            if w:
                self._img_active_workers[row] = w
                w.start()
        else:
            if row not in self._img_pending_queue:
                self._img_pending_queue.append(row)
                self._img_update_status(row, "WAIT", "#f59e0b")

    def _img_start_selected(self):
        if not self._session_token:
            QMessageBox.warning(self, "Lỗi", "Vui lòng Import Cookie trước!")
            return
        rows = [
            r for r in range(self.img_table.rowCount())
            if self.img_table.item(r, 0) and self.img_table.item(r, 0).checkState() == Qt.Checked
        ]
        if not rows:
            rows = list(range(self.img_table.rowCount()))
        self._img_pending_queue.clear()
        for r in rows:
            self._img_start_row(r)

    def _img_stop_all(self):
        self._img_pending_queue.clear()
        for row, w in list(self._img_active_workers.items()):
            try:
                if hasattr(w, "stop"):
                    w.stop()
                w.quit()
                if not w.wait(2000):
                    w.terminate()
            except Exception:
                pass
            self._img_update_status(row, "STOPPED", "#6b7280")
        self._img_active_workers.clear()

    def _img_select_all(self, state):
        st = Qt.Checked if state == 2 else Qt.Unchecked
        for r in range(self.img_table.rowCount()):
            item = self.img_table.item(r, 0)
            if item:
                item.setCheckState(st)

    def _img_delete_selected(self):
        rows = [
            r for r in range(self.img_table.rowCount())
            if self.img_table.item(r, 0) and self.img_table.item(r, 0).checkState() == Qt.Checked
        ]
        if not rows:
            self.img_table.setRowCount(0)
        else:
            for r in sorted(rows, reverse=True):
                self.img_table.removeRow(r)
        self.img_queue_badge.setText(str(self.img_table.rowCount()))

    # ═══════════════════════════════════════════════════════════════════
    # (Tiếp) PAGE 1: Video Merger
    # ═══════════════════════════════════════════════════════════════════

    def _build_merger(self):
        page = QWidget()
        page.setObjectName("merger_page")
        ml = QVBoxLayout(page)
        ml.setContentsMargins(28, 20, 28, 16)
        ml.setSpacing(14)

        # Header
        h1 = QLabel("Video Merger / Nối Video")
        h1.setStyleSheet(
            "color:#f1f2f6; font-size:22px; font-weight:800; letter-spacing:0.5px;"
        )
        desc = QLabel(
            "Dùng FFmpeg để nối các đoạn Video được đánh số tự động thành 1 video dài."
        )
        desc.setStyleSheet("color:#6b7280; font-size:12px;")
        ml.addWidget(h1)
        ml.addWidget(desc)
        ml.addSpacing(10)

        # Toolbar
        tb = QHBoxLayout()
        btn_add = QPushButton("+ Thêm Video")
        btn_add.setFixedSize(130, 36)
        btn_add.setCursor(Qt.PointingHandCursor)
        self._decorate_add_btn(btn_add)
        btn_add.clicked.connect(self._add_merge_files)
        tb.addWidget(btn_add)

        btn_clear = QPushButton("🗑 Xoá danh sách")
        btn_clear.setFixedSize(130, 36)
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.setStyleSheet(
            "QPushButton { background:rgba(239,68,68,0.12); color:#ef4444; font-size:13px; font-weight:700; border-radius:10px; border:1px solid rgba(239,68,68,0.3); } QPushButton:hover { background:#ef4444; color:#fff; }"
        )
        btn_clear.clicked.connect(self._clear_merge_list)
        tb.addWidget(btn_clear)

        tb.addStretch()

        self.merge_status = QLabel("")
        self.merge_status.setStyleSheet(
            "color:#f59e0b; font-size:12px; font-weight:700;"
        )
        tb.addWidget(self.merge_status)

        btn_run = QPushButton("▶ Khởi chạy Nối Video")
        btn_run.setFixedSize(160, 36)
        btn_run.setCursor(Qt.PointingHandCursor)
        btn_run.setStyleSheet(
            "QPushButton { background:#22c55e; color:#fff; font-size:13px; font-weight:700; border-radius:10px; border:none; } QPushButton:hover { opacity:0.85; }"
        )
        btn_run.clicked.connect(self._run_merge)
        tb.addWidget(btn_run)

        ml.addLayout(tb)

        # List Widget
        self.merge_list = QListWidget()
        self.merge_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.merge_list.setDragDropMode(QListWidget.NoDragDrop)
        self.merge_list.setStyleSheet(
            "QListWidget { background:#151824; border:1px solid #2d3140; border-radius:10px; padding:8px; color:#e8eaed; font-size:13px; }"
            "QListWidget::item { padding:8px; border-bottom:1px solid #1e2235; }"
            "QListWidget::item:selected { background:rgba(108,99,255,0.2); border-radius:6px; }"
        )
        self.merge_list.itemDoubleClicked.connect(self._play_merge_video)
        self.merge_list.itemChanged.connect(self._update_merge_numbers)
        ml.addWidget(self.merge_list, 1)

        hint = QLabel(
            "💡 Mẹo: Tích chọn video theo đúng thứ tự bạn muốn nối. Video tích trước sẽ nối trước. Nháy đúp để xem thử."
        )
        hint.setStyleSheet("color:#6b7280; font-style:italic; font-size:11px;")
        ml.addWidget(hint)
        self.merge_selected_order = []

        return page

    def _update_merge_numbers(self, item=None):
        from PySide6.QtCore import Qt
        import os

        if not hasattr(self, "merge_selected_order"):
            self.merge_selected_order = []

        if item is not None:
            if item.checkState() == Qt.Checked:
                if item not in self.merge_selected_order:
                    self.merge_selected_order.append(item)
            else:
                if item in self.merge_selected_order:
                    self.merge_selected_order.remove(item)

        self.merge_list.blockSignals(True)
        for i in range(self.merge_list.count()):
            it = self.merge_list.item(i)
            path = it.data(Qt.UserRole)
            if not path:
                continue
            fname = os.path.basename(path)
            if it.checkState() == Qt.Checked:
                try:
                    idx = self.merge_selected_order.index(it) + 1
                except ValueError:
                    idx = len(self.merge_selected_order) + 1
                it.setText(f"[ {idx} ] - {fname}")
                it.setForeground(QColor("#e8eaed"))
            else:
                it.setText(f"[ Bỏ qua ] - {fname}")
                it.setForeground(QColor("#6b7280"))
        self.merge_list.blockSignals(False)

    def _play_merge_video(self, item):
        import os
        from PySide6.QtCore import Qt

        path = item.data(Qt.UserRole)
        if path and os.path.exists(path):
            os.startfile(path)

    def _decorate_add_btn(self, btn):
        btn.setStyleSheet(
            "QPushButton { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7c5cfc,stop:1 #a78bfa); "
            "color:#fff; font-size:13px; font-weight:700; border-radius:10px; border:none; }"
            "QPushButton:hover { opacity:0.85; }"
        )

    def _add_merge_files(self):
        from PySide6.QtWidgets import QFileDialog, QListWidgetItem
        from PySide6.QtCore import Qt

        files, _ = QFileDialog.getOpenFileNames(
            self, "Chọn Video cần nối", "", "Video Files (*.mp4 *.avi *.mov *.mkv)"
        )
        if files:
            # Sắp xếp tự động theo tên file nếu người dùng chọn nhiều file cùng lúc
            files.sort()
            for f in files:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, f)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.merge_list.addItem(item)
            self._update_merge_numbers()

    def _clear_merge_list(self):
        self.merge_list.clear()
        if hasattr(self, "merge_selected_order"):
            self.merge_selected_order.clear()

    def _run_merge(self):
        if not hasattr(self, "merge_selected_order"):
            self.merge_selected_order = []
        checked_items = self.merge_selected_order

        if len(checked_items) < 2:
            QMessageBox.warning(
                self, "Lỗi", "Cần chọn (tích) ít nhất 2 video để ghép nối!"
            )
            return

        from PySide6.QtWidgets import QFileDialog

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Lưu Video", "merged_video.mp4", "Video Files (*.mp4)"
        )
        if not out_path:
            return

        # Prepare concat.txt
        concat_file = "concat_temp.txt"
        from PySide6.QtCore import Qt

        try:
            with open(concat_file, "w", encoding="utf-8") as f:
                for item in checked_items:
                    # Replace single quotes and format correctly for FFMPEG
                    safepath = item.data(Qt.UserRole).replace("'", "'\\''")
                    f.write(f"file '{safepath}'\n")
        except Exception as e:
            QMessageBox.critical(self, "Lỗi tạo file tạm", str(e))
            return

        self.merge_status.setText("Đang xử lý FFmpeg...")
        QApplication.processEvents()

        def run_ffmpeg():
            import subprocess

            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
                "-c",
                "copy",
                out_path,
            ]
            try:
                # hide console window on Windows
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                proc = subprocess.run(
                    cmd, startupinfo=startupinfo, capture_output=True, text=True
                )
                if proc.returncode == 0:
                    return True, "Nối video thành công!"
                else:
                    return False, f"FFmpeg Error:\n{proc.stderr}"
            except FileNotFoundError:
                return (
                    False,
                    "Không tìm thấy FFmpeg trong hệ thống (Hãy cài FFmpeg vào biến môi trường PATH).",
                )
            except Exception as e:
                return False, str(e)

        # Chạy block thread or QTimer
        import threading

        def worker():
            ok, msg = run_ffmpeg()
            from PySide6.QtCore import QMetaObject, Q_ARG

            QMetaObject.invokeMethod(
                self,
                "_on_merge_done",
                Qt.QueuedConnection,
                Q_ARG(bool, ok),
                Q_ARG(str, msg),
            )

        threading.Thread(target=worker, daemon=True).start()

    @Slot(bool, str)
    def _on_merge_done(self, ok, msg):
        self.merge_status.setText("")
        # Clean concat temp
        if os.path.exists("concat_temp.txt"):
            try:
                os.remove("concat_temp.txt")
            except:
                pass

        if ok:
            QMessageBox.information(self, "Thành công", msg)
        else:
            QMessageBox.warning(self, "Lỗi Nối Video", msg)


# === CẤU HÌNH BUILD ===
# build.bat sẽ thay đổi dòng này: True = có auth, False = không auth
AUTH_ENABLED = True


if __name__ == "__main__":
    # Auto-tạo cookies.json nếu chưa có (file duy nhất cần bên ngoài)
    if not os.path.exists("cookies.json"):
        with open("cookies.json", "w") as f:
            json.dump([], f)

    # ---- FILE LOGGING (chỉ khi chạy từ exe) ----
    if getattr(sys, "frozen", False):
        import logging
        from datetime import datetime

        _exe_dir = os.path.dirname(sys.executable)
        _log_path = os.path.join(_exe_dir, "veo3.log")

        # Ghi log ra file, giữ tối đa 5000 dòng cuối
        try:
            old_lines = []
            if os.path.exists(_log_path):
                with open(_log_path, "r", encoding="utf-8", errors="ignore") as _f:
                    old_lines = _f.readlines()
                if len(old_lines) > 5000:
                    old_lines = old_lines[-3000:]
            with open(_log_path, "w", encoding="utf-8") as _f:
                _f.writelines(old_lines)
        except Exception:
            pass

        class _LogWriter:
            """Redirect stdout/stderr ra file log + console (nếu có)."""

            def __init__(self, log_file, original):
                self._file = open(log_file, "a", encoding="utf-8", errors="replace")
                self._orig = original

            def write(self, text):
                if text.strip():
                    try:
                        self._file.write(text)
                        self._file.flush()
                    except Exception:
                        pass
                if self._orig:
                    try:
                        self._orig.write(text)
                    except Exception:
                        pass

            def flush(self):
                try:
                    self._file.flush()
                except Exception:
                    pass
                if self._orig:
                    try:
                        self._orig.flush()
                    except Exception:
                        pass

        sys.stdout = _LogWriter(_log_path, sys.stdout)
        sys.stderr = _LogWriter(_log_path, sys.stderr)

        print(f"\n{'='*60}")
        print(f"  Veo3 Log - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Exe: {sys.executable}")
        print(f"{'='*60}")

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")

    # 2. Check update (chỉ khi là exe)
    if getattr(sys, "frozen", False):
        try:
            from core.updater import (
                check_for_update,
                download_and_update,
                restart_app,
                get_local_version,
            )

            has_update, remote_ver, download_url = check_for_update()
            if has_update:
                local_ver = get_local_version()
                reply = QMessageBox.question(
                    None,
                    "Cập nhật mới",
                    f"Phiên bản mới {remote_ver} đã có!\n"
                    f"Bạn đang dùng phiên bản {local_ver}.\n\n"
                    f"Bạn có muốn cập nhật không?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if reply == QMessageBox.Yes:
                    ok, msg = download_and_update(download_url)
                    if ok:
                        QMessageBox.information(None, "Cập nhật", msg)
                        restart_app()
                    else:
                        QMessageBox.warning(None, "Cập nhật", msg)
        except Exception as e:
            print(f"[Update] Check failed: {e}")

    # 3. Auth (nếu bật)
    if AUTH_ENABLED:
        login = LoginDialog()
        if login.exec() != QDialog.Accepted:
            sys.exit(0)

    # 4. Khởi động app
    w = AutoVoiceApp()
    w.show()

    # 5. Kiểm tra license định kỳ (mỗi 60 giây) — tắt app ngay nếu bị khóa
    if AUTH_ENABLED:
        from PySide6.QtCore import QTimer
        import json as _json

        def _periodic_license_check():
            try:
                from core.auth import check_license_active

                sf = "settings.json"
                if not os.path.exists(sf):
                    return
                with open(sf, "r") as f:
                    lk = _json.load(f).get("license_key", "")
                if not lk:
                    return
                if not check_license_active(lk):
                    QMessageBox.critical(
                        w,
                        "Bản quyền bị thu hồi",
                        "Mã kích hoạt của bạn đã bị Admin vô hiệu hóa.\n"
                        "Ứng dụng sẽ tự đóng ngay bây giờ.\n\n"
                        "Liên hệ Admin để được hỗ trợ.",
                    )
                    app.quit()
            except Exception:
                pass

        _license_timer = QTimer()
        _license_timer.timeout.connect(_periodic_license_check)
        _license_timer.start(60_000)  # 60 giây

    sys.exit(app.exec())
