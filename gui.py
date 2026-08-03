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
    OUTPUT_DIR,
    SUPPORTED_BROWSERS,
    VERTICAL_ANCHORS,
    VERTICAL_RATIOS,
    find_ffmpeg,
)
from downloader import Downloader

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

        self.var_clip = BooleanVar(value=False)
        self.var_mode = StringVar(value="crop")
        self.var_keep_original = BooleanVar(value=True)

        self.var_vertical = BooleanVar(value=False)
        self.var_vertical_ratio = StringVar(value=next(iter(VERTICAL_RATIOS)))
        self.var_vertical_anchor = StringVar(value=next(iter(VERTICAL_ANCHORS)))

        self.var_crop_top = IntVar(value=80)
        self.var_crop_bottom = IntVar(value=0)
        self.var_crop_left = IntVar(value=0)
        self.var_crop_right = IntVar(value=0)

        self.var_blur_x = IntVar(value=0)
        self.var_blur_y = IntVar(value=0)
        self.var_blur_w = IntVar(value=240)
        self.var_blur_h = IntVar(value=80)
        self.var_blur_strength = IntVar(value=12)

        self.var_status = StringVar(value="就緒")
        self.var_overall = StringVar(value="")

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

        # --- blur 參數 ---
        self.blur_frame = ttk.Frame(frame, padding=(0, 6, 0, 0))
        self.blur_frame.pack(fill="x")
        specs = [
            ("X", self.var_blur_x),
            ("Y", self.var_blur_y),
            ("寬", self.var_blur_w),
            ("高", self.var_blur_h),
            ("強度", self.var_blur_strength),
        ]
        self.blur_entries = self._spin_row(self.blur_frame, specs, limit=4000)

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

        ttk.Label(
            frame,
            text="來源本來就是直式（比目標更窄）時會原樣輸出，不會二次裁切。",
            foreground="#666",
        ).pack(anchor="w", pady=(4, 0))

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
        self.progress = ttk.Progressbar(bar_row, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
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

        for widget in (self.rb_crop, self.rb_blur):
            widget.configure(state="normal" if clip_on else "disabled")
        # 兩種處理任一開啟就會產生新檔，保留原始檔的選項才有意義
        self.chk_keep.configure(state="normal" if (clip_on or vertical_on) else "disabled")

        state = "readonly" if vertical_on else "disabled"
        self.cmb_ratio.configure(state=state)
        self.cmb_anchor.configure(state=state)

        crop_on = clip_on and self.var_mode.get() == "crop"
        blur_on = clip_on and self.var_mode.get() == "blur"
        for spin in self.crop_entries:
            spin.configure(state="normal" if crop_on else "disabled")
        for spin in self.blur_entries:
            spin.configure(state="normal" if blur_on else "disabled")

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
                elif kind == "done":
                    self._set_running(False)
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

    def _collect_settings(self) -> tuple[DownloadSettings, ClipSettings] | None:
        raw = self.txt_urls.get("1.0", "end").strip()
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        if not urls:
            messagebox.showwarning("缺少網址", "請至少輸入一個影片網址。")
            return None

        outdir = Path(self.var_outdir.get().strip() or str(OUTPUT_DIR))
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("輸出資料夾錯誤", f"無法建立資料夾：{exc}")
            return None

        dl = DownloadSettings(
            urls=urls,
            output_dir=outdir,
            use_cookies=self.var_cookies.get(),
            browser=self.var_browser.get(),
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
                keep_original=self.var_keep_original.get(),
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

        self.cancel_event.clear()
        self._set_running(True)
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
        self.root.destroy()

    # ---------- 背景任務 ----------

    def _run_job(self, dl: DownloadSettings, clip: ClipSettings) -> None:
        log = lambda msg, level="info": self._emit("log", msg, level)
        progress = lambda pct, text: self._emit("progress", pct, text)

        total = len(dl.urls)
        ok_count = 0
        fail_count = 0

        try:
            for index, url in enumerate(dl.urls, start=1):
                if self.cancel_event.is_set():
                    raise Cancelled()

                self._emit("overall", f"第 {index} / {total} 個網址")
                log(f"[{index}/{total}] {url}", "ok")

                downloader = Downloader(dl, log, progress, self.cancel_event)
                files = downloader.run(url)
                if not files:
                    log("此網址沒有產生任何檔案。", "warn")
                    fail_count += 1
                    continue

                for path in files:
                    log(f"已下載：{path.name}", "ok")
                    if not clip.needs_processing:
                        ok_count += 1
                        continue
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

            summary = f"全部完成：成功 {ok_count} 個，失敗 {fail_count} 個"
            log(summary, "ok")
            self._emit("done", summary)

        except Cancelled:
            log("任務已中止。", "warn")
            self._emit("done", "已中止")
        except Exception as exc:
            log(f"未預期的錯誤：{exc}", "error")
            log(traceback.format_exc(), "error")
            self._emit("done", "發生錯誤")

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

        self._append_log(f"輸出資料夾：{self.var_outdir.get()}", "info")


def main() -> None:
    root = Tk()
    App(root)
    root.mainloop()
