import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QColor, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QComboBox,
)

from account_session_client import AccountSessionError, fetch_account_session_token
from api_server import ApiServerConfig, LocalApiServer
from banana_client import BananaImageClient
from scheduler import BananaJob, BananaScheduler

APP_DIR = Path(__file__).resolve().parent
EXE_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else APP_DIR
SETTINGS_DIR = EXE_DIR if getattr(sys, 'frozen', False) else APP_DIR
SETTINGS_FILE = SETTINGS_DIR / 'gui_settings.json'
JOB_RESULTS_API_URL = 'https://nathamedia.net/api/user-job-results'
SUPABASE_URL = 'https://ncurpuawqbeknkbjwfer.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5jdXJwdWF3cWJla25rYmp3ZmVyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjQ1MTk1MDIsImV4cCI6MjA4MDA5NTUwMn0.07VLBYPfkuzB5MjHeh8DT-e3sZ9pynmDSjQDUpu_40A'
SUPABASE_LICENSE_TABLE = 'licenses'
REQUIRED_LICENSE_TYPE = 'tool_veo_3'
CARD_PREVIEW_WIDTH = 220
CARD_PREVIEW_HEIGHT = 150


class TokenRowWidget(QWidget):
    def __init__(self, token: str = '', remove_callback=None):
        super().__init__()
        self.remove_callback = remove_callback
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.edit = QLineEdit(token)
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText('Nhập access token')
        self.edit.setMinimumHeight(32)
        self.toggle_btn = QPushButton('Hiện')
        self.toggle_btn.clicked.connect(self._toggle_visible)
        self.remove_btn = QPushButton('Xóa')
        self.remove_btn.setObjectName('danger')
        self.remove_btn.clicked.connect(self._remove)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.toggle_btn)
        layout.addWidget(self.remove_btn)

    def token(self) -> str:
        return self.edit.text().strip()

    def set_token(self, token: str) -> None:
        self.edit.setText(token)

    def _toggle_visible(self) -> None:
        visible = self.edit.echoMode() == QLineEdit.Normal
        self.edit.setEchoMode(QLineEdit.Password if visible else QLineEdit.Normal)
        self.toggle_btn.setText('Hiện' if visible else 'Ẩn')

    def _remove(self) -> None:
        if self.remove_callback:
            self.remove_callback(self)


class TokenListWidget(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(6)
        outer.addLayout(self.rows_layout)
        self.add_token('')

    def add_token(self, token: str = '') -> None:
        row = TokenRowWidget(token, self.remove_token_row)
        self.rows_layout.addWidget(row)

    def remove_token_row(self, row: TokenRowWidget) -> None:
        if self.rows_layout.count() <= 1:
            row.set_token('')
            return
        self.rows_layout.removeWidget(row)
        row.deleteLater()

    def token_rows(self) -> list[TokenRowWidget]:
        rows = []
        for index in range(self.rows_layout.count()):
            widget = self.rows_layout.itemAt(index).widget()
            if isinstance(widget, TokenRowWidget):
                rows.append(widget)
        return rows

    def toPlainText(self) -> str:
        return '\n'.join(row.token() for row in self.token_rows() if row.token())

    def setPlainText(self, text: str) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        tokens = [line.strip() for line in (text or '').splitlines() if line.strip()]
        if not tokens:
            tokens = ['']
        for token in tokens:
            self.add_token(token)


class BananaGuiApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Banana Python App')
        self.resize(1400, 920)
        self.setMinimumSize(1180, 760)

        self.settings = self._load_settings()
        self.token_project_map: dict[str, str] = dict(self.settings.get('token_project_map') or {})
        self.result_queue: queue.Queue = queue.Queue()
        self.active_sessions: dict[str, dict] = {}
        self.job_row_map: dict[str, int] = {}
        self.session_counter = 0
        self.result_cards = []
        self.running_schedulers = set()
        self.running_schedulers_lock = threading.Lock()
        self.api_server: LocalApiServer | None = None
        self.is_closing = False
        self.api_key_active = False
        self.api_key_check_message = 'Chưa kiểm tra API key'
        self.api_key_check_value = ''
        self.api_key_check_generation = 0

        self._build_ui()
        self._apply_styles()
        self._apply_settings_to_ui()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_result_queue)
        self.poll_timer.start(200)
        self.license_timer = QTimer(self)
        self.license_timer.timeout.connect(self._periodic_api_key_check)
        self.license_timer.start(60_000)
        QTimer.singleShot(800, self._start_saved_token_check)
        QTimer.singleShot(300, self._start_api_key_activation_check)

    def _supabase_headers(self) -> dict:
        return {
            'apikey': SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json',
            'Prefer': 'return=representation',
        }

    def _is_license_expired(self, license_row: dict) -> bool:
        if bool(license_row.get('is_expired')):
            return True
        duration = (license_row.get('duration') or 'forever').strip()
        activated_at = license_row.get('activated_at')
        if not activated_at or duration == 'forever':
            return False
        try:
            from datetime import datetime, timedelta, timezone
            raw = str(activated_at).replace('Z', '+00:00')
            activated = datetime.fromisoformat(raw)
            if activated.tzinfo is None:
                activated = activated.replace(tzinfo=timezone.utc)
            if duration == '1_day':
                expiry = activated + timedelta(days=1)
            elif duration == '1_month':
                expiry = activated + timedelta(days=31)
            elif duration == '1_year':
                expiry = activated + timedelta(days=365)
            else:
                return False
            return datetime.now(timezone.utc) > expiry
        except Exception:
            return False

    def _get_machine_id(self) -> str:
        raw_parts = []
        if sys.platform.startswith('win'):
            try:
                result = subprocess.run(
                    ['wmic', 'csproduct', 'get', 'uuid'],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
                )
                for line in result.stdout.splitlines():
                    clean = line.strip()
                    if clean and clean.lower() != 'uuid':
                        raw_parts.append(clean)
                        break
            except Exception:
                pass
        raw_parts.append(str(uuid.getnode()))
        raw_parts.append(os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or '')
        raw = '|'.join(part for part in raw_parts if part).strip() or 'unknown-machine'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _patch_license_row(self, api_key: str, payload: dict) -> tuple[bool, str]:
        try:
            response = requests.patch(
                f'{SUPABASE_URL}/rest/v1/{SUPABASE_LICENSE_TABLE}',
                params={'key': f'eq.{api_key}'},
                headers=self._supabase_headers(),
                json=payload,
                timeout=15,
            )
            if response.status_code >= 400:
                return False, f'Lỗi cập nhật license Supabase: HTTP {response.status_code} {response.text}'.strip()
            return True, ''
        except Exception as exc:
            return False, f'Lỗi cập nhật license Supabase: {exc}'

    def _check_api_key_activation(self, api_key: str) -> tuple[bool, str]:
        api_key = (api_key or '').strip()
        if not api_key:
            return False, 'Bạn cần nhập API key trong tab Cài đặt để dùng tool.'
        try:
            response = requests.get(
                f'{SUPABASE_URL}/rest/v1/{SUPABASE_LICENSE_TABLE}',
                params={'key': f'eq.{api_key}', 'limit': '1'},
                headers=self._supabase_headers(),
                timeout=15,
            )
            if response.status_code >= 400:
                return False, f'Lỗi kiểm tra API key Supabase: HTTP {response.status_code} {response.text}'.strip()
            rows = response.json()
        except Exception as exc:
            return False, f'Lỗi kiểm tra API key Supabase: {exc}'

        if not rows:
            return False, 'API key không tồn tại.'
        license_row = rows[0]
        license_type = str(license_row.get('type') or '').strip()
        if license_type != REQUIRED_LICENSE_TYPE:
            return False, f'API key không thuộc type {REQUIRED_LICENSE_TYPE}.'
        if not bool(license_row.get('is_active')):
            return False, 'API key chưa bật hoặc đã bị khóa.'
        if self._is_license_expired(license_row):
            return False, 'API key đã hết hạn.'

        current_machine_id = self._get_machine_id()
        db_machine_id = str(license_row.get('machine_id') or '').strip()
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if db_machine_id and db_machine_id != current_machine_id:
            return False, 'API key đã được sử dụng trên máy khác.'

        patch_payload = {'last_login': now_iso}
        if not db_machine_id:
            patch_payload['machine_id'] = current_machine_id
            if not license_row.get('activated_at'):
                patch_payload['activated_at'] = now_iso
        patched, patch_message = self._patch_license_row(api_key, patch_payload)
        if not patched:
            return False, patch_message
        return True, f'API key hợp lệ: type {REQUIRED_LICENSE_TYPE}, đang bật và đúng máy.'

    def _start_api_key_activation_check(self) -> None:
        if self.is_closing:
            return
        api_key = self.account_api_key_edit.text().strip()
        self.api_key_active = False
        self.api_key_check_value = api_key
        self.api_key_check_generation += 1
        check_generation = self.api_key_check_generation
        self._update_api_key_tab_access()
        if not api_key:
            self.api_key_check_message = 'Bạn cần nhập API key trong tab Cài đặt để dùng tool.'
            self.statusBar().showMessage(self.api_key_check_message, 8000)
            return
        self.statusBar().showMessage('Đang kiểm tra API key...', 8000)

        def worker():
            ok, message = self._check_api_key_activation(api_key)
            self.result_queue.put(('api_key_check_done', ok, message, api_key, check_generation))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_api_key_check_done(self, ok: bool, message: str, api_key: str | None = None, check_generation: int | None = None) -> None:
        if api_key is not None and api_key != self.account_api_key_edit.text().strip():
            return
        if check_generation is not None and check_generation != self.api_key_check_generation:
            return
        self.api_key_active = ok
        self.api_key_check_message = message
        self.api_key_check_value = api_key or self.account_api_key_edit.text().strip()
        self._update_api_key_tab_access()
        if ok:
            self.statusBar().showMessage(message, 10000)
            return
        self._set_running_ui(False)
        self.statusBar().showMessage(f'API key chưa active: {message}', 10000)

    def _periodic_api_key_check(self) -> None:
        if self.is_closing:
            return
        api_key = self.account_api_key_edit.text().strip()
        if not api_key:
            return

        def worker():
            ok, message = self._check_api_key_activation(api_key)
            self.result_queue.put(('api_key_periodic_done', ok, message, api_key))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_api_key_periodic_done(self, ok: bool, message: str, api_key: str) -> None:
        if api_key != self.account_api_key_edit.text().strip():
            return
        was_active = self.api_key_active
        self.api_key_active = ok
        self.api_key_check_message = message
        self.api_key_check_value = api_key
        self._update_api_key_tab_access()
        if ok:
            self.statusBar().showMessage(message, 8000)
            return
        self._set_running_ui(False)
        self.statusBar().showMessage(f'API key chưa active: {message}', 10000)
        if was_active:
            QMessageBox.critical(self, 'API key bị khóa', f'{message}\n\nTool đã khóa các tab chức năng.')

    def _update_api_key_tab_access(self) -> None:
        current_api_key = self.account_api_key_edit.text().strip()
        can_use_tabs = bool(current_api_key) and self.api_key_active and current_api_key == self.api_key_check_value
        if hasattr(self, 'tabs'):
            for index in range(1, self.tabs.count()):
                self.tabs.setTabEnabled(index, can_use_tabs)
            if not can_use_tabs and self.tabs.currentIndex() > 0:
                self.tabs.setCurrentIndex(0)

    def _handle_api_key_text_changed(self, _text: str) -> None:
        self.api_key_active = False
        self._update_api_key_tab_access()
        QTimer.singleShot(700, self._start_api_key_activation_check)

    def _apply_styles(self) -> None:
        self.setStyleSheet('''
            QMainWindow, QWidget#root { background: #eef2ff; color: #111827; font-family: "Segoe UI", "Inter", Arial; }
            QTabWidget::pane { border: 1px solid rgba(148,163,184,0.28); background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #ffffff, stop:1 #f8fbff); border-radius: 18px; }
            QTabBar::tab { background: rgba(255,255,255,0.72); color: #64748b; padding: 10px 22px; border-top-left-radius: 12px; border-top-right-radius: 12px; margin-right: 4px; font-weight: 800; }
            QTabBar::tab:selected { background: #ffffff; color: #1d4ed8; border-bottom: 3px solid #3b82f6; }
            QFrame#card { background: rgba(255,255,255,0.92); border: 1px solid rgba(148,163,184,0.28); border-radius: 18px; }
            QLabel#title { font-size: 22px; font-weight: 900; color: #0f172a; letter-spacing: .2px; }
            QLabel#section { font-size: 12px; font-weight: 900; color: #1e293b; letter-spacing: .6px; text-transform: uppercase; }
            QLabel#muted { color: #64748b; font-size: 11px; }
            QLabel#pill { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563eb, stop:1 #7c3aed); color: white; border-radius: 10px; padding: 4px 9px; font-size: 11px; font-weight: 900; }
            QLabel#subpill { background: #ecfeff; color: #0e7490; border: 1px solid #a5f3fc; border-radius: 9px; padding: 3px 8px; font-size: 10px; font-weight: 900; }
            QLineEdit, QTextEdit, QComboBox, QSpinBox { background: #ffffff; color: #0f172a; border: 1px solid #d6dbe4; border-radius: 10px; padding: 8px 10px; selection-background-color: #2563eb; }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus { border: 2px solid #3b82f6; background: #fbfdff; }
            QPushButton { background: #ffffff; color: #0f172a; border: 1px solid #d6dbe4; border-radius: 10px; padding: 8px 14px; font-weight: 800; }
            QPushButton:hover { background: #f8fafc; border-color: #3b82f6; color:#1d4ed8; }
            QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563eb, stop:1 #7c3aed); color: white; border: none; }
            QPushButton#primary:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #1d4ed8, stop:1 #6d28d9); color:white; }
            QPushButton#danger { color: #dc2626; border-color: #fecaca; background:#fff7f7; }
            QPushButton#mini { padding: 5px 8px; border-radius: 8px; font-size: 10px; background:#f8fafc; color:#334155; }
            QPushButton#copy { padding: 5px 9px; border-radius: 8px; font-size: 10px; background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; }
            QPushButton#copy:hover { background:#dbeafe; }
            QTableWidget { background: #ffffff; border: 1px solid rgba(148,163,184,0.35); border-radius: 14px; gridline-color: #eef2f7; alternate-background-color: #f8fbff; }
            QTableWidget::item { padding: 8px; }
            QHeaderView::section { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #f8fafc, stop:1 #eef2ff); color: #334155; padding: 10px; border: none; border-bottom: 1px solid #d6dbe4; font-weight: 900; }
            QScrollArea { border: none; background: transparent; }
            QToolTip { background: #0f172a; color: #f8fafc; border: 1px solid #334155; border-radius: 10px; padding: 10px; font-size: 12px; }
        ''')

    def _card(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName('card')
        return frame

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName('root')
        main = QVBoxLayout(root)
        main.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        main.addWidget(self.tabs)
        self.setCentralWidget(root)
        self._build_settings_tab()
        self._build_create_tab()
        self._build_edit_tab()

    def _build_settings_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)
        title = QLabel('Access Token')
        title.setObjectName('title')
        layout.addWidget(title)
        desc = QLabel('Mỗi token một dòng. Có thể gọi API để lấy token hoặc vào https://labs.google/fx/api/auth/session rồi copy value access_token.')
        desc.setObjectName('muted')
        layout.addWidget(desc)
        api_key_label = QLabel('API Key lấy token')
        api_key_label.setObjectName('section')
        layout.addWidget(api_key_label)
        self.account_api_key_edit = QLineEdit()
        self.account_api_key_edit.setPlaceholderText('Nhập X-API-Key để lấy token từ API')
        self.account_api_key_edit.textChanged.connect(self._handle_api_key_text_changed)
        layout.addWidget(self.account_api_key_edit)
        self.tokens_text = TokenListWidget()
        layout.addWidget(self.tokens_text)
        proxy_label = QLabel('Proxy theo token')
        proxy_label.setObjectName('section')
        layout.addWidget(proxy_label)
        proxy_desc = QLabel('Mỗi proxy một dòng, map theo token cùng dòng. Hỗ trợ host:port, user:pass@host:port, host:port:user:pass, http://user:pass@host:port.')
        proxy_desc.setObjectName('muted')
        layout.addWidget(proxy_desc)
        self.proxies_text = QTextEdit()
        self.proxies_text.setMaximumHeight(110)
        self.proxies_text.setPlaceholderText('proxy1\nproxy2')
        layout.addWidget(self.proxies_text)
        footer = QHBoxLayout()
        btn = QPushButton('Lưu cài đặt')
        btn.clicked.connect(self._handle_save_settings)
        footer.addWidget(btn)
        add_token_btn = QPushButton('Thêm token')
        add_token_btn.clicked.connect(lambda: self.tokens_text.add_token(''))
        footer.addWidget(add_token_btn)
        api_btn = QPushButton('Lấy token từ API')
        api_btn.clicked.connect(self._handle_fetch_api_token)
        footer.addWidget(api_btn)
        note = QLabel('Lưu vào gui_settings.json trong thư mục app.')
        note.setObjectName('muted')
        footer.addWidget(note)
        footer.addStretch()
        layout.addLayout(footer)
        layout.addStretch()
        self.tabs.addTab(tab, 'Cài đặt')

    def _build_create_tab(self) -> None:
        tab = QWidget()
        page = QVBoxLayout(tab)
        page.setContentsMargins(22, 22, 22, 22)
        page.setSpacing(14)

        header = self._card()
        grid = QGridLayout(header)
        grid.setContentsMargins(18, 18, 18, 18)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.mode_combo = QComboBox(); self.mode_combo.addItems(['image', 'video'])
        self.image_type_combo = QComboBox(); self.image_type_combo.addItems(['text', 'reference'])
        self.video_type_combo = QComboBox(); self.video_type_combo.addItems(['Tạo video từ ảnh frame đầu', 'Tạo video từ ảnh frame đầu/cuối', 'Tạo video từ ảnh thành phần', 'Tạo video từ text'])
        self.model_combo = QComboBox(); self.model_combo.addItems(['GEM_PIX_2', 'NARWHAL'])
        self.image_resolution_combo = QComboBox(); self.image_resolution_combo.addItems(['1K', '4K'])
        self.aspect_combo = QComboBox(); self.aspect_combo.addItems(['16:9', '9:16', '1:1'])
        self.thread_spin = QSpinBox(); self.thread_spin.setRange(1, 999); self.thread_spin.setValue(15)
        self.videos_per_prompt_spin = QSpinBox(); self.videos_per_prompt_spin.setRange(1, 4); self.videos_per_prompt_spin.setValue(1)
        self.save_dir_edit = QLineEdit()
        self.project_name_edit = QLineEdit()
        self.reference_edit = QLineEdit()

        self.image_type_label = QLabel('Kiểu image')
        self.video_type_label = QLabel('Kiểu video')
        self.image_resolution_label = QLabel('Độ phân giải ảnh')
        self.thread_label = QLabel('Số luồng')
        self.videos_per_prompt_label = QLabel('Số video / 1 prompt')
        controls = [
            ('Chế độ', self.mode_combo, 0, 0),
            ('Kiểu image', self.image_type_combo, 0, 1),
            ('Kiểu video', self.video_type_combo, 0, 1),
            ('Model', self.model_combo, 0, 2),
            ('Độ phân giải ảnh', self.image_resolution_combo, 0, 3),
            ('Tỷ lệ', self.aspect_combo, 2, 0),
        ]
        for label, widget, row, col in controls:
            lab = self.video_type_label if label == 'Kiểu video' else self.image_type_label if label == 'Kiểu image' else self.image_resolution_label if label == 'Độ phân giải ảnh' else QLabel(label)
            lab.setObjectName('section')
            grid.addWidget(lab, row, col)
            grid.addWidget(widget, row + 1, col)
        grid.addWidget(QLabel('Thư mục lưu'), 2, 1)
        grid.addWidget(self.save_dir_edit, 3, 1, 1, 2)
        choose_save = QPushButton('Chọn thư mục'); choose_save.clicked.connect(self._choose_save_dir)
        grid.addWidget(choose_save, 3, 3)
        self.thread_label.setObjectName('section')
        grid.addWidget(self.thread_label, 4, 0)
        grid.addWidget(self.thread_spin, 5, 0)
        self.videos_per_prompt_label.setObjectName('section')
        grid.addWidget(self.videos_per_prompt_label, 4, 1)
        grid.addWidget(self.videos_per_prompt_spin, 5, 1)
        grid.addWidget(QLabel('Tên project'), 4, 2)
        grid.addWidget(self.project_name_edit, 5, 2, 1, 2)
        self.thread_preview_label = QLabel('Dự kiến: 0 job | Luồng thực tối đa: 0')
        self.thread_preview_label.setObjectName('muted')
        grid.addWidget(self.thread_preview_label, 6, 0, 1, 4)
        page.addWidget(header)

        prompt_card = self._card()
        prompt_layout = QVBoxLayout(prompt_card)
        prompt_layout.setContentsMargins(18, 18, 18, 18)
        title_row = QHBoxLayout()
        lbl = QLabel('Bảng Prompt + Ảnh tham chiếu riêng'); lbl.setObjectName('section')
        title_row.addWidget(lbl); title_row.addStretch()
        m = QLabel('Mỗi dòng = 1 job. Ảnh riêng dùng cho tạo video từ ảnh. Path ảnh có thể dùng full path hoặc relative path cùng thư mục app/exe; / hoặc \\ đều được.')
        m.setObjectName('muted')
        title_row.addWidget(m)
        prompt_layout.addLayout(title_row)
        self.bulk_prompts_text = QTextEdit(); self.bulk_prompts_text.setMaximumHeight(120)
        self.bulk_prompts_text.setPlaceholderText('Nhập prompt hàng loạt - mỗi dòng là 1 prompt')
        prompt_layout.addWidget(self.bulk_prompts_text)
        actions = QHBoxLayout()
        for text, cb in [
            ('Đổ prompt vào bảng', self._apply_bulk_prompts_to_table),
            ('Import Excel', self._import_excel_prompts),
            ('Thêm dòng', self._add_prompt_row),
            ('Xóa dòng đã chọn', self._remove_selected_prompt_rows),
            ('Xóa dòng trống', self._remove_empty_prompt_rows),
            ('Xóa tất cả', self._clear_all_prompt_rows),
        ]:
            b = QPushButton(text); b.clicked.connect(cb); actions.addWidget(b)
        actions.addStretch(); prompt_layout.addLayout(actions)
        self.prompt_table = QTableWidget(0, 8)
        self.prompt_table.setHorizontalHeaderLabels(['Chọn', 'Prompt / xem nhanh', 'Ảnh tham chiếu', 'Ảnh cuối', 'KQ', 'Link', 'Mở thư mục', 'Xóa'])
        self.prompt_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.prompt_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.prompt_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.prompt_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.prompt_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.prompt_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.prompt_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.prompt_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.prompt_table.setAlternatingRowColors(True)
        self.prompt_table.setShowGrid(False)
        self.prompt_table.verticalHeader().setDefaultSectionSize(86)
        self.prompt_table.verticalHeader().setVisible(False)
        self.prompt_table.setMinimumHeight(310)
        prompt_layout.addWidget(self.prompt_table, 1)
        page.addWidget(prompt_card, 1)

        control = self._card()
        c_layout = QHBoxLayout(control)
        self.start_button = QPushButton('Tạo'); self.start_button.setObjectName('primary'); self.start_button.clicked.connect(self._start_generation)
        clear = QPushButton('Clear UI'); clear.clicked.connect(self._clear_create_form)
        self.status_label = QLabel('Sẵn sàng')
        self.summary_label = QLabel('Chưa có kết quả'); self.summary_label.setObjectName('muted')
        c_layout.addWidget(self.start_button); c_layout.addWidget(clear); c_layout.addWidget(self.status_label); c_layout.addStretch(); c_layout.addWidget(self.summary_label)
        page.addWidget(control)


        self.tabs.addTab(tab, 'Tạo')
        self.mode_combo.currentTextChanged.connect(self._update_mode_ui)
        self.image_type_combo.currentTextChanged.connect(self._update_image_type_ui)
        self.video_type_combo.currentTextChanged.connect(self._update_video_type_ui)
        self.thread_spin.valueChanged.connect(self._update_thread_preview_ui)
        self.videos_per_prompt_spin.valueChanged.connect(self._update_thread_preview_ui)
        self.bulk_prompts_text.textChanged.connect(self._update_thread_preview_ui)
        self._update_mode_ui()
        self._update_thread_preview_ui()

    def _build_edit_tab(self) -> None:
        tab = QWidget()
        page = QVBoxLayout(tab)
        page.setContentsMargins(22, 22, 22, 22)
        page.setSpacing(12)

        header = self._card()
        header_layout = QVBoxLayout(header)
        title = QLabel('Edit video - nối nhiều video')
        title.setObjectName('title')
        desc = QLabel('Import folder video, tick chọn theo thứ tự muốn nối, rồi xuất ra một video mới. Chức năng này chạy local bằng ffmpeg và tách riêng logic tạo video.')
        desc.setObjectName('muted')
        header_layout.addWidget(title)
        header_layout.addWidget(desc)

        folder_row = QHBoxLayout()
        self.edit_folder_edit = QLineEdit()
        self.edit_folder_edit.setReadOnly(True)
        self.edit_folder_edit.setPlaceholderText('Chưa chọn folder video')
        import_btn = QPushButton('Import folder video')
        import_btn.clicked.connect(self._edit_import_folder)
        clear_btn = QPushButton('Bỏ chọn tất cả')
        clear_btn.clicked.connect(self._edit_clear_selection)
        up_btn = QPushButton('Đưa lên')
        up_btn.clicked.connect(lambda: self._edit_move_selected_order(-1))
        down_btn = QPushButton('Đưa xuống')
        down_btn.clicked.connect(lambda: self._edit_move_selected_order(1))
        folder_row.addWidget(self.edit_folder_edit, 1)
        folder_row.addWidget(import_btn)
        folder_row.addWidget(clear_btn)
        folder_row.addWidget(up_btn)
        folder_row.addWidget(down_btn)
        header_layout.addLayout(folder_row)
        page.addWidget(header)

        self.edit_video_table = QTableWidget(0, 5)
        self.edit_video_table.setHorizontalHeaderLabels(['Chọn', 'Thứ tự', 'Tên file', 'Đường dẫn', 'Dung lượng'])
        self.edit_video_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.edit_video_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.edit_video_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.edit_video_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.edit_video_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.edit_video_table.itemChanged.connect(self._edit_video_item_changed)
        page.addWidget(self.edit_video_table, 1)

        output_card = self._card()
        output_layout = QHBoxLayout(output_card)
        self.edit_output_edit = QLineEdit()
        self.edit_output_edit.setPlaceholderText('Chọn file output .mp4')
        choose_output_btn = QPushButton('Chọn output')
        choose_output_btn.clicked.connect(self._edit_choose_output)
        self.edit_concat_btn = QPushButton('Nối video đã chọn')
        self.edit_concat_btn.setObjectName('primary')
        self.edit_concat_btn.clicked.connect(self._edit_start_concat)
        self.edit_status_label = QLabel('Chưa nối video')
        self.edit_status_label.setObjectName('muted')
        output_layout.addWidget(self.edit_output_edit, 1)
        output_layout.addWidget(choose_output_btn)
        output_layout.addWidget(self.edit_concat_btn)
        output_layout.addWidget(self.edit_status_label)
        page.addWidget(output_card)

        self.edit_video_paths = []
        self.edit_selected_paths = []
        self.edit_updating_table = False
        self.tabs.addTab(tab, 'Edit')

    def _build_api_tab(self) -> None:
        tab = QWidget()
        page = QVBoxLayout(tab)
        page.setContentsMargins(22, 22, 22, 22)
        page.setSpacing(12)

        header = self._card()
        header_layout = QVBoxLayout(header)
        title = QLabel('REST API Local')
        title.setObjectName('title')
        desc = QLabel('Bật API tại http://localhost:3000/ để tạo ảnh/video bằng HTTP. API dùng token và cấu hình đã lưu, chỉ thay thao tác trên tab Tạo.')
        desc.setObjectName('muted')
        header_layout.addWidget(title)
        header_layout.addWidget(desc)
        controls = QHBoxLayout()
        self.api_status_label = QLabel('API đang tắt')
        self.api_status_label.setObjectName('section')
        self.api_start_button = QPushButton('Bật API')
        self.api_start_button.setObjectName('primary')
        self.api_start_button.clicked.connect(self._start_api_server)
        self.api_stop_button = QPushButton('Tắt API')
        self.api_stop_button.setObjectName('danger')
        self.api_stop_button.clicked.connect(self._stop_api_server)
        self.api_stop_button.setEnabled(False)
        controls.addWidget(self.api_start_button)
        controls.addWidget(self.api_stop_button)
        controls.addWidget(self.api_status_label)
        controls.addStretch()
        header_layout.addLayout(controls)
        page.addWidget(header)

        docs = QTextEdit()
        docs.setReadOnly(True)
        docs.setPlainText('''API chạy tại: http://localhost:3000/

1) Check server:
GET /health

2) Tạo task:
POST /tasks
Content-Type: application/json

Tạo ảnh:
{
  "action_type": "CREATE_IMAGE",
  "model": "NARWHAL",
  "screen_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
  "prompts": ["A beautiful sunset over the ocean"],
  "upsample_resolution": null
}

Tạo ảnh 4K:
{
  "action_type": "CREATE_IMAGE",
  "model": "NARWHAL",
  "screen_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
  "prompts": ["A beautiful sunset over the ocean"],
  "upsample_resolution": "4K"
}

Tạo video từ ảnh:
{
  "action_type": "IMAGE_TO_VIDEO",
  "name": "video from image",
  "model": "FAST",
  "screen_ratio": "16:9",
  "prompts": ["Camera slowly zooms out"],
  "image_paths": ["D:/images/start.png"]
}

Tạo video ảnh đầu/cuối:
{
  "action_type": "FRAMES_TO_VIDEO",
  "name": "video name",
  "model": "FAST",
  "screen_ratio": "16:9",
  "prompts": ["transition between frames"],
  "image_paths": ["D:/images/start.png", "D:/images/end.png"]
}

Response tạo task trả ngay status PENDING và id task.

3) Poll xem task xong chưa:
GET /tasks/{id}

Status có thể là: PENDING, COMPLETED, FAILED.
Nếu COMPLETED sẽ có output_filename.
Nếu FAILED sẽ có error.

4) Xem danh sách task:
GET /tasks
''')
        page.addWidget(docs, 1)

        self.api_log_text = QTextEdit()
        self.api_log_text.setReadOnly(True)
        self.api_log_text.setMaximumHeight(120)
        self.api_log_text.setPlaceholderText('Log API...')
        page.addWidget(self.api_log_text)
        self.tabs.addTab(tab, 'API')
        self._update_api_key_tab_access()

    VIDEO_TYPE_LABELS = {
        'single': 'Tạo video từ ảnh frame đầu',
        'start_end': 'Tạo video từ ảnh frame đầu/cuối',
        'reference': 'Tạo video từ ảnh thành phần',
        'text': 'Tạo video từ text',
    }
    VIDEO_TYPE_VALUES = {label: value for value, label in VIDEO_TYPE_LABELS.items()}

    def _video_type_value(self) -> str:
        return self.VIDEO_TYPE_VALUES.get(self.video_type_combo.currentText(), self.video_type_combo.currentText() or 'single')

    def _set_video_type_value(self, value: str) -> None:
        self.video_type_combo.setCurrentText(self.VIDEO_TYPE_LABELS.get(value, value or self.VIDEO_TYPE_LABELS['single']))

    def _load_settings(self) -> dict:
        if SETTINGS_FILE.exists():
            try:
                return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            except Exception:
                pass
        return {'tokens': '', 'proxies': '', 'account_api_key': '', 'token_project_map': {}, 'save_dir': str(APP_DIR / 'outputs'), 'project_name': '', 'thread_count': 15, 'videos_per_prompt': 1, 'mode': 'image', 'image_type': 'text', 'video_type': 'single', 'model': 'GEM_PIX_2', 'image_resolution': '1K', 'aspect_ratio': '16:9', 'reference_path': ''}

    def _save_settings(self) -> None:
        data = {
            'tokens': self.tokens_text.toPlainText().strip(),
            'proxies': self.proxies_text.toPlainText().strip() if hasattr(self, 'proxies_text') else '',
            'account_api_key': self.account_api_key_edit.text().strip(),
            'save_dir': self.save_dir_edit.text().strip(),
            'project_name': self.project_name_edit.text().strip(),
            'thread_count': self.thread_spin.value(),
            'videos_per_prompt': self.videos_per_prompt_spin.value(),
            'mode': self.mode_combo.currentText(),
            'image_type': self.image_type_combo.currentText(),
            'video_type': self._video_type_value(),
            'model': self.model_combo.currentText(),
            'image_resolution': self.image_resolution_combo.currentText(),
            'aspect_ratio': self.aspect_combo.currentText(),
            'reference_path': self.reference_edit.text().strip(),
            'token_project_map': {
                token: project_id
                for token, project_id in self.token_project_map.items()
                if token and project_id
            },
        }
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.settings = data

    def _apply_settings_to_ui(self) -> None:
        self.tokens_text.setPlainText(self.settings.get('tokens', ''))
        if hasattr(self, 'proxies_text'):
            self.proxies_text.setPlainText(self.settings.get('proxies', ''))
        self.account_api_key_edit.setText(self.settings.get('account_api_key', ''))
        self.save_dir_edit.setText(self.settings.get('save_dir', str(APP_DIR / 'outputs')))
        self.project_name_edit.setText(self.settings.get('project_name', ''))
        self.thread_spin.setValue(int(self.settings.get('thread_count', 15) or 15))
        self.videos_per_prompt_spin.setValue(max(1, min(4, int(self.settings.get('videos_per_prompt', 1) or 1))))
        self.mode_combo.setCurrentText(self.settings.get('mode', 'image'))
        self.image_type_combo.setCurrentText(self.settings.get('image_type', 'text'))
        self._set_video_type_value(self.settings.get('video_type', 'single'))
        self.model_combo.setCurrentText(self.settings.get('model', 'GEM_PIX_2'))
        self.image_resolution_combo.setCurrentText(self.settings.get('image_resolution', '1K'))
        self.aspect_combo.setCurrentText(self.settings.get('aspect_ratio', '16:9'))
        self.reference_edit.setText(self.settings.get('reference_path', ''))
        self._update_api_key_tab_access()
        self._update_mode_ui()

    def _update_mode_ui(self) -> None:
        mode = self.mode_combo.currentText() or 'image'
        is_video = mode == 'video'
        self.video_type_combo.setVisible(is_video)
        self.video_type_label.setVisible(is_video)
        self.videos_per_prompt_label.setVisible(is_video)
        self.videos_per_prompt_spin.setVisible(is_video)
        self.videos_per_prompt_spin.setEnabled(is_video)
        if not is_video and self.videos_per_prompt_spin.value() != 1:
            self.videos_per_prompt_spin.setValue(1)
        self.image_type_combo.setVisible(not is_video)
        self.image_type_label.setVisible(not is_video)
        self.model_combo.clear()
        self.aspect_combo.clear()
        self.image_resolution_label.setVisible(not is_video)
        self.image_resolution_combo.setVisible(not is_video)
        if is_video:
            self.model_combo.addItems(['VIDEO_I2V'])
            self.aspect_combo.addItems(['16:9', '9:16'])
        else:
            self._set_video_type_value('single')
            self.model_combo.addItems(['GEM_PIX_2', 'NARWHAL'])
            self.aspect_combo.addItems(['16:9', '9:16', '1:1'])
        self._update_image_type_ui()
        self._update_video_type_ui()
        self._update_thread_preview_ui()

    def _update_image_type_ui(self) -> None:
        video_type = self._video_type_value()
        show_reference = (self.mode_combo.currentText() == 'video' and video_type != 'text') or self.image_type_combo.currentText() == 'reference'
        self.prompt_table.setColumnHidden(2, not show_reference)
        if self.mode_combo.currentText() == 'video':
            self.prompt_table.horizontalHeaderItem(2).setText('Ảnh tham chiếu' if video_type == 'reference' else 'Ảnh đầu / tham chiếu')
        else:
            self.prompt_table.horizontalHeaderItem(2).setText('Ảnh tham chiếu')

    def _update_video_type_ui(self) -> None:
        video_type = self._video_type_value()
        is_video = self.mode_combo.currentText() == 'video'
        show_end_image = is_video and video_type == 'start_end'
        self.prompt_table.setColumnHidden(3, not show_end_image)
        if is_video:
            self.prompt_table.setColumnHidden(2, video_type == 'text')
            self.prompt_table.horizontalHeaderItem(2).setText('Ảnh tham chiếu' if video_type == 'reference' else 'Ảnh đầu / tham chiếu')
            self.prompt_table.horizontalHeaderItem(3).setText('Ảnh cuối / tham chiếu' if video_type == 'start_end' else 'Ảnh tham chiếu')

    def _handle_save_settings(self) -> None:
        self._save_settings()
        self._start_api_key_activation_check()
        QMessageBox.information(self, 'Thông báo', 'Đã lưu cài đặt và đang kiểm tra API key')

    def _handle_fetch_api_token(self) -> None:
        api_key = self.account_api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, 'Thiếu API key', 'Bạn cần nhập API key trước khi lấy token từ API')
            return
        try:
            session = fetch_account_session_token(api_key=api_key)
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi lấy token API', str(exc))
            return
        token = session['token']
        current_tokens = [line.strip() for line in self.tokens_text.toPlainText().splitlines() if line.strip()]
        current_tokens = [existing for existing in current_tokens if existing != token]
        self.tokens_text.setPlainText('\n'.join([token, *current_tokens]))
        account_name = session.get('account_name') or 'API'
        project_id = session.get('project_id') or ''
        if project_id:
            self.token_project_map[token] = project_id
            self._save_settings()
        suffix = f' | project_id={project_id}' if project_id else ''
        self.statusBar().showMessage(f'Đã lấy token từ API: {account_name}{suffix}. Token API được ưu tiên chạy trước.', 8000)

    def _start_saved_token_check(self) -> None:
        tokens = [line.strip() for line in self.tokens_text.toPlainText().splitlines() if line.strip()]
        if not tokens or self.is_closing:
            return
        self.statusBar().showMessage('Đang kiểm tra hạn token đã lưu...', 8000)

        def worker():
            checker = BananaImageClient(logger=lambda _message: None, lane_id='token-check')
            results = []
            try:
                for index, token in enumerate(tokens, start=1):
                    token_short = f'{token[:6]}...{token[-4:]}' if len(token) > 12 else token
                    try:
                        info = checker.fetch_token_info(token)
                        results.append({
                            'index': index,
                            'token': token_short,
                            'ok': True,
                            'credits': info.get('credits'),
                            'tier': info.get('userPaygateTier') or info.get('serviceTier') or 'UNKNOWN',
                            'error': '',
                        })
                    except Exception as exc:
                        results.append({
                            'index': index,
                            'token': token_short,
                            'ok': False,
                            'credits': None,
                            'tier': '',
                            'error': str(exc),
                        })
            finally:
                try:
                    checker.shutdown(remove_profile=False)
                except Exception:
                    pass
            self.result_queue.put(('token_check_done', results))

        threading.Thread(target=worker, daemon=True).start()

    def _show_token_check_popup(self, results: list[dict]) -> None:
        if self.is_closing:
            return
        ok_count = sum(1 for item in results if item.get('ok'))
        lines = [f'Tổng token: {len(results)} | Còn hạn: {ok_count} | Lỗi/hết hạn: {len(results) - ok_count}', '']
        for item in results:
            if item.get('ok'):
                lines.append(f"✅ Token {item['index']} ({item['token']}): còn hạn | credits={item.get('credits')} | tier={item.get('tier')}")
            else:
                lines.append(f"❌ Token {item['index']} ({item['token']}): hết hạn/lỗi | {item.get('error')}")
        QMessageBox.information(self, 'Kiểm tra token đã lưu', '\n'.join(lines))
        self.statusBar().showMessage(f'Kiểm tra token xong: {ok_count}/{len(results)} token còn hạn', 10000)

    def _api_tokens(self) -> list[str]:
        return [line.strip() for line in self.tokens_text.toPlainText().splitlines() if line.strip()]

    def _api_proxies(self) -> list[str]:
        if not hasattr(self, 'proxies_text'):
            return []
        return [line.strip() for line in self.proxies_text.toPlainText().splitlines() if line.strip()]

    def _api_token_project_map(self) -> dict[str, str]:
        return dict(self.token_project_map)

    def _api_save_dir(self) -> str:
        return self.save_dir_edit.text().strip() or str(APP_DIR / 'outputs')

    def _api_thread_count(self) -> int:
        return max(1, int(self.thread_spin.value()))

    def _append_api_log(self, message: str) -> None:
        if hasattr(self, 'api_log_text'):
            self.api_log_text.append(f'{time.strftime("%H:%M:%S")} {message}')

    def _start_api_server(self) -> None:
        if self.api_server and self.api_server.is_running():
            return
        self._save_settings()
        config = ApiServerConfig(
            tokens_provider=self._api_tokens,
            proxies_provider=self._api_proxies,
            token_project_map_provider=self._api_token_project_map,
            save_dir_provider=self._api_save_dir,
            thread_count_provider=self._api_thread_count,
            runtime_root=APP_DIR / '.runtime',
            log_callback=lambda message: self.result_queue.put(('api_log', message)),
        )
        try:
            self.api_server = LocalApiServer(config=config, port=3000)
            self.api_server.start()
        except Exception as exc:
            self.api_server = None
            QMessageBox.critical(self, 'API', f'Không bật được API: {exc}')
            return
        self.api_start_button.setEnabled(False)
        self.api_stop_button.setEnabled(True)
        self.api_status_label.setText('API đang chạy: http://localhost:3000/')
        self._append_api_log('Đã bật API tại http://localhost:3000/')

    def _stop_api_server(self) -> None:
        if self.api_server:
            try:
                self.api_server.stop()
            except Exception as exc:
                logging.warning('stage=api action=stop failed=%s', exc)
        self.api_server = None
        if hasattr(self, 'api_start_button'):
            self.api_start_button.setEnabled(True)
            self.api_stop_button.setEnabled(False)
            self.api_status_label.setText('API đang tắt')
            self._append_api_log('Đã tắt API')

    def _choose_save_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, 'Chọn thư mục lưu')
        if path:
            self.save_dir_edit.setText(path)

    def _choose_row_reference(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, 'Chọn ảnh tham chiếu cho dòng này', '', 'Image files (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*.*)')
        if path:
            edit.setText(path)

    def _add_prompt_row(self, prompt: str = '', reference_path: str = '', end_reference_path: str = '') -> None:
        row = self.prompt_table.rowCount()
        self.prompt_table.insertRow(row)
        self.prompt_table.setRowHeight(row, 88)
        selected = QTableWidgetItem()
        selected.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        selected.setCheckState(Qt.Unchecked)
        self.prompt_table.setItem(row, 0, selected)
        self.prompt_table.setCellWidget(row, 1, self._prompt_cell(prompt, reference_path, end_reference_path))
        start_edit = QLineEdit(reference_path); end_edit = QLineEdit(end_reference_path)
        self.prompt_table.setCellWidget(row, 2, self._path_picker(start_edit, 'Ảnh đầu / reference'))
        self.prompt_table.setCellWidget(row, 3, self._path_picker(end_edit, 'Ảnh cuối'))
        result_item = QTableWidgetItem('Đợi chạy')
        result_item.setFlags(Qt.ItemIsEnabled)
        result_item.setTextAlignment(Qt.AlignCenter)
        self.prompt_table.setItem(row, 4, result_item)
        self._set_row_actions(row, None, None)
        delete_btn = QPushButton('Xóa'); delete_btn.setObjectName('danger'); delete_btn.clicked.connect(lambda _=False, b=delete_btn: self._remove_prompt_button_row(b))
        self.prompt_table.setCellWidget(row, 7, delete_btn)
        self._refresh_prompt_row_preview(row)

    def _copy_text_to_clipboard(self, text: str, message: str = 'Đã copy') -> None:
        QApplication.clipboard().setText(text or '')
        self.statusBar().showMessage(message, 2500)

    def _row_mode_label(self) -> str:
        mode = self.mode_combo.currentText() if hasattr(self, 'mode_combo') else 'image'
        if mode != 'video':
            image_type = self.image_type_combo.currentText() if hasattr(self, 'image_type_combo') else 'text'
            return 'TEXT → IMAGE' if image_type == 'text' else 'REFERENCE → IMAGE'
        video_type = self._video_type_value() if hasattr(self, 'video_type_combo') else 'single'
        labels = {
            'single': 'ẢNH ĐẦU → VIDEO',
            'start_end': 'ẢNH ĐẦU + ẢNH CUỐI → VIDEO',
            'reference': 'ẢNH REFERENCE → VIDEO',
            'text': 'TEXT → VIDEO',
        }
        return labels.get(video_type, 'VIDEO')

    def _prompt_detail_tooltip(self, prompt: str, reference_path: str = '', end_reference_path: str = '') -> str:
        model = self.model_combo.currentText() if hasattr(self, 'model_combo') else ''
        aspect = self.aspect_combo.currentText() if hasattr(self, 'aspect_combo') else ''
        image_lines = []
        if reference_path:
            image_lines.append(f'Ảnh đầu/reference: {reference_path}')
        if end_reference_path:
            image_lines.append(f'Ảnh cuối: {end_reference_path}')
        if not image_lines:
            image_lines.append('Ảnh tham chiếu: không có')
        return '\n'.join([
            self._row_mode_label(),
            f'Model: {model} · Tỷ lệ: {aspect}',
            '',
            'PROMPT:',
            prompt or '(trống)',
            '',
            *image_lines,
            '',
            'Nút Copy prompt để copy full prompt. Nút Copy path để copy đường dẫn ảnh.',
        ])

    def _prompt_cell(self, prompt: str = '', reference_path: str = '', end_reference_path: str = '') -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        top = QHBoxLayout()
        pill = QLabel(self._row_mode_label())
        pill.setObjectName('pill')
        model = self.model_combo.currentText() if hasattr(self, 'model_combo') else ''
        model_pill = QLabel(model)
        model_pill.setObjectName('subpill')
        top.addWidget(pill)
        top.addWidget(model_pill)
        top.addStretch()
        copy_btn = QPushButton('Copy prompt')
        copy_btn.setObjectName('copy')
        top.addWidget(copy_btn)
        lay.addLayout(top)

        edit = QLineEdit(prompt)
        edit.setPlaceholderText('Nhập prompt... hover để xem full, bấm Copy prompt để copy')
        edit.setToolTip(self._prompt_detail_tooltip(prompt, reference_path, end_reference_path))
        edit.textChanged.connect(lambda text, e=edit: e.setToolTip(self._prompt_detail_tooltip(text, self._path_text_for_row_widget(wrap, 2), self._path_text_for_row_widget(wrap, 3))))
        copy_btn.clicked.connect(lambda _=False, e=edit: self._copy_text_to_clipboard(e.text(), 'Đã copy prompt'))
        lay.addWidget(edit)

        wrap.prompt_edit = edit
        return wrap

    def _path_text_for_row_widget(self, widget: QWidget, column: int) -> str:
        try:
            for row in range(self.prompt_table.rowCount()):
                if self.prompt_table.cellWidget(row, 1) is widget:
                    path_widget = self.prompt_table.cellWidget(row, column)
                    return path_widget.path_edit.text().strip() if path_widget and hasattr(path_widget, 'path_edit') else ''
        except Exception:
            pass
        return ''

    def _copy_image_to_clipboard(self, path_text: str) -> None:
        normalized = self._normalize_image_path_input(path_text)
        pix = QPixmap(normalized)
        if pix.isNull():
            self._copy_text_to_clipboard(path_text, 'Không đọc được ảnh, đã copy path')
            return
        QApplication.clipboard().setPixmap(pix)
        self.statusBar().showMessage('Đã copy ảnh vào clipboard', 2500)

    def _refresh_prompt_row_preview(self, row: int) -> None:
        prompt_widget = self.prompt_table.cellWidget(row, 1)
        if not prompt_widget or not hasattr(prompt_widget, 'prompt_edit'):
            return
        prompt = prompt_widget.prompt_edit.text()
        ref_widget = self.prompt_table.cellWidget(row, 2)
        end_widget = self.prompt_table.cellWidget(row, 3)
        reference_path = ref_widget.path_edit.text().strip() if ref_widget and hasattr(ref_widget, 'path_edit') else ''
        end_reference_path = end_widget.path_edit.text().strip() if end_widget and hasattr(end_widget, 'path_edit') else ''
        prompt_widget.prompt_edit.setToolTip(self._prompt_detail_tooltip(prompt, reference_path, end_reference_path))
        if hasattr(prompt_widget, 'findChildren'):
            for label in prompt_widget.findChildren(QLabel):
                if label.objectName() == 'pill':
                    label.setText(self._row_mode_label())
                elif label.objectName() == 'subpill':
                    label.setText(self.model_combo.currentText() if hasattr(self, 'model_combo') else '')

    def _refresh_prompt_row_preview_for_path_widget(self, path_widget: QWidget) -> None:
        try:
            for row in range(self.prompt_table.rowCount()):
                if self.prompt_table.cellWidget(row, 2) is path_widget or self.prompt_table.cellWidget(row, 3) is path_widget:
                    self._refresh_prompt_row_preview(row)
                    return
        except Exception:
            return

    def _path_picker(self, edit: QLineEdit, label: str = 'Ảnh tham chiếu') -> QWidget:
        wrap = QWidget(); lay = QHBoxLayout(wrap); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(5)
        edit.setPlaceholderText('VD: image_01.jpg hoặc D:/data/images/image_01.jpg')
        edit.setToolTip('Hover/copy để xem path ảnh. Có thể nhập full path hoặc relative path cùng thư mục exe/app.')
        edit.textChanged.connect(lambda _text, w=wrap: self._refresh_prompt_row_preview_for_path_widget(w))
        btn = QPushButton('Chọn'); btn.setObjectName('mini'); btn.clicked.connect(lambda: self._choose_row_reference(edit))
        copy_btn = QPushButton('Copy path'); copy_btn.setObjectName('copy'); copy_btn.clicked.connect(lambda _=False, e=edit: self._copy_text_to_clipboard(e.text(), 'Đã copy path ảnh'))
        copy_img_btn = QPushButton('Copy ảnh'); copy_img_btn.setObjectName('copy'); copy_img_btn.clicked.connect(lambda _=False, e=edit: self._copy_image_to_clipboard(e.text()))
        lay.addWidget(edit, 1); lay.addWidget(btn); lay.addWidget(copy_btn); lay.addWidget(copy_img_btn)
        wrap.path_edit = edit
        return wrap

    def _remove_prompt_button_row(self, button: QPushButton) -> None:
        for row in range(self.prompt_table.rowCount()):
            if self.prompt_table.cellWidget(row, 7) is button:
                if self.prompt_table.rowCount() <= 1:
                    self._set_row_values(row, '', '', '')
                else:
                    self.prompt_table.removeRow(row)
                return

    def _prompt_edit_for_row(self, row: int) -> QLineEdit | None:
        widget = self.prompt_table.cellWidget(row, 1)
        if widget is None:
            return None
        if hasattr(widget, 'prompt_edit'):
            return widget.prompt_edit
        if isinstance(widget, QLineEdit):
            return widget
        return None

    def _set_row_values(self, row: int, prompt: str, reference_path: str, end_reference_path: str) -> None:
        prompt_edit = self._prompt_edit_for_row(row)
        if prompt_edit:
            prompt_edit.setText(prompt)
        self.prompt_table.cellWidget(row, 2).path_edit.setText(reference_path)
        self.prompt_table.cellWidget(row, 3).path_edit.setText(end_reference_path)
        item = self.prompt_table.item(row, 0)
        if item:
            item.setCheckState(Qt.Unchecked)
        self._refresh_prompt_row_preview(row)

    def _row_values(self) -> list[tuple[str, str, str, bool]]:
        values = []
        for row in range(self.prompt_table.rowCount()):
            prompt_edit = self._prompt_edit_for_row(row)
            prompt = prompt_edit.text().strip() if prompt_edit else ''
            reference_path = self.prompt_table.cellWidget(row, 2).path_edit.text().strip()
            end_reference_path = self.prompt_table.cellWidget(row, 3).path_edit.text().strip()
            selected = self.prompt_table.item(row, 0).checkState() == Qt.Checked
            values.append((prompt, reference_path, end_reference_path, selected))
        return values

    def _rebuild_prompt_rows(self, values=None) -> None:
        self.prompt_table.setRowCount(0)
        if not values:
            return
        for value in values:
            prompt, reference_path, *rest = value
            end_reference_path = rest[-1] if rest else ''
            self._add_prompt_row(prompt, reference_path, end_reference_path)

    def _remove_selected_prompt_rows(self) -> None:
        kept = [(p, r, e) for p, r, e, s in self._row_values() if not s]
        self._rebuild_prompt_rows(kept)

    def _remove_empty_prompt_rows(self) -> None:
        kept = [(p, r, e) for p, r, e, _s in self._row_values() if p or r or e]
        self._rebuild_prompt_rows(kept)

    def _clear_all_prompt_rows(self) -> None:
        self.bulk_prompts_text.clear()
        self._rebuild_prompt_rows([])
        self.status_label.setText('Đã xóa tất cả prompt trong bảng')

    def _bulk_prompt_lines(self) -> list[str]:
        return [line.strip() for line in self.bulk_prompts_text.toPlainText().splitlines() if line.strip()]

    def _apply_bulk_prompts_to_table(self) -> None:
        prompts = self._bulk_prompt_lines()
        if not prompts:
            return
        existing = [(r, e) for _p, r, e, _s in self._row_values()]
        self._rebuild_prompt_rows([(p, existing[i][0] if i < len(existing) else '', existing[i][1] if i < len(existing) else '') for i, p in enumerate(prompts)])

    def _ensure_prompt_table_before_run(self) -> None:
        has_table_prompt = any(prompt for prompt, _r, _e, _s in self._row_values())
        if not has_table_prompt and self._bulk_prompt_lines():
            self._apply_bulk_prompts_to_table()

    def _mark_submitted_rows_running(self) -> None:
        for row, (prompt, _reference_path, _end_reference_path, _selected) in enumerate(self._row_values()):
            if prompt:
                self._set_row_result(row, 'Đang chạy', '#f59e0b')

    def _import_excel_prompts(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, 'Chọn file Excel prompt', '', 'Excel files (*.xlsx);;All files (*.*)')
        if not path:
            return
        try:
            values = self._read_xlsx_prompt_rows(path)
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi import Excel', str(exc)); return
        if not values:
            QMessageBox.warning(self, 'Import Excel', 'Không tìm thấy dòng prompt hợp lệ trong file Excel'); return
        self._rebuild_prompt_rows(values)
        self.status_label.setText(f'Đã import {len(values)} dòng từ Excel')

    def _read_xlsx_prompt_rows(self, path: str) -> list[tuple[str, str, str]]:
        with zipfile.ZipFile(path) as archive:
            shared_strings = self._read_xlsx_shared_strings(archive)
            sheet_xml = ET.fromstring(archive.read(self._first_xlsx_sheet_name(archive)))
        ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        table = []
        for row in sheet_xml.findall('.//x:sheetData/x:row', ns):
            cells = {}
            for cell in row.findall('x:c', ns):
                cells[self._xlsx_column_index(cell.attrib.get('r', ''))] = self._xlsx_cell_text(cell, shared_strings, ns)
            if cells:
                table.append([cells.get(i, '') for i in range(max(cells) + 1)])
        if not table:
            return []
        headers = [v.strip().lower() for v in table[0]]
        prompt_index = headers.index('prompt') if 'prompt' in headers else 0
        image_index = headers.index('image') if 'image' in headers else 1
        values = []
        for row in table[1:]:
            prompt = row[prompt_index].strip() if prompt_index < len(row) else ''
            image_value = row[image_index].strip() if image_index < len(row) else ''
            parts = [part.strip() for part in image_value.split(';') if part.strip()]
            if prompt:
                values.append((prompt, parts[0] if parts else '', parts[1] if len(parts) > 1 else ''))
        return values

    def _read_xlsx_shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        except KeyError:
            return []
        ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        return [''.join(node.text or '' for node in item.findall('.//x:t', ns)) for item in root.findall('x:si', ns)]

    def _first_xlsx_sheet_name(self, archive: zipfile.ZipFile) -> str:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        first_sheet = workbook.find('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets/{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet')
        if first_sheet is None:
            raise ValueError('File Excel không có sheet')
        rel_id = first_sheet.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        for rel in rels.findall('{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'):
            if rel.attrib.get('Id') == rel_id:
                return 'xl/' + rel.attrib.get('Target', 'worksheets/sheet1.xml').lstrip('/')
        return 'xl/worksheets/sheet1.xml'

    def _xlsx_column_index(self, cell_ref: str) -> int:
        letters = ''.join(c for c in cell_ref if c.isalpha()).upper(); index = 0
        for char in letters:
            index = index * 26 + (ord(char) - ord('A') + 1)
        return max(0, index - 1)

    def _xlsx_cell_text(self, cell, shared_strings: list[str], ns: dict[str, str]) -> str:
        cell_type = cell.attrib.get('t')
        if cell_type == 'inlineStr':
            return ''.join(node.text or '' for node in cell.findall('.//x:t', ns))
        value_node = cell.find('x:v', ns)
        if value_node is None or value_node.text is None:
            return ''
        if cell_type == 's':
            try:
                return shared_strings[int(value_node.text)]
            except Exception:
                return ''
        return value_node.text

    def _normalize_image_path_input(self, raw_path: str | None) -> str:
        path_text = (raw_path or '').strip().strip('"').strip("'")
        if not path_text:
            return ''
        path_text = os.path.expandvars(os.path.expanduser(path_text.replace('\\', '/')))
        candidate = Path(path_text)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)

        relative_text = path_text.lstrip('/')
        search_roots = [EXE_DIR, APP_DIR, Path.cwd()]
        for root in search_roots:
            candidate = root / relative_text
            if candidate.exists():
                return str(candidate)

        if Path(path_text).is_absolute():
            return str(Path(path_text))
        return str(EXE_DIR / relative_text)

    def _parse_prompt_rows(self) -> list[tuple[str, str | None, str | None]]:
        rows = []
        for prompt, reference_path, end_reference_path, _selected in self._row_values():
            if prompt:
                rows.append((
                    prompt,
                    self._normalize_image_path_input(reference_path) or None,
                    self._normalize_image_path_input(end_reference_path) or None,
                ))
        if rows:
            return rows
        return [(prompt, None, None) for prompt in self._bulk_prompt_lines()]

    def _source_prompt_count_for_preview(self) -> int:
        bulk_prompts = self._bulk_prompt_lines()
        if bulk_prompts:
            return len({self._prompt_for_generation(prompt) for prompt in bulk_prompts})
        prompts = []
        seen = set()
        for prompt, _reference_path, _end_reference_path, _selected in self._row_values():
            if not prompt:
                continue
            clean_prompt = self._prompt_for_generation(prompt)
            if clean_prompt in seen:
                continue
            seen.add(clean_prompt)
            prompts.append(clean_prompt)
        return len(prompts)

    def _update_thread_preview_ui(self) -> None:
        if not hasattr(self, 'thread_preview_label'):
            return
        prompt_count = self._source_prompt_count_for_preview()
        videos_per_prompt = max(1, min(4, self.videos_per_prompt_spin.value()))
        total_jobs = prompt_count * videos_per_prompt
        requested_threads = self.thread_spin.value()
        actual_threads = min(requested_threads, total_jobs) if total_jobs else 0
        safe_hint = max(1, requested_threads // videos_per_prompt) if total_jobs else 0
        token_count = len(self._api_tokens()) if hasattr(self, 'tokens_text') else 0
        proxy_count = len(self._api_proxies()) if hasattr(self, 'proxies_text') else 0
        max_token_threads = max(1, token_count * 10) if token_count else 0
        self.thread_preview_label.setText(
            f'Dự kiến: {prompt_count} prompt x {videos_per_prompt} = {total_jobs} job | '
            f'Token: {token_count} | Proxy: {proxy_count} | Luồng đặt: {requested_threads} | '
            f'Tối đa theo token: {max_token_threads} | Luồng thực tối đa: {actual_threads} | '
            f'Gợi ý an toàn: {safe_hint}'
        )

    def _expand_prompt_rows_for_copies(self, videos_per_prompt: int) -> None:
        videos_per_prompt = max(1, min(4, videos_per_prompt))
        table_rows = [(p, r, e) for p, r, e, _s in self._row_values() if p]
        reference_by_prompt = {}
        for prompt, reference_path, end_reference_path in table_rows:
            clean_prompt = self._prompt_for_generation(prompt)
            reference_by_prompt.setdefault(clean_prompt, (reference_path, end_reference_path))

        bulk_prompts = self._bulk_prompt_lines()
        original_rows = []
        seen = set()
        if bulk_prompts:
            for prompt in bulk_prompts:
                clean_prompt = self._prompt_for_generation(prompt)
                if clean_prompt in seen:
                    continue
                seen.add(clean_prompt)
                reference_path, end_reference_path = reference_by_prompt.get(clean_prompt, ('', ''))
                original_rows.append((clean_prompt, reference_path, end_reference_path))
        else:
            for prompt, reference_path, end_reference_path in table_rows:
                clean_prompt = self._prompt_for_generation(prompt)
                key = (clean_prompt, reference_path, end_reference_path)
                if key in seen:
                    continue
                seen.add(key)
                original_rows.append((clean_prompt, reference_path, end_reference_path))

        if not original_rows:
            return
        expanded_rows = []
        for prompt, reference_path, end_reference_path in original_rows:
            for copy_index in range(1, videos_per_prompt + 1):
                display_prompt = prompt if videos_per_prompt == 1 else f'{prompt} [video {copy_index}/{videos_per_prompt}]'
                expanded_rows.append((display_prompt, reference_path, end_reference_path))
        self._rebuild_prompt_rows(expanded_rows)

    def _prompt_for_generation(self, prompt: str) -> str:
        marker = ' [video '
        if marker in prompt and prompt.endswith(']'):
            base, suffix = prompt.rsplit(marker, 1)
            numbers = suffix[:-1].split('/', 1)
            if len(numbers) == 2 and all(part.isdigit() for part in numbers):
                return base
        return prompt

    def _build_jobs(self) -> tuple[list[str], list[BananaJob], str]:
        tokens = [line.strip() for line in self.tokens_text.toPlainText().splitlines() if line.strip()]
        if not tokens:
            raise ValueError('Chưa có token trong tab Cài đặt')
        prompt_rows = self._parse_prompt_rows()
        if not prompt_rows:
            raise ValueError('Chưa có prompt')
        mode = self.mode_combo.currentText() or 'image'
        video_type = self._video_type_value()
        if mode == 'video' and video_type != 'text' and any(not ref for _p, ref, _e in prompt_rows):
            raise ValueError('Chế độ video này bắt buộc mỗi prompt có ảnh đầu / ảnh tham chiếu')
        save_dir = Path(self.save_dir_edit.text().strip() or (APP_DIR / 'outputs'))
        project_name = self.project_name_edit.text().strip()
        if project_name:
            save_dir = save_dir / project_name
        save_dir.mkdir(parents=True, exist_ok=True)
        jobs = []
        extension = 'jpg' if mode == 'image' else 'mp4'
        for index, (prompt, reference_path, end_reference_path) in enumerate(prompt_rows, start=1):
            clean_prompt = self._prompt_for_generation(prompt)
            output_name = f'banana_{index}.{extension}'
            job = BananaJob(mode=mode, prompt=clean_prompt, model=self.model_combo.currentText(), aspect_ratio=self.aspect_combo.currentText(), reference_path=reference_path if video_type != 'text' else None, output_path=str(save_dir / output_name), video_type=video_type, end_reference_path=end_reference_path if video_type == 'start_end' else None, image_resolution=self.image_resolution_combo.currentText())
            jobs.append(job)
            self.job_row_map[job.job_id] = index - 1
        return tokens, jobs, save_dir.as_posix()

    def _start_generation(self) -> None:
        self._save_settings()
        ok, message = self._check_api_key_activation(self.account_api_key_edit.text().strip())
        self.api_key_active = ok
        self.api_key_check_message = message
        if not ok:
            QMessageBox.critical(self, 'API key chưa active', f'{message}\n\nKhông thể chạy tool khi API key active=false.')
            return
        self.statusBar().showMessage(message, 8000)
        self._ensure_prompt_table_before_run()
        videos_per_prompt = max(1, min(4, self.videos_per_prompt_spin.value()))
        self._expand_prompt_rows_for_copies(videos_per_prompt)
        try:
            tokens, jobs, save_dir = self._build_jobs()
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', str(exc)); return
        session_id = self._next_session_id()
        project_name = self.project_name_edit.text().strip() or session_id
        mode = self.mode_combo.currentText() or 'image'
        model = self.model_combo.currentText()
        thread_count = self.thread_spin.value()
        runtime_dir = APP_DIR / '.runtime' / 'projects' / session_id
        runtime_dir.mkdir(parents=True, exist_ok=True)
        videos_per_prompt = self.videos_per_prompt_spin.value()
        self.active_sessions[session_id] = {'project_name': project_name, 'save_dir': save_dir, 'mode': mode, 'model': model, 'job_count': len(jobs)}
        self._mark_submitted_rows_running()
        self._set_running_ui(True)
        self.status_label.setText(f'Đang chạy: {project_name} ({len(jobs)} job)')
        actual_threads = min(thread_count, len(jobs)) if jobs else 0
        safe_hint = max(1, thread_count // videos_per_prompt) if jobs else 0
        self.summary_label.setText(f'Project đang chạy nền: {project_name}\nTổng job: {len(jobs)} | Mode: {mode} | Model: {model} | Luồng đặt: {thread_count} | Luồng thực tối đa: {actual_threads} | Video/prompt: {videos_per_prompt} | Gợi ý an toàn: {safe_hint}')

        def worker():
            api_key_for_refresh = self.account_api_key_edit.text().strip()

            def refresh_token_for_poll(current_token: str, project_id: Optional[str] = None):
                if not api_key_for_refresh:
                    raise RuntimeError('Missing API key for token refresh')
                session = fetch_account_session_token(
                    api_key=api_key_for_refresh,
                    current_token=current_token,
                    project_id=project_id,
                    force_refresh=True,
                )
                new_token = session.get('token') or ''
                new_project_id = session.get('project_id') or project_id or ''
                if new_token:
                    self.token_project_map[new_token] = new_project_id
                return {'token': new_token, 'project_id': new_project_id}

            scheduler = BananaScheduler(
                tokens=tokens,
                thread_count=thread_count,
                max_attempts=5,
                runtime_dir=str(runtime_dir),
                token_project_map=self.token_project_map,
                result_callback=lambda result: self.result_queue.put(('job_result', session_id, result)),
                proxies=self._api_proxies(),
                token_refresh_callback=refresh_token_for_poll,
            )
            with self.running_schedulers_lock:
                self.running_schedulers.add(scheduler)
            try:
                self.result_queue.put(('done', session_id, project_name, save_dir, scheduler.submit(jobs)))
            except Exception as exc:
                if not self.is_closing:
                    self.result_queue.put(('error', session_id, project_name, str(exc)))
            finally:
                try:
                    scheduler.shutdown()
                except Exception:
                    pass
                with self.running_schedulers_lock:
                    self.running_schedulers.discard(scheduler)
        threading.Thread(target=worker, daemon=True).start()

    def _edit_import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, 'Chọn folder chứa video')
        if not folder:
            return
        self.edit_folder_edit.setText(folder)
        video_exts = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}
        self.edit_video_paths = [
            str(path)
            for path in sorted(Path(folder).iterdir(), key=lambda item: item.name.lower())
            if path.is_file() and path.suffix.lower() in video_exts
        ]
        self.edit_selected_paths = []
        self._edit_refresh_table()
        self.edit_status_label.setText(f'Đã import {len(self.edit_video_paths)} video')

    def _edit_refresh_table(self) -> None:
        if not hasattr(self, 'edit_video_table'):
            return
        self.edit_updating_table = True
        try:
            self.edit_video_table.setRowCount(0)
            for path_text in self.edit_video_paths:
                path = Path(path_text)
                row = self.edit_video_table.rowCount()
                self.edit_video_table.insertRow(row)
                check_item = QTableWidgetItem()
                check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                check_item.setCheckState(Qt.Checked if path_text in self.edit_selected_paths else Qt.Unchecked)
                self.edit_video_table.setItem(row, 0, check_item)

                order = self.edit_selected_paths.index(path_text) + 1 if path_text in self.edit_selected_paths else ''
                order_item = QTableWidgetItem(str(order))
                order_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.edit_video_table.setItem(row, 1, order_item)

                name_item = QTableWidgetItem(path.name)
                name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.edit_video_table.setItem(row, 2, name_item)

                path_item = QTableWidgetItem(path_text)
                path_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.edit_video_table.setItem(row, 3, path_item)

                size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0
                size_item = QTableWidgetItem(f'{size_mb:.1f} MB')
                size_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.edit_video_table.setItem(row, 4, size_item)
        finally:
            self.edit_updating_table = False

    def _edit_video_item_changed(self, item: QTableWidgetItem) -> None:
        if self.edit_updating_table or item.column() != 0:
            return
        row = item.row()
        if row < 0 or row >= len(self.edit_video_paths):
            return
        path_text = self.edit_video_paths[row]
        if item.checkState() == Qt.Checked:
            if path_text not in self.edit_selected_paths:
                self.edit_selected_paths.append(path_text)
        else:
            self.edit_selected_paths = [path for path in self.edit_selected_paths if path != path_text]
        self._edit_refresh_table()
        self.edit_status_label.setText(f'Đã chọn {len(self.edit_selected_paths)} video')

    def _edit_clear_selection(self) -> None:
        self.edit_selected_paths = []
        self._edit_refresh_table()
        self.edit_status_label.setText('Đã bỏ chọn tất cả video')

    def _edit_move_selected_order(self, direction: int) -> None:
        selected_rows = sorted({index.row() for index in self.edit_video_table.selectedIndexes()}) if hasattr(self, 'edit_video_table') else []
        selected_paths = [self.edit_video_paths[row] for row in selected_rows if 0 <= row < len(self.edit_video_paths)]
        selected_in_order = [path for path in self.edit_selected_paths if path in selected_paths]
        if not selected_in_order:
            QMessageBox.information(self, 'Edit video', 'Hãy chọn dòng video đã tick trong bảng để đổi thứ tự.')
            return
        for path in selected_in_order if direction < 0 else reversed(selected_in_order):
            index = self.edit_selected_paths.index(path)
            new_index = index + direction
            if 0 <= new_index < len(self.edit_selected_paths):
                self.edit_selected_paths[index], self.edit_selected_paths[new_index] = self.edit_selected_paths[new_index], self.edit_selected_paths[index]
        self._edit_refresh_table()
        self.edit_status_label.setText('Đã cập nhật thứ tự nối video')

    def _edit_choose_output(self) -> None:
        default_dir = self.edit_folder_edit.text().strip() or self.save_dir_edit.text().strip() or str(APP_DIR / 'outputs')
        output_path, _ = QFileDialog.getSaveFileName(self, 'Chọn file output', str(Path(default_dir) / 'merged_video.mp4'), 'MP4 video (*.mp4);;All files (*.*)')
        if output_path:
            if not Path(output_path).suffix:
                output_path += '.mp4'
            self.edit_output_edit.setText(output_path)

    def _run_hidden_subprocess(self, command: list[str], **kwargs):
        if sys.platform.startswith('win'):
            kwargs.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return subprocess.run(command, **kwargs)

    def _find_ffmpeg(self) -> str | None:
        candidates = ['ffmpeg', str(APP_DIR / 'ffmpeg.exe'), str(EXE_DIR / 'ffmpeg.exe')]
        for candidate in candidates:
            try:
                self._run_hidden_subprocess([candidate, '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                return candidate
            except Exception:
                continue
        return None

    def _write_ffmpeg_concat_list(self, paths: list[str], list_path: Path) -> None:
        def escape_ffmpeg_path(path_text: str) -> str:
            return path_text.replace('\\', '/').replace("'", "'\\''")
        lines = [f"file '{escape_ffmpeg_path(path)}'" for path in paths]
        list_path.write_text('\n'.join(lines), encoding='utf-8')

    def _edit_start_concat(self) -> None:
        selected_paths = list(self.edit_selected_paths)
        output_path = self.edit_output_edit.text().strip()
        if len(selected_paths) < 2:
            QMessageBox.warning(self, 'Edit video', 'Bạn cần chọn ít nhất 2 video để nối.')
            return
        if not output_path:
            QMessageBox.warning(self, 'Edit video', 'Bạn cần chọn file output.')
            return
        ffmpeg_path = self._find_ffmpeg()
        if not ffmpeg_path:
            QMessageBox.critical(self, 'Thiếu ffmpeg', 'Không tìm thấy ffmpeg. Hãy cài ffmpeg hoặc đặt ffmpeg.exe cạnh app.')
            return
        self.edit_concat_btn.setEnabled(False)
        self.edit_status_label.setText(f'Đang nối {len(selected_paths)} video...')

        def worker():
            try:
                runtime_dir = APP_DIR / '.runtime' / 'edit'
                runtime_dir.mkdir(parents=True, exist_ok=True)
                concat_list = runtime_dir / f'concat_{int(time.time() * 1000)}.txt'
                self._write_ffmpeg_concat_list(selected_paths, concat_list)
                input_temp = runtime_dir / f'concat_input_{int(time.time() * 1000)}.mp4'
                output_temp = Path(output_path)
                output_temp.parent.mkdir(parents=True, exist_ok=True)

                concat_cmd = [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', str(concat_list), '-c', 'copy', str(input_temp)]
                result = self._run_hidden_subprocess(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    concat_cmd = [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', str(concat_list), '-c:v', 'libx264', '-c:a', 'aac', str(input_temp)]
                    result = self._run_hidden_subprocess(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    message = (result.stderr or result.stdout or 'ffmpeg concat failed').strip()
                    self.result_queue.put(('edit_concat_done', False, message[-1500:], str(output_temp)))
                    return

                result = self._run_hidden_subprocess(
                    [
                        ffmpeg_path, '-y',
                        '-fflags', '+genpts',
                        '-i', str(input_temp),
                        '-c', 'copy',
                        '-movflags', '+faststart',
                        str(output_temp),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode != 0:
                    result = self._run_hidden_subprocess(
                        [
                            ffmpeg_path, '-y',
                            '-fflags', '+genpts',
                            '-i', str(input_temp),
                            '-c:v', 'libx264',
                            '-c:a', 'aac',
                            '-movflags', '+faststart',
                            str(output_temp),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                if result.returncode != 0:
                    message = (result.stderr or result.stdout or 'ffmpeg final output failed').strip()
                    self.result_queue.put(('edit_concat_done', False, message[-1500:], str(output_temp)))
                    return
                self.result_queue.put(('edit_concat_done', True, f'Đã nối {len(selected_paths)} video thành công.', str(output_temp)))
            except Exception as exc:
                self.result_queue.put(('edit_concat_done', False, str(exc), output_path))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_edit_concat_done(self, ok: bool, message: str, output_path: str) -> None:
        self.edit_concat_btn.setEnabled(True)
        if ok:
            self.edit_status_label.setText(message)
            self.statusBar().showMessage(message, 10000)
            QMessageBox.information(self, 'Edit video', f'{message}\n\nOutput:\n{output_path}')
            return
        self.edit_status_label.setText('Nối video thất bại')
        QMessageBox.critical(self, 'Lỗi nối video', message)

    def _next_session_id(self) -> str:
        self.session_counter += 1
        raw_name = self.project_name_edit.text().strip() or f'project_{self.session_counter}'
        safe_name = ''.join(c if c.isalnum() or c in {'-', '_'} else '_' for c in raw_name).strip('_') or f'project_{self.session_counter}'
        return f'{safe_name}_{self.session_counter}'

    def _clear_create_form(self) -> None:
        self.project_name_edit.clear(); self.bulk_prompts_text.clear(); self._rebuild_prompt_rows([]); self._clear_results()
        self._set_running_ui(False)
        running_count = len(self.active_sessions)
        self.status_label.setText(f'Đã clear UI. Project nền vẫn tiếp tục chạy: {running_count}' if running_count else 'Sẵn sàng')
        self.summary_label.setText(f'Đã clear UI. Project nền vẫn tiếp tục chạy: {running_count}' if running_count else 'Chưa có kết quả')

    def _clear_results(self) -> None:
        for row in range(self.prompt_table.rowCount()):
            self._set_row_result(row, 'Đợi chạy', '#64748b')
            self._set_row_actions(row, None, None)

    def _set_row_result(self, row: int, text: str, color: str) -> None:
        item = self.prompt_table.item(row, 4)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.prompt_table.setItem(row, 4, item)
        item.setText(text)
        item.setForeground(QColor(color))

    def _set_row_actions(self, row: int, download_url: str | None, saved_path: str | None, failed: bool = False) -> None:
        link_btn = QPushButton('Copy link')
        link_btn.setEnabled(bool(download_url))
        if download_url:
            link_btn.setStyleSheet('background:#16a34a; color:white; font-weight:800; border-radius:8px; padding:6px 10px;')
            link_btn.clicked.connect(lambda _=False, u=download_url: QApplication.clipboard().setText(u))
        elif failed:
            link_btn.setStyleSheet('background:#fee2e2; color:#b91c1c; font-weight:800; border-radius:8px; padding:6px 10px;')
        else:
            link_btn.setStyleSheet('background:#e2e8f0; color:#64748b; font-weight:800; border-radius:8px; padding:6px 10px;')
        self.prompt_table.setCellWidget(row, 5, link_btn)

        folder_btn = QPushButton('Mở thư mục')
        folder_btn.setEnabled(bool(saved_path))
        if saved_path:
            folder_btn.setStyleSheet('background:#16a34a; color:white; font-weight:800; border-radius:8px; padding:6px 10px;')
            folder_btn.clicked.connect(lambda _=False, p=saved_path: self._open_path(str(Path(p).parent)))
        elif failed:
            folder_btn.setStyleSheet('background:#fee2e2; color:#b91c1c; font-weight:800; border-radius:8px; padding:6px 10px;')
        else:
            folder_btn.setStyleSheet('background:#e2e8f0; color:#64748b; font-weight:800; border-radius:8px; padding:6px 10px;')
        self.prompt_table.setCellWidget(row, 6, folder_btn)

    def _set_running_ui(self, is_running: bool) -> None:
        if is_running:
            self.start_button.setText('Đang chạy...')
            self.start_button.setEnabled(False)
            self.start_button.setStyleSheet('background:#f59e0b; color:white; font-weight:900; border-radius:10px; padding:9px 16px;')
        else:
            self.start_button.setText('Tạo')
            self.start_button.setEnabled(True)
            self.start_button.setStyleSheet('')

    def _poll_result_queue(self) -> None:
        try:
            while True:
                item = self.result_queue.get_nowait(); kind = item[0]
                if kind == 'done':
                    _k, session_id, project_name, save_dir, results = item; self._handle_done(session_id, project_name, save_dir, results)
                elif kind == 'job_result':
                    _k, session_id, result = item; self._handle_job_result(session_id, result)
                elif kind == 'error':
                    _k, session_id, project_name, message = item; self._handle_error(session_id, project_name, message)
                elif kind == 'token_check_done':
                    _k, results = item; self._show_token_check_popup(results)
                elif kind == 'api_key_check_done':
                    if len(item) >= 5:
                        _k, ok, message, api_key, check_generation = item; self._handle_api_key_check_done(ok, message, api_key, check_generation)
                    else:
                        _k, ok, message = item; self._handle_api_key_check_done(ok, message)
                elif kind == 'api_key_periodic_done':
                    _k, ok, message, api_key = item; self._handle_api_key_periodic_done(ok, message, api_key)
                elif kind == 'api_log':
                    _k, message = item; self._append_api_log(message)
                elif kind == 'edit_concat_done':
                    _k, ok, message, output_path = item; self._handle_edit_concat_done(ok, message, output_path)
        except queue.Empty:
            pass

    def _handle_job_result(self, session_id: str, result) -> None:
        session = self.active_sessions.get(session_id)
        if not session:
            return
        row = self.job_row_map.get(result.job_id)
        if row is None:
            return
        failed = result.status != 'completed'
        if result.status == 'completed':
            self._set_row_result(row, 'OK', '#15803d')
        else:
            self._set_row_result(row, result.error or 'FAIL', '#dc2626')
        self._set_row_actions(row, result.download_url, result.saved_path, failed=failed)
        self._append_run_result_log(session.get('project_name', ''), session.get('save_dir', ''), result, row + 1)

    def _handle_done(self, session_id: str, project_name: str, save_dir: str, results) -> None:
        self.active_sessions.pop(session_id, None)
        completed = sum(1 for r in results if r.status == 'completed')
        failed = sum(1 for r in results if r.status != 'completed')
        if not self.active_sessions:
            self._set_running_ui(False)
        self.status_label.setText(f'Hoàn tất {project_name}: thành công={completed}, thất bại={failed}; còn chạy nền={len(self.active_sessions)}')
        self.summary_label.setText(f'Project hoàn tất: {project_name}\nTổng job: {len(results)} | Thành công: {completed} | Thất bại: {failed}')
        for result in results:
            row = self.job_row_map.get(result.job_id)
            if row is None:
                row = self.prompt_table.rowCount()
                self._add_prompt_row(result.prompt)
            failed = result.status != 'completed'
            if result.status == 'completed':
                self._set_row_result(row, 'OK', '#15803d')
            else:
                self._set_row_result(row, result.error or 'FAIL', '#dc2626')
            self._set_row_actions(row, result.download_url, result.saved_path, failed=failed)
        self.statusBar().showMessage(f'Hoàn tất project: {project_name}', 10000)

    def _result_log_record(self, project_name: str, result, index: int) -> dict:
        return {
            'index': index,
            'project_name': project_name,
            'job_id': result.job_id,
            'prompt': result.prompt,
            'status': result.status,
            'lane_id': result.lane_id,
            'token_fingerprint': result.token_fingerprint,
            'attempts': result.attempts,
            'download_url': result.download_url,
            'saved_path': result.saved_path,
            'error': result.error,
            'duration_seconds': result.duration_seconds,
        }

    def _job_result_api_record(self, project_name: str, result) -> dict:
        return {
            'project_name': project_name,
            'job_id': result.job_id,
            'prompt': result.prompt,
            'status': result.status,
            'download_url': result.download_url,
            'duration_seconds': result.duration_seconds,
            'error': result.error,
        }

    def _post_job_results_api(self, records: list[dict]) -> None:
        if not records:
            return
        api_key = self.account_api_key_edit.text().strip()
        if not api_key:
            return
        payload = json.dumps(records, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            JOB_RESULTS_API_URL,
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'X-API-Key': api_key,
                'User-Agent': 'tool-veo3-mau/1.0',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response_body = response.read()

        except urllib.error.HTTPError as exc:
            error_body = exc.read()

        except Exception as exc:
            pass 

    def _append_run_result_log(self, project_name: str, save_dir: str, result, index: int) -> str:
        if result.status == 'completed':
            self._post_job_results_api([self._job_result_api_record(project_name, result)])
        return JOB_RESULTS_API_URL

    def _write_run_results_log(self, project_name: str, save_dir: str, results) -> str:
        self._post_job_results_api([self._job_result_api_record(project_name, result) for result in results if result.status == 'completed'])
        return JOB_RESULTS_API_URL

    def _handle_error(self, session_id: str, project_name: str, message: str) -> None:
        self.active_sessions.pop(session_id, None)
        if not self.active_sessions:
            self._set_running_ui(False)
        self.status_label.setText(f'Lỗi project {project_name}; còn chạy nền={len(self.active_sessions)}')
        self.summary_label.setText(f'Project lỗi: {project_name}\n{message}')
        QMessageBox.critical(self, 'Lỗi', f'{project_name}: {message}')

    def _add_result_card(self, result) -> None:
        card = self._card(); lay = QHBoxLayout(card); lay.setContentsMargins(14, 14, 14, 14)
        preview = QLabel('VIDEO' if (result.saved_path or '').lower().endswith('.mp4') else 'NO PREVIEW')
        preview.setFixedSize(CARD_PREVIEW_WIDTH, CARD_PREVIEW_HEIGHT); preview.setAlignment(Qt.AlignCenter); preview.setStyleSheet('background:#eef2f7; border:1px solid #d6dbe4; border-radius:8px; color:#64748b; font-weight:800;')
        if result.saved_path and result.saved_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
            pix = QPixmap(result.saved_path)
            if not pix.isNull():
                preview.setPixmap(pix.scaled(CARD_PREVIEW_WIDTH - 10, CARD_PREVIEW_HEIGHT - 10, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(preview)
        info = QVBoxLayout(); status = QLabel(result.status.upper()); status.setStyleSheet('font-weight:900; color:#15803d;' if result.status == 'completed' else 'font-weight:900; color:#dc2626;')
        info.addWidget(status)
        info.addWidget(QLabel(f'{result.lane_id} | token {result.token_fingerprint} | attempts {result.attempts} | {result.duration_seconds or 0:.2f}s'))
        prompt = QLabel(result.prompt); prompt.setWordWrap(True); info.addWidget(prompt)
        details = []
        if result.saved_path: details.append(f'File: {result.saved_path}')
        if result.download_url: details.append(f'URL: {result.download_url}')
        if result.error: details.append(f'Error: {result.error}')
        detail = QLabel('\n'.join(details) if details else 'Không có chi tiết bổ sung'); detail.setWordWrap(True); detail.setStyleSheet('color:#64748b; font-family:Consolas;')
        info.addWidget(detail)
        btns = QHBoxLayout()
        if result.saved_path:
            open_btn = QPushButton('Mở file'); open_btn.clicked.connect(lambda _=False, p=result.saved_path: self._open_path(p)); btns.addWidget(open_btn)
            folder_btn = QPushButton('Mở thư mục'); folder_btn.clicked.connect(lambda _=False, p=result.saved_path: self._open_path(str(Path(p).parent))); btns.addWidget(folder_btn)
        if result.download_url:
            copy_btn = QPushButton('Copy link'); copy_btn.clicked.connect(lambda _=False, u=result.download_url: QApplication.clipboard().setText(u)); btns.addWidget(copy_btn)
        btns.addStretch(); info.addLayout(btns); lay.addLayout(info, 1)
        self.results_layout.insertWidget(max(0, self.results_layout.count() - 1), card)

    def _open_path(self, path: str) -> None:
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(['explorer', path])

    def closeEvent(self, event) -> None:
        self.is_closing = True
        try:
            self._save_settings()
        except Exception as exc:
            logging.warning('stage=cleanup action=save_settings failed=%s', exc)
        self._stop_api_server()
        self._shutdown_running_schedulers()
        self._kill_app_chrome_on_exit()
        event.accept()

    def _shutdown_running_schedulers(self) -> None:
        with self.running_schedulers_lock:
            schedulers = list(self.running_schedulers)
        for scheduler in schedulers:
            try:
                scheduler.shutdown()
            except Exception as exc:
                logging.warning('stage=cleanup action=shutdown_scheduler failed=%s', exc)
        deadline = threading.Event()
        deadline.wait(0.5)

    def _kill_app_chrome_on_exit(self) -> None:
        runtime_root = (APP_DIR / '.runtime').resolve()
        try:
            command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', "$runtimeRoot = $args[0].Replace('/', '\\'); Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | Where-Object { $_.CommandLine -and $_.CommandLine.Replace('/', '\\') -like \"*--user-data-dir*\" -and $_.CommandLine.Replace('/', '\\') -like \"*$runtimeRoot*\" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }", str(runtime_root)]
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as exc:
            logging.warning('stage=cleanup action=kill_app_chrome failed=%s', exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    app = QApplication([])
    window = BananaGuiApp()
    window.show()
    app.exec()


if __name__ == '__main__':
    main()
