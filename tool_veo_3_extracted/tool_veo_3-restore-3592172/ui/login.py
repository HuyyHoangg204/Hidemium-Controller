from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QFrame,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor
import json
import os
from core.auth import verify_license, mark_license_verified


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AutoVoice - Kích Hoạt Phần Mềm")
        self.setFixedSize(450, 260)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Load saved key
        self.settings_file = "settings.json"

        self.setup_ui()
        self.load_saved_key()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        frame = QFrame()
        frame.setObjectName("MainFrame")
        frame.setStyleSheet(
            """
            #MainFrame {
                background-color: #0f111a;
                border-radius: 12px;
                border: 1px solid #1e2235;
            }
        """
        )

        # Add shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 0)
        frame.setGraphicsEffect(shadow)

        layout = QVBoxLayout(frame)
        layout.setSpacing(15)
        layout.setContentsMargins(30, 30, 30, 30)

        title = QLabel("XÁC THỰC BẢN QUYỀN")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "color: #e2e8f0; font-size: 16px; font-weight: bold; letter-spacing: 1px;"
        )
        layout.addWidget(title)

        desc = QLabel("Nhập mã kích hoạt của bạn để sử dụng phần mềm:")
        desc.setStyleSheet("color: #94a3b8; font-size: 11px;")
        desc.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc)

        self.key_input = QLineEdit()
        self.key_input.setFixedHeight(50)
        self.key_input.setPlaceholderText("XXXX-XXXX-XXXX-XXXX")
        self.key_input.setAlignment(Qt.AlignCenter)
        self.key_input.setStyleSheet(
            """
            QLineEdit {
                background: #1e2235;
                color: #f8fafc;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 10px 15px;
                font-size: 16px;
                font-family: 'Consolas', monospace;
                letter-spacing: 2px;
            }
            QLineEdit:focus {
                border: 1px solid #3b82f6;
            }
        """
        )
        layout.addWidget(self.key_input)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.btn_exit = QPushButton("Thoát")
        self.btn_exit.setCursor(Qt.PointingHandCursor)
        self.btn_exit.setFixedHeight(35)
        self.btn_exit.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: rgba(255,255,255,0.05);
                color: #f8fafc;
            }
        """
        )
        self.btn_exit.clicked.connect(self.reject)

        self.btn_login = QPushButton("Đăng Nhập")
        self.btn_login.setCursor(Qt.PointingHandCursor)
        self.btn_login.setFixedHeight(35)
        self.btn_login.setStyleSheet(
            """
            QPushButton {
                background: #3b82f6;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #2563eb;
            }
        """
        )
        self.btn_login.clicked.connect(self.verify)

        btn_layout.addWidget(self.btn_exit)
        btn_layout.addWidget(self.btn_login)
        layout.addLayout(btn_layout)

        main_layout.addWidget(frame)

    def load_saved_key(self):
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, "r") as f:
                    data = json.load(f)
                    saved_key = data.get("license_key", "")
                    if saved_key:
                        self.key_input.setText(saved_key)
        except Exception:
            pass

    def save_key(self, key):
        try:
            data = {}
            if os.path.exists(self.settings_file):
                with open(self.settings_file, "r") as f:
                    data = json.load(f)
            data["license_key"] = key
            with open(self.settings_file, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def verify(self):
        key = self.key_input.text().strip()
        self.btn_login.setEnabled(False)
        self.btn_login.setText("Đang kiểm tra...")
        self.repaint()  # update UI immediately

        success, msg = verify_license(key)

        if success:
            self.save_key(key)
            mark_license_verified(key)
            self.accept()
        else:
            QMessageBox.critical(self, "Lỗi Kích Hoạt", msg)
            self.btn_login.setEnabled(True)
            self.btn_login.setText("Đăng Nhập")
