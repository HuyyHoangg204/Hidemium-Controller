"""
Faceless Video Tab - Giao diện PySide6 cho Auto Faceless Video Pipeline.
Chạy riêng biệt trong 1 tab, không ảnh hưởng gì đến màn hình chính.
"""

import os
import json
import threading

SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json"
)

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QTextEdit,
    QProgressBar,
    QFrame,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QMessageBox,
    QGroupBox,
    QComboBox,
    QCheckBox,
    QSpinBox,
)


class _Signals(QObject):
    """Thread-safe signals để cập nhật UI từ background thread."""

    log = Signal(str)
    progress = Signal(int, int)  # step, total_steps
    finished = Signal(str)  # output path
    error = Signal(str)
    srt_ready = Signal(str)  # srt path - yêu cầu user review
    prompts_ready = Signal(list, list)  # prompts, scenes - yêu cầu user duyệt prompt


class FacelessTab(QWidget):
    """Tab giao diện cho Auto Faceless Video."""

    def __init__(self, session_token_getter=None, parent=None):
        super().__init__(parent)
        self._session_token_getter = session_token_getter
        self._signals = _Signals()
        self._signals.log.connect(self._append_log)
        self._signals.progress.connect(self._update_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.error.connect(self._on_error)
        self._signals.srt_ready.connect(self._on_srt_ready)
        self._signals.prompts_ready.connect(self._on_prompts_ready)

        self._srt_path = None
        self._assets = None
        self._scenes = None
        self._prompts = None
        self._running = False

        self._build_ui()
        self._load_settings()

    # ---------------------------------------------------------------
    # UI BUILDING
    # ---------------------------------------------------------------

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(28, 20, 28, 16)
        main.setSpacing(14)

        # -- Header --
        hdr = QLabel("🎬  Auto Faceless Video Generator")
        hdr.setStyleSheet(
            "font-size:22px; font-weight:700; color:#e2e8f0; padding:4px 0;"
        )
        main.addWidget(hdr)

        sub = QLabel("Biến link YouTube thành video Infographic tự động bằng Veo 3")
        sub.setStyleSheet("font-size:13px; color:#94a3b8; margin-bottom:8px;")
        main.addWidget(sub)

        # -- Input Card --
        card = QFrame()
        card.setObjectName("faceless_card")
        card.setStyleSheet(
            """
            #faceless_card {
                background: rgba(30,41,59,0.85);
                border: 1px solid rgba(100,116,139,0.25);
                border-radius: 14px;
                padding: 18px;
            }
        """
        )
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(12)

        # YouTube URL
        url_row = QHBoxLayout()
        url_label = QLabel("🔗 YouTube URL:")
        url_label.setStyleSheet("color:#cbd5e1; font-weight:600; font-size:13px;")
        url_row.addWidget(url_label)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Dán link YouTube vào đây...")
        self.url_input.setStyleSheet(
            """
            QLineEdit {
                background: rgba(15,23,42,0.8);
                border: 1px solid rgba(100,116,139,0.3);
                border-radius: 8px;
                color: #e2e8f0;
                padding: 10px 14px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #3b82f6;
            }
        """
        )
        url_row.addWidget(self.url_input, 1)
        card_layout.addLayout(url_row)

        # Gemini API Key
        key_row = QHBoxLayout()
        key_label = QLabel("🔑 Groq API Key:")
        key_label.setStyleSheet("color:#cbd5e1; font-weight:600; font-size:13px;")
        key_row.addWidget(key_label)
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText(
            "Nhập Groq API Key (miễn phí tại console.groq.com)..."
        )
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setStyleSheet(self.url_input.styleSheet())
        key_row.addWidget(self.key_input, 1)
        card_layout.addLayout(key_row)

        # Aspect Ratio
        ar_row = QHBoxLayout()
        ar_label = QLabel("📍 Tỉ lệ video:")
        ar_label.setStyleSheet("color:#cbd5e1; font-weight:600; font-size:13px;")
        ar_row.addWidget(ar_label)
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["16:9 (Landscape)", "9:16 (Portrait)"])
        self.aspect_combo.setStyleSheet(
            """
            QComboBox {
                background: rgba(15,23,42,0.8);
                border: 1px solid rgba(100,116,139,0.3);
                border-radius: 8px;
                color: #e2e8f0;
                padding: 8px 14px;
                font-size: 13px;
                min-width: 180px;
            }
            QComboBox:focus { border-color: #3b82f6; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background: #1e293b; color: #e2e8f0;
                selection-background-color: #3b82f6;
            }
        """
        )
        ar_row.addWidget(self.aspect_combo)
        ar_row.addStretch()
        card_layout.addLayout(ar_row)

        # Test Mode
        test_row = QHBoxLayout()
        self.test_mode_cb = QCheckBox("🧪 Test Mode")
        self.test_mode_cb.setChecked(True)
        self.test_mode_cb.setStyleSheet(
            "color:#fbbf24; font-weight:600; font-size:13px;"
        )
        test_row.addWidget(self.test_mode_cb)

        test_lbl = QLabel("Số cảnh test:")
        test_lbl.setStyleSheet("color:#94a3b8; font-size:12px; margin-left:8px;")
        test_row.addWidget(test_lbl)
        self.test_limit_spin = QSpinBox()
        self.test_limit_spin.setRange(1, 50)
        self.test_limit_spin.setValue(5)
        self.test_limit_spin.setStyleSheet(
            """
            QSpinBox {
                background: rgba(15,23,42,0.8);
                border: 1px solid rgba(100,116,139,0.3);
                border-radius: 6px;
                color: #fbbf24;
                padding: 4px 10px;
                font-size: 13px;
                min-width: 60px;
            }
        """
        )
        test_row.addWidget(self.test_limit_spin)
        test_row.addStretch()
        card_layout.addLayout(test_row)

        # Buttons Row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_start = QPushButton("🚀  Bắt Đầu Pipeline")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setFixedHeight(42)
        self.btn_start.setStyleSheet(
            """
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #3b82f6,stop:1 #6366f1);
                color: white;
                font-weight: 700;
                font-size: 14px;
                border: none;
                border-radius: 10px;
                padding: 0 24px;
            }
            QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #2563eb,stop:1 #4f46e5); }
            QPushButton:disabled { background: #475569; color: #94a3b8; }
        """
        )
        self.btn_start.clicked.connect(self._on_start)
        btn_row.addWidget(self.btn_start)

        self.btn_continue = QPushButton("✅  Đã Sửa SRT - Tiếp Tục")
        self.btn_continue.setCursor(Qt.PointingHandCursor)
        self.btn_continue.setFixedHeight(42)
        self.btn_continue.setEnabled(False)
        self.btn_continue.setStyleSheet(
            """
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #10b981,stop:1 #059669);
                color: white;
                font-weight: 700;
                font-size: 14px;
                border: none;
                border-radius: 10px;
                padding: 0 24px;
            }
            QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #059669,stop:1 #047857); }
            QPushButton:disabled { background: #374151; color: #6b7280; }
        """
        )
        self.btn_continue.clicked.connect(self._on_continue)
        btn_row.addWidget(self.btn_continue)

        self.btn_open_srt = QPushButton("📝 Mở File SRT")
        self.btn_open_srt.setCursor(Qt.PointingHandCursor)
        self.btn_open_srt.setFixedHeight(42)
        self.btn_open_srt.setEnabled(False)
        self.btn_open_srt.setStyleSheet(
            """
            QPushButton {
                background: rgba(100,116,139,0.3);
                color: #e2e8f0;
                font-weight: 600;
                font-size: 13px;
                border: 1px solid rgba(100,116,139,0.3);
                border-radius: 10px;
                padding: 0 18px;
            }
            QPushButton:hover { background: rgba(100,116,139,0.5); }
            QPushButton:disabled { color: #6b7280; }
        """
        )
        self.btn_open_srt.clicked.connect(self._open_srt_file)
        btn_row.addWidget(self.btn_open_srt)

        btn_row.addStretch()
        card_layout.addLayout(btn_row)

        main.addWidget(card)

        # -- Progress --
        prog_row = QHBoxLayout()
        self.step_label = QLabel("Sẵn sàng")
        self.step_label.setStyleSheet("color:#94a3b8; font-size:12px;")
        prog_row.addWidget(self.step_label)
        prog_row.addStretch()
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setRange(0, 6)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            """
            QProgressBar {
                background: rgba(30,41,59,0.6);
                border: none;
                border-radius: 5px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #3b82f6,stop:1 #8b5cf6);
                border-radius: 5px;
            }
        """
        )
        prog_row.addWidget(self.progress_bar, 1)
        main.addLayout(prog_row)

        # -- Log Console --
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            """
            QTextEdit {
                background: rgba(15,23,42,0.9);
                border: 1px solid rgba(100,116,139,0.2);
                border-radius: 10px;
                color: #a5f3fc;
                font-family: 'Cascadia Code', 'Consolas', monospace;
                font-size: 12px;
                padding: 12px;
            }
        """
        )
        self.log_box.setPlaceholderText("Console log sẽ hiển thị ở đây...")
        main.addWidget(self.log_box, 1)

        # -- Prompt Review Area (ẩn mặc định, hiện khi Step 3 xong) --
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setVisible(False)
        self._prompt_edit.setStyleSheet(
            """
            QTextEdit {
                background: rgba(20,30,50,0.95);
                border: 2px solid #f59e0b;
                border-radius: 10px;
                color: #fef3c7;
                font-family: 'Cascadia Code', 'Consolas', monospace;
                font-size: 12px;
                padding: 12px;
            }
        """
        )
        self._prompt_edit.setPlaceholderText("Prompt sẽ hiện ở đây để bạn duyệt...")
        self._prompt_edit.setMaximumHeight(200)
        main.addWidget(self._prompt_edit)

        self.btn_approve_prompts = QPushButton("✅ Duyệt Prompt - Tiếp Tục")
        self.btn_approve_prompts.setVisible(False)
        self.btn_approve_prompts.setStyleSheet(
            """
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #f59e0b,stop:1 #d97706);
                border: none; border-radius: 8px;
                color: #1e293b; font-weight: bold; font-size: 13px;
                padding: 10px 20px;
            }
            QPushButton:hover { background: #fbbf24; }
        """
        )
        self.btn_approve_prompts.clicked.connect(self._on_approve_prompts)
        main.addWidget(self.btn_approve_prompts)

    # ---------------------------------------------------------------
    # SLOTS
    # ---------------------------------------------------------------

    def _append_log(self, msg: str):
        self.log_box.append(msg)

    def _update_progress(self, step: int, total: int):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(step)
        self.step_label.setText(f"Bước {step}/{total}")

    def _on_finished(self, path: str):
        self._running = False
        self.btn_start.setEnabled(True)
        self._append_log(f"\n🎉 HOÀN TẤT! Video đã xuất tại: {path}")
        QMessageBox.information(self, "Thành công!", f"Video đã được tạo:\n{path}")

    def _on_error(self, msg: str):
        self._running = False
        self.btn_start.setEnabled(True)
        self._append_log(f"\n❌ LỖI: {msg}")

    def _on_srt_ready(self, srt_path: str):
        """Được gọi khi file SRT đã tải xong, chờ user sửa."""
        self._srt_path = srt_path
        self.btn_continue.setEnabled(True)
        self.btn_open_srt.setEnabled(True)
        self._append_log(f"\n⏸️ ĐÃ TẠM DỪNG! File SRT: {srt_path}")
        self._append_log(
            "  → Mở file SRT, sửa chính tả, rồi bấm 'Đã Sửa SRT - Tiếp Tục'"
        )
        # Tự động mở file SRT cho user xem/sửa ngay
        try:
            if os.path.exists(srt_path):
                os.startfile(srt_path)
        except Exception:
            pass

    def _open_srt_file(self):
        if self._srt_path and os.path.exists(self._srt_path):
            os.startfile(self._srt_path)

    def _load_settings(self):
        """Tải Gemini API Key đã lưu từ settings.json."""
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                key = data.get("GROQ_API_KEY", "") or data.get("GEMINI_API_KEY", "")
                if key:
                    self.key_input.setText(key)
        except Exception:
            pass

    def _save_api_key(self, key: str):
        """Lưu Gemini API Key vào settings.json."""
        try:
            data = {}
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["GROQ_API_KEY"] = key
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    # ---------------------------------------------------------------
    # PIPELINE EXECUTION
    # ---------------------------------------------------------------

    def _on_start(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Thiếu URL", "Vui lòng dán link YouTube!")
            return

        api_key = self.key_input.text().strip()
        if not api_key:
            QMessageBox.warning(
                self,
                "Thiếu API Key",
                "Vui lòng nhập Groq API Key!\nĐăng ký miễn phí tại: console.groq.com",
            )
            return

        # Lưu API key để lần sau không cần nhập lại
        self._save_api_key(api_key)

        self._running = True
        self.btn_start.setEnabled(False)
        self.btn_continue.setEnabled(False)
        self.btn_open_srt.setEnabled(False)
        self.log_box.clear()
        self.progress_bar.setValue(0)

        # Run Step 1 in background
        self._api_key = api_key
        self._url = url
        self._aspect = (
            "landscape" if self.aspect_combo.currentIndex() == 0 else "portrait"
        )
        self._test_mode = self.test_mode_cb.isChecked()
        self._test_limit = self.test_limit_spin.value()
        t = threading.Thread(target=self._run_step1, daemon=True)
        t.start()

    def _run_step1(self):
        """Background: Tải audio + SRT, sau đó pause chờ user sửa SRT."""
        try:
            from core.auto_faceless import download_youtube_assets

            self._signals.log.emit(
                "▶ [Step 1/6] Đang tải audio và phụ đề từ YouTube..."
            )
            self._signals.progress.emit(1, 6)

            assets = download_youtube_assets(self._url, output_dir="temp")
            self._assets = assets

            if not assets.get("srt"):
                self._signals.error.emit(
                    "Không tìm thấy file phụ đề (.srt) cho video này!"
                )
                return

            self._signals.log.emit(f"  ✔ Audio: {assets['audio']}")
            self._signals.log.emit(f"  ✔ SRT: {assets['srt']}")
            self._signals.srt_ready.emit(assets["srt"])

        except Exception as e:
            self._signals.error.emit(str(e))

    def _on_continue(self):
        """User đã sửa SRT xong, tiếp tục pipeline Step 2-3."""
        self.btn_continue.setEnabled(False)
        self.btn_open_srt.setEnabled(False)

        t = threading.Thread(target=self._run_steps_2_to_3, daemon=True)
        t.start()

    def _on_prompts_ready(self, prompts, scenes):
        """Hiện prompt cho user duyệt trước khi tạo video."""
        self._scenes = scenes
        self._prompts = prompts

        # Hiện prompt trong log, mỗi prompt cách 1 dòng
        self._append_log(
            "\n⏸️ DUYỆT PROMPT! Sửa nếu cần rồi bấm 'Duyệt Prompt - Tiếp Tục'"
        )
        self._append_log("─" * 60)
        for i, p in enumerate(prompts):
            self._append_log(f"  [{i+1}] {p}\n")
        self._append_log("─" * 60)

        # Hiện prompt trong text area riêng để user sửa
        prompt_text = "\n\n".join(prompts)
        self._prompt_edit.setPlainText(prompt_text)
        self._prompt_edit.setVisible(True)
        self.btn_approve_prompts.setVisible(True)
        self.btn_approve_prompts.setEnabled(True)

    def _on_approve_prompts(self):
        """User duyệt prompt xong, tiếp tục Step 4-6."""
        # Đọc prompt đã sửa từ text area
        edited = self._prompt_edit.toPlainText().strip()
        self._prompts = [p.strip() for p in edited.split("\n\n") if p.strip()]

        self._prompt_edit.setVisible(False)
        self.btn_approve_prompts.setVisible(False)

        self._append_log(
            f"\n✔ Đã duyệt {len(self._prompts)} prompt. Tiếp tục tạo video..."
        )

        t = threading.Thread(target=self._run_steps_4_to_6, daemon=True)
        t.start()

    def _run_steps_2_to_3(self):
        """Background: Step 2 (parse SRT) + Step 3 (generate prompts) → pause cho user duyệt."""
        try:
            from core.auto_faceless import (
                parse_srt,
                chunk_scenes,
                generate_prompts_groq,
            )

            # -- Step 2 --
            self._signals.log.emit("\n▶ [Step 2/6] Đang phân tích SRT và gom cảnh...")
            self._signals.progress.emit(2, 6)
            entries = parse_srt(self._assets["srt"])
            scenes = chunk_scenes(entries, target_duration=8.0)
            total_scenes = len(scenes)
            self._signals.log.emit(
                f"  ✔ Gom được {total_scenes} cảnh từ {len(entries)} dòng sub."
            )

            # Test Mode: giới hạn số cảnh
            if self._test_mode and total_scenes > self._test_limit:
                scenes = scenes[: self._test_limit]
                self._signals.log.emit(
                    f"  🧪 TEST MODE: Chỉ xử lý {len(scenes)}/{total_scenes} cảnh đầu tiên"
                )

            for i, s in enumerate(scenes):
                self._signals.log.emit(
                    f"    Scene {i+1}: [{s['duration']:.1f}s] {s['text'][:60]}..."
                )

            # -- Step 3 --
            self._signals.log.emit(f"\n▶ [Step 3/6] Đang tạo prompt bằng Groq AI...")
            self._signals.progress.emit(3, 6)
            prompts = generate_prompts_groq(scenes, self._api_key)
            self._signals.log.emit(f"  ✔ Đã tạo {len(prompts)} prompt.")

            # Pause: emit signal để user duyệt prompt
            self._signals.prompts_ready.emit(prompts, scenes)

        except Exception as e:
            self._signals.error.emit(str(e))

    def _run_steps_4_to_6(self):
        """Background: Step 4 (Veo) + Step 5-6 (render) — chạy sau khi user duyệt prompt."""
        try:
            from core.auto_faceless import (
                generate_veo_clips,
                render_final_video,
            )

            prompts = self._prompts
            scenes = self._scenes

            # -- Step 4 --
            self._signals.log.emit(f"\n▶ [Step 4/6] Đang tạo video clip bằng Veo 3...")
            self._signals.progress.emit(4, 6)
            session_token = None
            if self._session_token_getter:
                session_token = self._session_token_getter()
            clip_paths = generate_veo_clips(
                prompts,
                scenes,
                "temp",
                session_token,
                aspect=self._aspect,
            )
            ok = sum(1 for p in clip_paths if p)
            self._signals.log.emit(f"  ✔ Tạo xong {ok}/{len(clip_paths)} clip.")

            # -- Step 5 & 6 --
            self._signals.log.emit(f"\n▶ [Step 5-6/6] Đang render video cuối cùng...")
            self._signals.progress.emit(5, 6)

            output = render_final_video(
                scenes,
                clip_paths,
                self._assets["audio"],
                self._assets["srt"],
                output_path="temp/output_faceless.mp4",
            )

            self._signals.progress.emit(6, 6)
            if output:
                self._signals.finished.emit(output)
            else:
                self._signals.error.emit("Render thất bại!")

        except Exception as e:
            self._signals.error.emit(str(e))
