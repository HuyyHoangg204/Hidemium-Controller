from __future__ import annotations

import datetime
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from app_logging import LOG_FILE, setup_logging
from hidemium_client import HidemiumClient, HidemiumError


APP_BG = "#08111f"
PANEL_BG = "#101b2f"
PANEL_2 = "#14243d"
TEXT = "#eaf2ff"
MUTED = "#8ea2c6"
ACCENT = "#6ee7f9"
ACCENT_2 = "#a78bfa"
SUCCESS = "#34d399"
DANGER = "#fb7185"
WARNING = "#fbbf24"


class TkTextLogHandler(logging.Handler):
    def __init__(self, output_queue: queue.Queue[tuple[str, Any]]) -> None:
        super().__init__(logging.INFO)
        self.output_queue = output_queue
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.output_queue.put(("log", self.format(record), None))


class HidemiumGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Hidemium Profile Controller")
        self.geometry("1180x720")
        self.minsize(980, 580)
        self.configure(bg=APP_BG)

        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.logger = setup_logging("hidemium_controller.gui", console=True)
        self.logger.addHandler(TkTextLogHandler(self.queue))
        self.logger.info("GUI starting")
        self.profiles: list[dict[str, Any]] = []
        self.selected_uuid = ""

        self.base_url = tk.StringVar(value=os.getenv("HIDEMIUM_BASE_URL", "http://127.0.0.1:2222"))
        self.is_local = tk.BooleanVar(value=os.getenv("HIDEMIUM_IS_LOCAL", "false").lower() in {"1", "true", "yes", "y"})
        self.search = tk.StringVar(value="")
        self.limit = tk.IntVar(value=100)
        self.command = tk.StringVar(value="--window-position=100,100 --window-size=1280,800")
        self.proxy = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="Sẵn sàng. Mở Hidemium 4 rồi bấm Tải danh sách.")

        # Folder filter: name -> list of folder IDs
        self.FOLDER_MAP: dict[str, list[int]] = {
            "Tất cả folder": [],
            "veo3 ultra đang chạy (23)": [3024],
            "veo3 ultra 30k (15)": [3053],
            "2 folder veo3": [3053, 3024],
        }
        self.folder_var = tk.StringVar(value="veo3 ultra đang chạy (23)")

        # --- Scheduler state ---
        self.sched_enabled    = tk.BooleanVar(value=False)
        self.sched_time       = tk.StringVar(value="08:00")   # HH:MM (mode cố định)
        self.sched_interval   = tk.IntVar(value=60)            # phút (mode lặp lại)
        self.sched_mode       = tk.StringVar(value="interval") # "fixed" | "interval"
        self.sched_delay      = tk.IntVar(value=3)             # giây giữa mỗi lần mở
        self.sched_batch      = tk.IntVar(value=10)            # số profile mỗi batch
        self.sched_wait       = tk.IntVar(value=20)            # giây đợi sau khi mở xong batch
        self.sched_status     = tk.StringVar(value="Hẹn giờ chưa bật")
        self._sched_fired_today: str = ""                      # ngày đã fire (mode fixed)
        self._sched_next_run: datetime.datetime | None = None  # lần chạy tiếp (mode interval)
        self._sched_running   = False

        self._setup_style()
        self._build_ui()
        self.after(100, self._process_queue)
        self.after(300, self.refresh_profiles)
        self.after(1000, self._scheduler_tick)

    def _client(self) -> HidemiumClient:
        return HidemiumClient(self.base_url.get().strip() or "http://127.0.0.1:2222", timeout=30, logger=self.logger)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL_BG, foreground=TEXT, fieldbackground=PANEL_BG, rowheight=34, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL_2, foreground=ACCENT, font=("Segoe UI Semibold", 10), relief="flat")
        style.map("Treeview", background=[("selected", "#233b66")], foreground=[("selected", "#ffffff")])
        style.configure("TCheckbutton", background=APP_BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL_2, bordercolor=PANEL_2, lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=APP_BG)
        header.pack(fill="x", padx=20, pady=(18, 10))

        title_box = tk.Frame(header, bg=APP_BG)
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text="Hidemium Controller", bg=APP_BG, fg=TEXT, font=("Segoe UI Semibold", 24)).pack(anchor="w")
        tk.Label(title_box, text="Bảng quản lý profile: xem danh sách, mở/đóng profile tùy ý", bg=APP_BG, fg=MUTED, font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 0))

        self.pill = tk.Label(header, text="API: chưa kiểm tra", bg="#1e293b", fg=WARNING, padx=14, pady=7, font=("Segoe UI Semibold", 10))
        self.pill.pack(side="right")

        toolbar = tk.Frame(self, bg=PANEL_BG, highlightbackground="#223456", highlightthickness=1)
        toolbar.pack(fill="x", padx=20, pady=(0, 12))

        self._labeled_entry(toolbar, "Base URL", self.base_url, 22).pack(side="left", padx=(14, 8), pady=12)
        self._labeled_entry(toolbar, "Tìm kiếm", self.search, 16).pack(side="left", padx=8, pady=12)
        self._labeled_entry(toolbar, "Limit", self.limit, 6).pack(side="left", padx=8, pady=12)

        # Folder filter dropdown
        folder_frame = tk.Frame(toolbar, bg=PANEL_BG)
        folder_frame.pack(side="left", padx=8, pady=12)
        tk.Label(folder_frame, text="Folder", bg=PANEL_BG, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w")
        folder_cb = ttk.Combobox(folder_frame, textvariable=self.folder_var,
                                  values=list(self.FOLDER_MAP.keys()),
                                  state="readonly", width=22, font=("Segoe UI", 10))
        folder_cb.pack(ipady=5)

        ttk.Checkbutton(toolbar, text="Profile local", variable=self.is_local).pack(side="left", padx=8)

        self._button(toolbar, "Tải danh sách", self.refresh_profiles, ACCENT, "#062f3a").pack(side="left", padx=8)
        self._button(toolbar, "Check API", self.check_api, ACCENT_2, "#2b214a").pack(side="left", padx=(0, 14))

        body = tk.Frame(self, bg=APP_BG)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        table_card = tk.Frame(body, bg=PANEL_BG, highlightbackground="#223456", highlightthickness=1)
        table_card.pack(side="left", fill="both", expand=True)

        columns = ("name", "uuid", "status", "proxy", "folder", "note")
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")
        headings = {
            "name": "Tên profile",
            "uuid": "UUID",
            "status": "Trạng thái",
            "proxy": "Proxy",
            "folder": "Folder",
            "note": "Ghi chú",
        }
        widths = {"name": 210, "uuid": 285, "status": 110, "proxy": 190, "folder": 130, "note": 220}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=80, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _event: self.open_selected())

        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y", pady=12, padx=(0, 12))
        self.tree.configure(yscrollcommand=scrollbar.set)

        # ── Side panel có scroll ──
        side_outer = tk.Frame(body, bg=PANEL_BG, width=330, highlightbackground="#223456", highlightthickness=1)
        side_outer.pack(side="right", fill="y", padx=(12, 0))
        side_outer.pack_propagate(False)

        side_canvas = tk.Canvas(side_outer, bg=PANEL_BG, highlightthickness=0, width=310)
        side_scroll = ttk.Scrollbar(side_outer, orient="vertical", command=side_canvas.yview)
        side_canvas.configure(yscrollcommand=side_scroll.set)
        side_scroll.pack(side="right", fill="y")
        side_canvas.pack(side="left", fill="both", expand=True)

        side = tk.Frame(side_canvas, bg=PANEL_BG)
        side_win = side_canvas.create_window((0, 0), window=side, anchor="nw")

        def _on_side_configure(e: Any) -> None:
            side_canvas.configure(scrollregion=side_canvas.bbox("all"))
            side_canvas.itemconfig(side_win, width=side_canvas.winfo_width())
        side.bind("<Configure>", _on_side_configure)

        def _on_mousewheel(e: Any) -> None:
            side_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        side_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Điều khiển profile ──
        tk.Label(side, text="Điều khiển profile", bg=PANEL_BG, fg=TEXT, font=("Segoe UI Semibold", 14)).pack(anchor="w", padx=16, pady=(14, 2))
        self.selected_label = tk.Label(side, text="Chưa chọn profile", bg=PANEL_BG, fg=MUTED, wraplength=280, justify="left", font=("Segoe UI", 10))
        self.selected_label.pack(anchor="w", padx=16, pady=(0, 8))

        self._button(side, "Mở profile đã chọn", self.open_selected, SUCCESS, "#063222").pack(fill="x", padx=16, pady=3)
        self._button(side, "Đóng profile đã chọn", self.close_selected, DANGER, "#3a0b16").pack(fill="x", padx=16, pady=3)
        self._button(side, "Xem JSON profile", self.show_selected_json, ACCENT_2, "#2b214a").pack(fill="x", padx=16, pady=3)

        tk.Label(side, text="Chrome command", bg=PANEL_BG, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=16, pady=(10, 2))
        tk.Entry(side, textvariable=self.command, bg="#0b1628", fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 9)).pack(fill="x", padx=16, ipady=5)

        tk.Label(side, text="Proxy khi mở (tùy chọn)", bg=PANEL_BG, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=16, pady=(8, 2))
        tk.Entry(side, textvariable=self.proxy, bg="#0b1628", fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 9)).pack(fill="x", padx=16, ipady=5)
        tk.Label(side, text="Format: HTTP|host|port|user|pass", bg=PANEL_BG, fg="#60708f", font=("Segoe UI", 8)).pack(anchor="w", padx=16)

        # ── Divider ──
        tk.Frame(side, bg="#1e3a5f", height=1).pack(fill="x", padx=12, pady=8)

        # ── Hẹn giờ tự mở ──
        hg_hdr = tk.Frame(side, bg=PANEL_BG)
        hg_hdr.pack(fill="x", padx=16, pady=(0, 6))
        tk.Label(hg_hdr, text="⏰  Hẹn giờ tự mở", bg=PANEL_BG, fg=WARNING, font=("Segoe UI Semibold", 12)).pack(side="left")
        self.sched_toggle_btn = tk.Button(
            hg_hdr, text="BẬT", font=("Segoe UI Semibold", 9), relief="flat", bd=0,
            padx=10, pady=3, cursor="hand2",
            bg="#063222", fg=SUCCESS,
            command=self._toggle_scheduler,
        )
        self.sched_toggle_btn.pack(side="right")

        # Mode selector
        mode_row = tk.Frame(side, bg=PANEL_BG)
        mode_row.pack(fill="x", padx=16, pady=(0, 4))
        tk.Radiobutton(mode_row, text="Lặp mỗi X phút", variable=self.sched_mode,
                       value="interval", bg=PANEL_BG, fg=ACCENT,
                       selectcolor="#0b1628", activebackground=PANEL_BG,
                       font=("Segoe UI", 9)).pack(side="left")
        tk.Radiobutton(mode_row, text="Giờ cố định", variable=self.sched_mode,
                       value="fixed", bg=PANEL_BG, fg=MUTED,
                       selectcolor="#0b1628", activebackground=PANEL_BG,
                       font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

        def _srow(label: str, var: tk.Variable, fg: str = TEXT, bold: bool = False) -> None:
            row = tk.Frame(side, bg=PANEL_BG)
            row.pack(fill="x", padx=16, pady=2)
            tk.Label(row, text=label, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), width=19, anchor="w").pack(side="left")
            tk.Entry(row, textvariable=var, bg="#0b1628", fg=fg,
                     insertbackground=TEXT, relief="flat",
                     font=("Segoe UI Semibold" if bold else "Segoe UI", 10), width=7).pack(side="left", ipady=4)

        _srow("Lặp mỗi (phút)", self.sched_interval, ACCENT, bold=True)
        _srow("Giờ cố định (HH:MM)", self.sched_time, MUTED)
        _srow("Mở mỗi lần (profile)", self.sched_batch, ACCENT, bold=True)
        _srow("Đợi trước đóng (s)", self.sched_wait, WARNING, bold=True)
        _srow("Delay mỗi mở (s)", self.sched_delay)

        self.sched_status_lbl = tk.Label(
            side, textvariable=self.sched_status,
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
            wraplength=285, justify="left",
        )
        self.sched_status_lbl.pack(anchor="w", padx=16, pady=(6, 4))

        # ── Nút Chạy ngay ──
        self._button(side, "▶  Chạy ngay (test)", self._open_all_folder_profiles, SUCCESS, "#063222").pack(fill="x", padx=16, pady=(4, 16))


        console_card = tk.Frame(self, bg=PANEL_BG, highlightbackground="#223456", highlightthickness=1)
        console_card.pack(fill="both", padx=20, pady=(0, 12))
        console_header = tk.Frame(console_card, bg=PANEL_BG)
        console_header.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(console_header, text="Console log", bg=PANEL_BG, fg=ACCENT, font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Label(console_header, text=f"File: {LOG_FILE}", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")
        self.console = tk.Text(console_card, height=8, bg="#050b14", fg="#c8facc", insertbackground=TEXT, relief="flat", font=("Consolas", 9), wrap="word")
        self.console.pack(fill="both", expand=False, padx=12, pady=(8, 12))
        self.console.configure(state="disabled")

        footer = tk.Frame(self, bg=APP_BG)
        footer.pack(fill="x", padx=20, pady=(0, 14))
        self.progress = ttk.Progressbar(footer, mode="indeterminate", style="Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 12))
        tk.Label(footer, textvariable=self.status_text, bg=APP_BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="right")

    def _labeled_entry(self, parent: tk.Widget, label: str, variable: tk.Variable, width: int) -> tk.Frame:
        frame = tk.Frame(parent, bg=PANEL_BG)
        tk.Label(frame, text=label, bg=PANEL_BG, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Entry(frame, textvariable=variable, width=width, bg="#0b1628", fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).pack(ipady=7)
        return frame

    def _button(self, parent: tk.Widget, text: str, command: Any, fg: str, bg: str) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=fg, activeforeground="#06101f", relief="flat", bd=0, padx=14, pady=9, cursor="hand2", font=("Segoe UI Semibold", 10))

    def _run_async(self, action: str, fn: Any) -> None:
        self.status_text.set(f"Đang xử lý: {action}...")
        self.logger.info("Start action: %s", action)
        self.progress.start(10)
        threading.Thread(target=self._worker, args=(action, fn), daemon=True).start()

    def _worker(self, action: str, fn: Any) -> None:
        try:
            self.queue.put(("result", action, fn()))
        except Exception as exc:  # noqa: BLE001 - display any API/thread error to user
            self.queue.put(("error", action, exc))

    def _process_queue(self) -> None:
        try:
            while True:
                kind, action, payload = self.queue.get_nowait()
                self.progress.stop()
                if kind == "log":
                    self._append_console(str(action))
                    continue
                if kind == "error":
                    self.logger.error("Action failed: %s -> %s", action, payload)
                    self.pill.config(text="API: lỗi", fg=DANGER)
                    self.status_text.set(f"Lỗi {action}: {payload}")
                    messagebox.showerror("Hidemium Controller", str(payload))
                elif action == "refresh":
                    self.pill.config(text="API: online", fg=SUCCESS)
                    self._render_profiles(payload)
                elif action == "check":
                    self.pill.config(text="API: online", fg=SUCCESS)
                    self.status_text.set("API Hidemium đang hoạt động.")
                    messagebox.showinfo("Hidemium Controller", "Kết nối API Hidemium OK.")
                else:
                    self.logger.info("Action completed: %s", action)
                    self.status_text.set(f"Xong: {action}")
                    if action in {"open", "close"}:
                        self.after(800, self.refresh_profiles)
        except queue.Empty:
            pass
        self.after(100, self._process_queue)

    def _append_console(self, line: str) -> None:
        self.console.configure(state="normal")
        self.console.insert("end", line + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def refresh_profiles(self) -> None:
        folder_ids = self.FOLDER_MAP.get(self.folder_var.get(), [])
        self._run_async("refresh", lambda: self._client().list_profiles(
            self.is_local.get(), 1, int(self.limit.get()), self.search.get().strip(), folder_ids
        ))

    def check_api(self) -> None:
        self._run_async("check", lambda: self._client().get_user_uuid())

    def open_selected(self) -> None:
        uuid = self._selected_uuid_or_warn()
        if not uuid:
            return
        self._run_async("open", lambda: self._client().open_profile(uuid, self.command.get().strip(), self.proxy.get().strip()))

    def close_selected(self) -> None:
        uuid = self._selected_uuid_or_warn()
        if not uuid:
            return
        self._run_async("close", lambda: self._client().close_profile(uuid))

    def show_selected_json(self) -> None:
        uuid = self._selected_uuid_or_warn()
        if not uuid:
            return
        profile = next((p for p in self.profiles if self._profile_uuid(p) == uuid), {})
        top = tk.Toplevel(self)
        top.title(f"Profile JSON - {uuid}")
        top.geometry("760x560")
        top.configure(bg=APP_BG)
        text = tk.Text(top, bg="#08111f", fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 10), wrap="none")
        text.pack(fill="both", expand=True, padx=14, pady=14)
        import json

        text.insert("1.0", json.dumps(profile, ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def _selected_uuid_or_warn(self) -> str:
        if not self.selected_uuid:
            messagebox.showwarning("Hidemium Controller", "Bạn chưa chọn profile trong bảng.")
            return ""
        return self.selected_uuid

    def _on_select(self, _event: tk.Event) -> None:
        selected = self.tree.selection()
        if not selected:
            self.selected_uuid = ""
            self.selected_label.config(text="Chưa chọn profile")
            return
        values = self.tree.item(selected[0], "values")
        self.selected_uuid = str(values[1])
        self.selected_label.config(text=f"{values[0]}\nUUID: {self.selected_uuid}")

    def _render_profiles(self, response: Any) -> None:
        profiles = self._extract_profiles(response)
        self.profiles = profiles
        self.tree.delete(*self.tree.get_children())
        for profile in profiles:
            uuid = self._profile_uuid(profile)
            self.tree.insert("", "end", values=(
                self._pick(profile, "name", "browser_name", "profile_name", default="(no name)"),
                uuid,
                self._status(profile),
                self._proxy(profile),
                self._folder(profile),
                self._pick(profile, "note", "notes", "description", default=""),
            ))
        self.selected_uuid = ""
        self.selected_label.config(text="Chưa chọn profile")
        self.logger.info("Rendered %s profiles", len(profiles))
        self.status_text.set(f"Đã tải {len(profiles)} profile.")

    def _extract_profiles(self, response: Any) -> list[dict[str, Any]]:
        if isinstance(response, list):
            return [x for x in response if isinstance(x, dict)]
        if not isinstance(response, dict):
            return []
        candidates = [response]
        for key in ("data", "result", "rows", "items", "browsers", "profiles", "content"):
            value = response.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                candidates.append(value)
        for obj in candidates:
            for key in ("content", "data", "rows", "items", "browsers", "profiles", "docs"):
                value = obj.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    def _profile_uuid(self, profile: dict[str, Any]) -> str:
        return str(self._pick(profile, "uuid", "browser_uuid", "id", "profile_uuid", default=""))

    def _status(self, profile: dict[str, Any]) -> str:
        raw = self._pick(profile, "status", "status_name", "state", "browser_status", default="")
        if isinstance(raw, dict):
            return str(self._pick(raw, "name", "title", "value", default=""))
        return str(raw or "")

    def _proxy(self, profile: dict[str, Any]) -> str:
        proxy = self._pick(profile, "proxy", "proxy_info", "proxyInfo", default="")
        if isinstance(proxy, dict):
            host = self._pick(proxy, "host", "ip", "server", default="")
            port = self._pick(proxy, "port", default="")
            proxy_type = self._pick(proxy, "type", "mode", default="")
            return f"{proxy_type} {host}:{port}".strip()
        return str(proxy or "")

    def _folder(self, profile: dict[str, Any]) -> str:
        folder = self._pick(profile, "folder", "folder_name", "folderName", default="")
        if isinstance(folder, dict):
            return str(self._pick(folder, "name", "title", default=""))
        return str(folder or "")

    def _pick(self, obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        return default

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def _toggle_scheduler(self) -> None:
        enabled = not self.sched_enabled.get()
        self.sched_enabled.set(enabled)
        if enabled:
            mode = self.sched_mode.get()
            if mode == "interval":
                mins = max(1, int(self.sched_interval.get()))
                self._sched_next_run = datetime.datetime.now() + datetime.timedelta(minutes=mins)
                self.sched_toggle_btn.config(text="TẮT", bg="#3a0b16", fg=DANGER)
                self.sched_status.set(f"✅ Lặp mỗi {mins} phút")
                self.sched_status_lbl.config(fg=SUCCESS)
                self.logger.info("Scheduler INTERVAL mode: every %d min", mins)
            else:
                raw = self.sched_time.get().strip()
                try:
                    datetime.datetime.strptime(raw, "%H:%M")
                except ValueError:
                    messagebox.showerror("Hẹn giờ", "Giờ không hợp lệ! Định dạng HH:MM (ví dụ: 08:30)")
                    self.sched_enabled.set(False)
                    return
                self._sched_fired_today = ""
                self.sched_toggle_btn.config(text="TẮT", bg="#3a0b16", fg=DANGER)
                self.sched_status.set(f"✅ Hẹn giờ cố định: {raw} mỗi ngày")
                self.sched_status_lbl.config(fg=SUCCESS)
                self.logger.info("Scheduler FIXED mode at %s", raw)
        else:
            self.sched_toggle_btn.config(text="BẬT", bg="#063222", fg=SUCCESS)
            self.sched_status.set("Hẹn giờ chưa bật")
            self.sched_status_lbl.config(fg=MUTED)
            self._sched_next_run = None
            self.logger.info("Scheduler DISABLED")

    def _scheduler_tick(self) -> None:
        """Chạy mỗi giây — hỗ trợ cả mode interval và fixed."""
        if self.sched_enabled.get() and not self._sched_running:
            now = datetime.datetime.now()
            mode = self.sched_mode.get()

            if mode == "interval":
                # ── Mode lặp lại mỗi X phút ──
                if self._sched_next_run and now >= self._sched_next_run:
                    mins = max(1, int(self.sched_interval.get()))
                    self._sched_next_run = now + datetime.timedelta(minutes=mins)
                    self.logger.info("Interval scheduler FIRED — next run at %s",
                                     self._sched_next_run.strftime("%H:%M:%S"))
                    self.sched_status.set(f"🔥 Đang chạy... (tiếp theo: {self._sched_next_run.strftime('%H:%M')})")
                    self.sched_status_lbl.config(fg=WARNING)
                    threading.Thread(target=self._auto_open_all, daemon=True).start()
                elif self._sched_next_run:
                    diff = self._sched_next_run - now
                    h, rem = divmod(int(diff.total_seconds()), 3600)
                    m, s   = divmod(rem, 60)
                    mins   = int(self.sched_interval.get())
                    self.sched_status.set(f"⏳ Lặp {mins}ph — chạy tiếp sau {h:02d}:{m:02d}:{s:02d}")
                    self.sched_status_lbl.config(fg=MUTED)

            else:
                # ── Mode giờ cố định ──
                today = now.strftime("%Y-%m-%d")
                target = self.sched_time.get().strip()
                current_hhmm = now.strftime("%H:%M")

                if current_hhmm == target and self._sched_fired_today != today:
                    self._sched_fired_today = today
                    self.logger.info("Fixed scheduler FIRED at %s", target)
                    self.sched_status.set(f"🔥 Đang mở profiles... ({today} {target})")
                    self.sched_status_lbl.config(fg=WARNING)
                    threading.Thread(target=self._auto_open_all, daemon=True).start()
                else:
                    try:
                        target_dt = datetime.datetime.strptime(f"{today} {target}", "%Y-%m-%d %H:%M")
                        if target_dt < now:
                            target_dt += datetime.timedelta(days=1)
                        diff = target_dt - now
                        h, rem = divmod(int(diff.total_seconds()), 3600)
                        m, s = divmod(rem, 60)
                        self.sched_status.set(f"⏳ Còn {h:02d}:{m:02d}:{s:02d} → {target}")
                        self.sched_status_lbl.config(fg=MUTED)
                    except Exception:
                        pass

        self.after(1000, self._scheduler_tick)

    def _auto_open_all(self) -> None:
        """Mở profiles theo batch: mở N cái → đợi X giây → đóng hết → mở batch tiếp."""
        self._sched_running = True
        try:
            folder_ids = self.FOLDER_MAP.get(self.folder_var.get(), [])
            batch_size = max(1, int(self.sched_batch.get()))
            wait_sec   = max(1, int(self.sched_wait.get()))
            delay_sec  = max(0, int(self.sched_delay.get()))
            client     = self._client()

            # Lấy toàn bộ danh sách profiles
            response = client.list_profiles(self.is_local.get(), 1, 500, "", folder_ids)
            profiles = self._extract_profiles(response)
            total    = len(profiles)

            if total == 0:
                self.logger.warning("Scheduler: Không có profile nào để mở")
                self.sched_status.set("⚠️ Không có profile nào")
                return

            # Chia batch
            batches = [profiles[i:i + batch_size] for i in range(0, total, batch_size)]
            n_batches = len(batches)
            self.logger.info("Scheduler: %d profiles, %d batch x %d, wait=%ds, delay=%ds",
                             total, n_batches, batch_size, wait_sec, delay_sec)

            for b_idx, batch in enumerate(batches, 1):
                opened_uuids: list[str] = []

                # ── Mở từng profile trong batch ──
                for i, profile in enumerate(batch, 1):
                    uuid = self._profile_uuid(profile)
                    name = self._pick(profile, "name", "browser_name", default=uuid[:8])
                    if not uuid:
                        continue
                    try:
                        self.sched_status.set(
                            f"🟢 Batch {b_idx}/{n_batches} — Mở {i}/{len(batch)}: {name}"
                        )
                        client.open_profile(uuid, self.command.get().strip(), self.proxy.get().strip())
                        opened_uuids.append(uuid)
                        self.logger.info("[Batch %d/%d] Opened %d/%d: %s",
                                         b_idx, n_batches, i, len(batch), name)
                    except Exception as exc:
                        self.logger.error("[Batch %d] Failed to open %s: %s", b_idx, name, exc)

                    if i < len(batch) and delay_sec > 0:
                        time.sleep(delay_sec)

                # ── Đợi load ──
                self.logger.info("[Batch %d/%d] Đã mở %d profiles, đợi %ds...",
                                 b_idx, n_batches, len(opened_uuids), wait_sec)
                for remaining in range(wait_sec, 0, -1):
                    self.sched_status.set(
                        f"⏳ Batch {b_idx}/{n_batches}: đợi đóng sau {remaining}s "
                        f"({len(opened_uuids)} profile đang mở)..."
                    )
                    time.sleep(1)

                # ── Đóng tất cả profile trong batch ──
                self.sched_status.set(f"🔴 Batch {b_idx}/{n_batches}: Đang đóng {len(opened_uuids)} profiles...")
                for uuid in opened_uuids:
                    try:
                        client.close_profile(uuid)
                        self.logger.info("[Batch %d] Closed: %s", b_idx, uuid)
                    except Exception as exc:
                        self.logger.error("[Batch %d] Failed to close %s: %s", b_idx, uuid, exc)

                self.logger.info("[Batch %d/%d] Xong!", b_idx, n_batches)

                # Refresh UI sau mỗi batch
                self.after(0, self.refresh_profiles)

            # ── Hoàn tất ──
            done_time = datetime.datetime.now().strftime("%H:%M:%S")
            self.sched_status.set(f"✅ Hoàn tất {total} profiles lúc {done_time}")
            self.sched_status_lbl.config(fg=SUCCESS)
            self.logger.info("Scheduler: Hoàn tất tất cả %d batches (%d profiles)", n_batches, total)

        except Exception as exc:
            self.logger.error("Scheduler error: %s", exc)
            self.sched_status.set(f"❌ Lỗi: {exc}")
            self.sched_status_lbl.config(fg=DANGER)
        finally:
            self._sched_running = False

    def _open_all_folder_profiles(self) -> None:
        """Nút 'Chạy ngay' — chạy batch ngay lập tức để test."""
        if self._sched_running:
            messagebox.showwarning("Đang chạy", "Đang mở profiles, vui lòng chờ hoàn tất...")
            return
        self.logger.info("Manual run-now triggered: folder='%s', batch=%d, wait=%ds",
                         self.folder_var.get(), self.sched_batch.get(), self.sched_wait.get())
        threading.Thread(target=self._auto_open_all, daemon=True).start()


if __name__ == "__main__":
    try:
        HidemiumGUI().mainloop()
    except HidemiumError as exc:
        messagebox.showerror("Hidemium Controller", str(exc))
