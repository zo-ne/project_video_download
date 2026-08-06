"""Tkinter 圖形介面。"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Tk, filedialog, messagebox
from tkinter import scrolledtext, ttk

import processor
from config import (
    APP_NAME,
    APP_VERSION,
    Cancelled,
    ClipSettings,
    DownloadSettings,
    ENCODERS,
    OUTPUT_DIR,
    SUPPORTED_BROWSERS,
    VERTICAL_ANCHORS,
    DEFAULT_FILENAME_TEMPLATE,
    VERTICAL_FILLS,
    VERTICAL_RATIOS,
    VIDEO_FORMATS,
    available_encoders,
    find_ffmpeg,
    has_template_field,
    load_settings,
    save_settings,
    validate_filename_template,
)
from downloader import Downloader

VERTICAL_HINTS = {
    "crop": "裁掉左右兩側填滿畫面；來源本來就是直式（比目標更窄）時會原樣輸出。",
    "blur": "完整畫面置中，上下用同一畫面放大後的高斯模糊補滿。",
    "black": "完整畫面置中，上下補黑色。",
}

LOG_COLORS = {
    "info": "#1a1a1a",
    "ok": "#0a7d33",
    "warn": "#b26a00",
    "error": "#c0261c",
}


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("860x760")
        self.root.minsize(760, 640)

        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._init_vars()
        self._build_ui()
        self._sync_option_state()
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._startup_checks()

    # ---------- 狀態變數 ----------

    def _init_vars(self) -> None:
        self.var_outdir = StringVar(value=str(OUTPUT_DIR))
        self.var_cookies = BooleanVar(value=False)
        self.var_browser = StringVar(value="edge")
        self.var_filename = StringVar(value=DEFAULT_FILENAME_TEMPLATE)
        self.var_container = StringVar(value=next(iter(VIDEO_FORMATS)))

        self.var_clip = BooleanVar(value=False)
        self.var_mode = StringVar(value="crop")
        self.var_keep_original = BooleanVar(value=True)

        self.var_vertical = BooleanVar(value=False)
        self.var_vertical_ratio = StringVar(value=next(iter(VERTICAL_RATIOS)))
        self.var_vertical_anchor = StringVar(value=next(iter(VERTICAL_ANCHORS)))
        self.var_vertical_fill = StringVar(value=next(iter(VERTICAL_FILLS)))
        self.var_vertical_blur = IntVar(value=20)

        self.var_crop_top = IntVar(value=80)
        self.var_crop_bottom = IntVar(value=0)
        self.var_crop_left = IntVar(value=0)
        self.var_crop_right = IntVar(value=0)

        self.var_blur_x = IntVar(value=0)
        self.var_blur_y = IntVar(value=0)
        self.var_blur_w = IntVar(value=240)
        self.var_blur_h = IntVar(value=80)
        self.var_blur_strength = IntVar(value=12)

        self.var_encoder = StringVar(value=next(iter(ENCODERS)))
        self.var_quality = IntVar(value=18)

        self.var_status = StringVar(value="就緒")
        self.var_overall = StringVar(value="")

        self._restore_settings()

    # ---------- 設定保存 ----------

    def _setting_vars(self) -> dict:
        """會被記住的欄位。網址不存，每次開啟應該是空的。"""
        return {
            "outdir": self.var_outdir,
            "cookies": self.var_cookies,
            "browser": self.var_browser,
            "filename": self.var_filename,
            "container": self.var_container,
            "clip": self.var_clip,
            "mode": self.var_mode,
            "keep_original": self.var_keep_original,
            "vertical": self.var_vertical,
            "vertical_ratio": self.var_vertical_ratio,
            "vertical_anchor": self.var_vertical_anchor,
            "vertical_fill": self.var_vertical_fill,
            "vertical_blur": self.var_vertical_blur,
            "crop_top": self.var_crop_top,
            "crop_bottom": self.var_crop_bottom,
            "crop_left": self.var_crop_left,
            "crop_right": self.var_crop_right,
            "blur_x": self.var_blur_x,
            "blur_y": self.var_blur_y,
            "blur_w": self.var_blur_w,
            "blur_h": self.var_blur_h,
            "blur_strength": self.var_blur_strength,
            "encoder": self.var_encoder,
            "quality": self.var_quality,
        }

    def _restore_settings(self) -> None:
        data = load_settings()
        # 下拉選單的選項可能隨版本增減，存進來的舊值要先確認還在清單裡，
        # 否則之後用它去查字典會 KeyError
        allowed = {
            "browser": SUPPORTED_BROWSERS,
            "container": list(VIDEO_FORMATS),
            "mode": ["crop", "blur", "delogo"],
            "vertical_ratio": list(VERTICAL_RATIOS),
            "vertical_anchor": list(VERTICAL_ANCHORS),
            "vertical_fill": list(VERTICAL_FILLS),
            "encoder": list(ENCODERS),
        }
        for key, var in self._setting_vars().items():
            if key not in data:
                continue
            value = data[key]
            if key in allowed and value not in allowed[key]:
                continue
            try:
                var.set(value)
            except Exception:
                pass

    def _persist_settings(self) -> None:
        try:
            save_settings({k: v.get() for k, v in self._setting_vars().items()})
        except Exception:
            pass

    # ---------- 介面 ----------

    def _build_ui(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        self._build_url_section(outer)
        self._build_options_section(outer)
        self._build_output_section(outer)
        self._build_control_section(outer)
        self._build_log_section(outer)

    def _build_url_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 影片網址 ", padding=8)
        frame.pack(fill="x")

        ttk.Label(
            frame,
            text="每行一個網址，支援 YouTube / Bilibili 單片與播放清單",
            foreground="#666",
        ).pack(anchor="w", pady=(0, 4))

        self.txt_urls = scrolledtext.ScrolledText(frame, height=5, wrap="none", font=("Consolas", 10))
        self.txt_urls.pack(fill="x")
        # Ctrl+D 刪掉游標所在（或選取範圍內）的網址
        self.txt_urls.bind("<Control-d>", self._on_delete_selected_urls)
        self.txt_urls.bind("<Control-D>", self._on_delete_selected_urls)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(
            row, text="刪除選取 (Ctrl+D)", command=self._delete_selected_urls, width=18
        ).pack(side="left")
        ttk.Button(row, text="清空全部", command=self._clear_urls, width=10).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(row, text="去除重複", command=self._dedupe_urls, width=10).pack(
            side="left", padx=(6, 0)
        )

    def _build_options_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 選項 ", padding=8)
        frame.pack(fill="x", pady=(10, 0))

        # --- Cookie ---
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Checkbutton(
            row,
            text="自動帶入瀏覽器 Cookie（解鎖 1080p / 4K 等會員畫質）",
            variable=self.var_cookies,
            command=self._sync_option_state,
        ).pack(side="left")
        self.cmb_browser = ttk.Combobox(
            row, textvariable=self.var_browser, values=SUPPORTED_BROWSERS,
            state="readonly", width=10,
        )
        self.cmb_browser.pack(side="left", padx=(8, 0))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=8)

        # --- 剪輯開關 ---
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Checkbutton(
            row,
            text="開啟自動剪輯 / 去水印",
            variable=self.var_clip,
            command=self._sync_option_state,
        ).pack(side="left")
        self.chk_keep = ttk.Checkbutton(
            row, text="保留原始檔（另存 _edited）", variable=self.var_keep_original
        )
        self.chk_keep.pack(side="left", padx=(16, 0))

        # --- 模式 ---
        self.mode_row = ttk.Frame(frame)
        self.mode_row.pack(fill="x", pady=(6, 0))
        ttk.Label(self.mode_row, text="模式：").pack(side="left")
        self.rb_crop = ttk.Radiobutton(
            self.mode_row, text="邊緣裁切 (Crop)", value="crop",
            variable=self.var_mode, command=self._sync_option_state,
        )
        self.rb_crop.pack(side="left")
        self.rb_blur = ttk.Radiobutton(
            self.mode_row, text="區域模糊 (Blur)", value="blur",
            variable=self.var_mode, command=self._sync_option_state,
        )
        self.rb_blur.pack(side="left", padx=(12, 0))
        self.rb_delogo = ttk.Radiobutton(
            self.mode_row, text="去標誌 (Delogo)", value="delogo",
            variable=self.var_mode, command=self._sync_option_state,
        )
        self.rb_delogo.pack(side="left", padx=(12, 0))

        # --- crop 參數 ---
        self.crop_frame = ttk.Frame(frame, padding=(0, 6, 0, 0))
        self.crop_frame.pack(fill="x")
        specs = [
            ("上 (px)", self.var_crop_top),
            ("下 (px)", self.var_crop_bottom),
            ("左 (px)", self.var_crop_left),
            ("右 (px)", self.var_crop_right),
        ]
        self.crop_entries = self._spin_row(self.crop_frame, specs, limit=4000)

        # --- blur / delogo 共用的區域參數 ---
        self.blur_frame = ttk.Frame(frame, padding=(0, 6, 0, 0))
        self.blur_frame.pack(fill="x")
        specs = [
            ("X", self.var_blur_x),
            ("Y", self.var_blur_y),
            ("寬", self.var_blur_w),
            ("高", self.var_blur_h),
        ]
        self.region_entries = self._spin_row(self.blur_frame, specs, limit=4000)
        self.lbl_strength = ttk.Label(self.blur_frame, text="強度")
        self.lbl_strength.pack(side="left", padx=(0, 4))
        self.spin_strength = ttk.Spinbox(
            self.blur_frame, from_=1, to=100, textvariable=self.var_blur_strength, width=7
        )
        self.spin_strength.pack(side="left")

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=8)

        # --- 直式短影音裁切（獨立於去水印，可疊加） ---
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Checkbutton(
            row,
            text="裁成手機直式尺寸（短影音）",
            variable=self.var_vertical,
            command=self._sync_option_state,
        ).pack(side="left")

        ttk.Label(row, text="比例").pack(side="left", padx=(16, 4))
        self.cmb_ratio = ttk.Combobox(
            row, textvariable=self.var_vertical_ratio,
            values=list(VERTICAL_RATIOS), state="readonly", width=26,
        )
        self.cmb_ratio.pack(side="left")

        ttk.Label(row, text="取景").pack(side="left", padx=(12, 4))
        self.cmb_anchor = ttk.Combobox(
            row, textvariable=self.var_vertical_anchor,
            values=list(VERTICAL_ANCHORS), state="readonly", width=6,
        )
        self.cmb_anchor.pack(side="left")

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="填滿方式").pack(side="left", padx=(0, 4))
        self.cmb_fill = ttk.Combobox(
            row, textvariable=self.var_vertical_fill, values=list(VERTICAL_FILLS),
            state="readonly", width=28,
        )
        self.cmb_fill.pack(side="left")
        self.cmb_fill.bind("<<ComboboxSelected>>", lambda _e: self._sync_option_state())

        self.lbl_vblur = ttk.Label(row, text="模糊強度")
        self.lbl_vblur.pack(side="left", padx=(12, 4))
        self.spin_vblur = ttk.Spinbox(
            row, from_=1, to=200, textvariable=self.var_vertical_blur, width=6
        )
        self.spin_vblur.pack(side="left")

        self.lbl_vertical_hint = ttk.Label(frame, text="", foreground="#666")
        self.lbl_vertical_hint.pack(anchor="w", pady=(4, 0))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=8)

        # --- 編碼設定 ---
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="編碼器").pack(side="left", padx=(0, 4))
        self.cmb_encoder = ttk.Combobox(
            row, textvariable=self.var_encoder, values=list(ENCODERS),
            state="readonly", width=20,
        )
        self.cmb_encoder.pack(side="left")

        ttk.Label(row, text="品質").pack(side="left", padx=(12, 4))
        ttk.Spinbox(row, from_=0, to=51, textvariable=self.var_quality, width=5).pack(
            side="left"
        )
        ttk.Label(
            row, text="（數字越小畫質越好、檔案越大；18 接近視覺無損）",
            foreground="#666",
        ).pack(side="left", padx=(8, 0))

    def _spin_row(self, parent: ttk.Frame, specs, limit: int) -> list[ttk.Spinbox]:
        widgets = []
        for label, var in specs:
            ttk.Label(parent, text=label).pack(side="left", padx=(0, 4))
            spin = ttk.Spinbox(parent, from_=0, to=limit, textvariable=var, width=7)
            spin.pack(side="left", padx=(0, 14))
            widgets.append(spin)
        return widgets

    def _build_output_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(10, 0))
        ttk.Label(frame, text="輸出資料夾：").pack(side="left")
        ttk.Entry(frame, textvariable=self.var_outdir).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ttk.Button(frame, text="瀏覽…", command=self._pick_outdir, width=8).pack(side="left")
        ttk.Button(frame, text="開啟", command=self._open_outdir, width=6).pack(
            side="left", padx=(6, 0)
        )

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="檔名樣板：").pack(side="left")
        ttk.Entry(row, textvariable=self.var_filename).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ttk.Button(row, text="還原預設", command=self._reset_filename, width=9).pack(
            side="left"
        )
        ttk.Label(row, text="格式").pack(side="left", padx=(12, 4))
        ttk.Combobox(
            row, textvariable=self.var_container, values=list(VIDEO_FORMATS),
            state="readonly", width=22,
        ).pack(side="left")

        ttk.Label(
            parent,
            text="可用 %(title)s 標題、%(id)s 影片 ID、%(uploader)s 頻道、"
                 "%(upload_date)s 上傳日期、%(autonumber)s 流水號；副檔名會自動補。",
            foreground="#666",
        ).pack(anchor="w", pady=(2, 0))

    def _build_control_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(12, 0))

        self.btn_start = ttk.Button(frame, text="開始下載", command=self._on_start, width=14)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(
            frame, text="停止", command=self._on_stop, width=10, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(frame, text="清除日誌", command=self._clear_log, width=10).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(frame, textvariable=self.var_overall, foreground="#555").pack(side="right")

        bar_row = ttk.Frame(parent)
        bar_row.pack(fill="x", pady=(8, 0))

        ttk.Label(bar_row, text="目前步驟", width=8, foreground="#555").grid(
            row=0, column=0, sticky="w"
        )
        self.progress = ttk.Progressbar(bar_row, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=1, sticky="ew")

        ttk.Label(bar_row, text="整體進度", width=8, foreground="#555").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        self.progress_all = ttk.Progressbar(bar_row, mode="determinate", maximum=100)
        self.progress_all.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        bar_row.columnconfigure(1, weight=1)

        ttk.Label(parent, textvariable=self.var_status, foreground="#333").pack(
            anchor="w", pady=(4, 0)
        )

    def _build_log_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 日誌 ", padding=6)
        frame.pack(fill="both", expand=True, pady=(10, 0))
        self.txt_log = scrolledtext.ScrolledText(
            frame, height=14, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.txt_log.pack(fill="both", expand=True)
        for level, color in LOG_COLORS.items():
            self.txt_log.tag_config(level, foreground=color)

    # ---------- 介面狀態 ----------

    def _sync_option_state(self) -> None:
        self.cmb_browser.configure(state="readonly" if self.var_cookies.get() else "disabled")

        clip_on = self.var_clip.get()
        vertical_on = self.var_vertical.get()

        for widget in (self.rb_crop, self.rb_blur, self.rb_delogo):
            widget.configure(state="normal" if clip_on else "disabled")
        # 兩種處理任一開啟就會產生新檔，保留原始檔的選項才有意義
        self.chk_keep.configure(state="normal" if (clip_on or vertical_on) else "disabled")

        state = "readonly" if vertical_on else "disabled"
        self.cmb_ratio.configure(state=state)
        self.cmb_fill.configure(state=state)

        fill = VERTICAL_FILLS.get(self.var_vertical_fill.get(), "crop")
        # 補背景的兩種填法會保留完整畫面，沒有「要留哪一側」的問題
        anchor_on = vertical_on and fill == "crop"
        self.cmb_anchor.configure(state="readonly" if anchor_on else "disabled")
        vblur_on = vertical_on and fill == "blur"
        self.spin_vblur.configure(state="normal" if vblur_on else "disabled")
        self.lbl_vblur.configure(foreground="#333" if vblur_on else "#aaa")
        self.lbl_vertical_hint.configure(text=VERTICAL_HINTS.get(fill, ""))

        mode = self.var_mode.get()
        crop_on = clip_on and mode == "crop"
        region_on = clip_on and mode in ("blur", "delogo")
        for spin in self.crop_entries:
            spin.configure(state="normal" if crop_on else "disabled")
        for spin in self.region_entries:
            spin.configure(state="normal" if region_on else "disabled")
        # delogo 是往內插補，沒有「強度」可調
        strength_on = clip_on and mode == "blur"
        self.spin_strength.configure(state="normal" if strength_on else "disabled")
        self.lbl_strength.configure(foreground="#333" if strength_on else "#aaa")

    def _set_running(self, running: bool) -> None:
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if not running:
            self.progress["value"] = 0

    # ---------- 事件佇列 ----------

    def _emit(self, kind: str, *payload) -> None:
        """由工作執行緒呼叫，只丟訊息不碰 widget。"""
        self.events.put((kind, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(*payload)
                elif kind == "progress":
                    pct, text = payload
                    self.progress["value"] = pct
                    self.var_status.set(text)
                elif kind == "overall":
                    self.var_overall.set(payload[0])
                elif kind == "overall_pct":
                    self.progress_all["value"] = payload[0]
                elif kind == "done":
                    self._set_running(False)
                    self.progress_all["value"] = 100 if payload[1] else 0
                    self.var_status.set(payload[0])
                    self.var_overall.set("")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _append_log(self, text: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{stamp}] {text}\n", level)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ---------- 網址欄 ----------

    def _url_lines(self) -> list[str]:
        raw = self.txt_urls.get("1.0", "end").strip()
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _set_url_lines(self, lines: list[str]) -> None:
        self.txt_urls.delete("1.0", "end")
        if lines:
            self.txt_urls.insert("1.0", "\n".join(lines) + "\n")

    def _on_delete_selected_urls(self, _event) -> str:
        self._delete_selected_urls()
        return "break"  # 蓋掉 Text 內建的 Ctrl+D

    def _delete_selected_urls(self) -> None:
        """刪掉選取範圍碰到的整行；沒有選取就刪游標所在那一行。"""
        try:
            first = int(self.txt_urls.index("sel.first").split(".")[0])
            last_index = self.txt_urls.index("sel.last")
            last = int(last_index.split(".")[0])
            # 選取剛好停在行首時，那一行其實沒被選到，不要跟著刪
            if last > first and last_index.endswith(".0"):
                last -= 1
        except Exception:
            first = last = int(self.txt_urls.index("insert").split(".")[0])

        self.txt_urls.delete(f"{first}.0", f"{last + 1}.0")
        self.txt_urls.focus_set()

    def _clear_urls(self) -> None:
        if not self._url_lines():
            return
        if not messagebox.askokcancel("清空網址", "確定要清空所有網址嗎？"):
            return
        self.txt_urls.delete("1.0", "end")

    def _dedupe_urls(self) -> None:
        lines = self._url_lines()
        unique = list(dict.fromkeys(lines))
        removed = len(lines) - len(unique)
        if not removed:
            self._append_log("沒有重複的網址。", "info")
            return
        self._set_url_lines(unique)
        self._append_log(f"已移除 {removed} 個重複網址。", "ok")

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ---------- 按鈕 ----------

    def _pick_outdir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.var_outdir.get() or str(OUTPUT_DIR))
        if chosen:
            self.var_outdir.set(chosen)

    def _open_outdir(self) -> None:
        path = Path(self.var_outdir.get())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _reset_filename(self) -> None:
        self.var_filename.set(DEFAULT_FILENAME_TEMPLATE)

    def _collect_settings(self) -> tuple[DownloadSettings, ClipSettings] | None:
        urls = self._url_lines()
        if not urls:
            messagebox.showwarning("缺少網址", "請至少輸入一個影片網址。")
            return None

        outdir = Path(self.var_outdir.get().strip() or str(OUTPUT_DIR))
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("輸出資料夾錯誤", f"無法建立資料夾：{exc}")
            return None

        template = self.var_filename.get().strip()
        error = validate_filename_template(template)
        if error:
            messagebox.showerror("檔名錯誤", error)
            return None
        # 固定檔名遇到多部影片會一路覆蓋，只留下最後一部
        if not has_template_field(template):
            if not messagebox.askokcancel(
                "檔名沒有變數",
                f"「{template}」是固定檔名，一次下載多部影片時會互相覆蓋，"
                "只會留下最後一部。\n\n"
                "要避免的話可以加上 %(title)s 或 %(autonumber)s。\n\n仍要繼續嗎？",
            ):
                return None

        dl = DownloadSettings(
            urls=urls,
            output_dir=outdir,
            use_cookies=self.var_cookies.get(),
            browser=self.var_browser.get(),
            filename_template=template,
            container=VIDEO_FORMATS[self.var_container.get()],
        )

        try:
            clip = ClipSettings(
                enabled=self.var_clip.get(),
                mode=self.var_mode.get(),
                crop_top=self.var_crop_top.get(),
                crop_bottom=self.var_crop_bottom.get(),
                crop_left=self.var_crop_left.get(),
                crop_right=self.var_crop_right.get(),
                blur_x=self.var_blur_x.get(),
                blur_y=self.var_blur_y.get(),
                blur_w=self.var_blur_w.get(),
                blur_h=self.var_blur_h.get(),
                blur_strength=self.var_blur_strength.get(),
                vertical=self.var_vertical.get(),
                vertical_ratio=self.var_vertical_ratio.get(),
                vertical_anchor=VERTICAL_ANCHORS[self.var_vertical_anchor.get()],
                vertical_fill=VERTICAL_FILLS[self.var_vertical_fill.get()],
                vertical_blur=self.var_vertical_blur.get(),
                keep_original=self.var_keep_original.get(),
                encoder=ENCODERS[self.var_encoder.get()],
                quality=self.var_quality.get(),
            )
        except Exception:
            messagebox.showerror("參數錯誤", "剪輯參數必須是整數。")
            return None

        if clip.needs_processing:
            error = processor.validate(clip)
            if error:
                messagebox.showerror("參數錯誤", error)
                return None
            if not find_ffmpeg():
                messagebox.showerror(
                    "找不到 FFmpeg",
                    "剪輯功能需要 FFmpeg。請安裝並加入 PATH，或把 ffmpeg.exe 放到專案的 bin/ 目錄。",
                )
                return None

        return dl, clip

    def _on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        collected = self._collect_settings()
        if not collected:
            return
        dl, clip = collected

        self._persist_settings()
        self.cancel_event.clear()
        self._set_running(True)
        self.progress_all["value"] = 0
        self.var_status.set("準備中…")
        self._append_log(f"開始處理 {len(dl.urls)} 個網址", "ok")

        self.worker = threading.Thread(target=self._run_job, args=(dl, clip), daemon=True)
        self.worker.start()

    def _on_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.btn_stop.configure(state="disabled")
            self._append_log("已送出停止指令，等待目前步驟結束…", "warn")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("確認關閉", "任務仍在執行中，確定要關閉嗎？"):
                return
            self.cancel_event.set()
        self._persist_settings()
        self.root.destroy()

    # ---------- 背景任務 ----------

    def _run_job(self, dl: DownloadSettings, clip: ClipSettings) -> None:
        log = lambda msg, level="info": self._emit("log", msg, level)

        total = len(dl.urls)
        # 沒開後製時，下載就是該網址的全部工作
        dl_weight = 0.6 if clip.needs_processing else 1.0
        state = {"index": 1, "phase": "download"}

        def progress(pct: float, text: str) -> None:
            """單一步驟的進度，同時換算成整批的加權進度。"""
            self._emit("progress", pct, text)
            if state["phase"] == "download":
                frac = pct / 100 * dl_weight
            else:
                frac = dl_weight + pct / 100 * (1 - dl_weight)
            overall = (state["index"] - 1 + frac) / total * 100
            self._emit("overall_pct", max(0.0, min(100.0, overall)))

        ok_count = 0
        fail_count = 0
        numbering = 1  # %(autonumber)s 要跨網址接續，不能每個網址從 1 重來

        try:
            for index, url in enumerate(dl.urls, start=1):
                if self.cancel_event.is_set():
                    raise Cancelled()

                state["index"], state["phase"] = index, "download"
                self._emit("overall", f"第 {index} / {total} 個網址")
                log(f"[{index}/{total}] {url}", "ok")

                downloader = Downloader(
                    dl, log, progress, self.cancel_event, autonumber_start=numbering
                )
                files = downloader.run(url)
                numbering += len(files)
                if not files:
                    log("此網址沒有產生任何檔案。", "warn")
                    fail_count += 1
                    self._emit("overall_pct", index / total * 100)
                    continue

                for path in files:
                    log(f"已下載：{path.name}", "ok")
                    if not clip.needs_processing:
                        ok_count += 1
                        continue
                    state["phase"] = "process"
                    try:
                        result = processor.process(
                            path, clip, log, progress, self.cancel_event
                        )
                        log(f"已剪輯：{result.name}", "ok")
                        ok_count += 1
                    except Cancelled:
                        raise
                    except Exception as exc:
                        log(f"剪輯失敗（保留原始檔）：{exc}", "error")
                        fail_count += 1

                self._emit("overall_pct", index / total * 100)

            summary = f"全部完成：成功 {ok_count} 個，失敗 {fail_count} 個"
            log(summary, "ok")
            self._emit("done", summary, True)

        except Cancelled:
            log("任務已中止。", "warn")
            self._emit("done", "已中止", False)
        except Exception as exc:
            log(f"未預期的錯誤：{exc}", "error")
            log(traceback.format_exc(), "error")
            self._emit("done", "發生錯誤", False)

    # ---------- 啟動檢查 ----------

    def _startup_checks(self) -> None:
        self._append_log(f"{APP_NAME} v{APP_VERSION} 已啟動", "ok")
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            self._append_log(f"FFmpeg：{ffmpeg}", "info")
        else:
            self._append_log(
                "警告：找不到 FFmpeg。最高畫質合併與剪輯功能將無法使用。", "warn"
            )

        # yt-dlp 需要 JS runtime 才能解開 YouTube 的簽章挑戰，否則高畫質格式會缺失
        deno = shutil.which("deno")
        if deno:
            self._append_log(f"JS runtime：{deno}", "info")
        else:
            self._append_log(
                "警告：找不到 deno，YouTube 部分高畫質格式可能取不到。", "warn"
            )

        encoders = available_encoders()
        hw = [e for e in encoders if e != "cpu"]
        self._append_log(
            f"可用編碼器：{'、'.join(encoders)}"
            + ("" if hw else "（未偵測到硬體加速，將使用 CPU）"),
            "info",
        )

        self._append_log(f"輸出資料夾：{self.var_outdir.get()}", "info")


def main() -> None:
    root = Tk()
    App(root)
    root.mainloop()
