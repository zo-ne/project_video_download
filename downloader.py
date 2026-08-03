"""yt-dlp 下載封裝：最高畫質、自動合併、瀏覽器 Cookie。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import yt_dlp

from config import Cancelled, DownloadSettings, find_ffmpeg

LogFn = Callable[[str, str], None]           # (訊息, 層級)
ProgressFn = Callable[[float, str], None]    # (0~100, 狀態文字)


class Downloader:
    """把 yt-dlp 的 hook 事件轉成 GUI 用得上的日誌與進度回呼。"""

    def __init__(
        self,
        settings: DownloadSettings,
        log: LogFn,
        progress: ProgressFn,
        cancel_event: threading.Event,
    ) -> None:
        self.settings = settings
        self.log = log
        self.progress = progress
        self.cancel_event = cancel_event
        self._finished: list[Path] = []

    # ---------- yt-dlp hooks ----------

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _progress_hook(self, d: dict) -> None:
        self._check_cancel()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            speed = d.get("speed")
            eta = d.get("eta")
            speed_txt = f"{speed / 1024 / 1024:.2f} MB/s" if speed else "--"
            eta_txt = f"{int(eta)}s" if eta else "--"
            self.progress(pct, f"下載中 {pct:5.1f}%  {speed_txt}  剩餘 {eta_txt}")
        elif status == "finished":
            name = Path(d.get("filename", "")).name
            self.progress(100.0, "下載完成，準備合併…")
            self.log(f"分段下載完成：{name}", "info")

    def _postprocessor_hook(self, d: dict) -> None:
        self._check_cancel()
        # after_move 階段的 info_dict 才是最終落地的檔案路徑
        if d.get("status") == "finished" and d.get("postprocessor") == "MoveFiles":
            filepath = d.get("info_dict", {}).get("filepath")
            if filepath:
                self._finished.append(Path(filepath))

    # ---------- 選項 ----------

    def _ydl_opts(self) -> dict:
        outtmpl = str(self.settings.output_dir / "%(title)s.%(ext)s")
        opts: dict = {
            # 先挑最佳影音分軌合併，取不到才退回單一檔
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "noplaylist": False,
            "ignoreerrors": True,
            "continuedl": True,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "consoletitle": False,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "logger": _YdlLogger(self.log),
            # B 站分 P / YouTube 章節資訊保留在檔名之外，不額外寫檔
            "writethumbnail": False,
            "writesubtitles": False,
        }

        ffmpeg = find_ffmpeg()
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg

        if self.settings.use_cookies:
            opts["cookiesfrombrowser"] = (self.settings.browser, None, None, None)

        return opts

    # ---------- 對外 ----------

    def run(self, url: str) -> list[Path]:
        """下載單一網址（可能是播放清單），回傳所有產生的檔案路徑。"""
        self._finished = []
        opts = self._ydl_opts()

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                ydl.download([url])
            except Cancelled:
                raise
            except yt_dlp.utils.DownloadError as exc:
                self.log(f"下載失敗：{exc}", "error")
                return []

        # 去重並保留順序
        seen: set[Path] = set()
        result: list[Path] = []
        for path in self._finished:
            if path not in seen and path.exists():
                seen.add(path)
                result.append(path)
        return result


class _YdlLogger:
    """把 yt-dlp 內部訊息導到 GUI 日誌。"""

    def __init__(self, log: LogFn) -> None:
        self._log = log

    def debug(self, msg: str) -> None:
        # yt-dlp 會把一般訊息也送到 debug，前綴為 [debug] 的才是真的除錯訊息
        if msg.startswith("[debug] "):
            return
        text = msg.strip()
        if text:
            self._log(text, "info")

    def info(self, msg: str) -> None:
        self._log(msg.strip(), "info")

    def warning(self, msg: str) -> None:
        self._log(msg.strip(), "warn")

    def error(self, msg: str) -> None:
        self._log(msg.strip(), "error")
