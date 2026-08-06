"""共用設定與資料結構。"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "影片下載 / 自動微剪輯工具"
APP_VERSION = "1.1.0"

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SETTINGS_FILE = BASE_DIR / "settings.json"

SUPPORTED_BROWSERS = ["edge", "chrome", "firefox", "brave", "opera", "vivaldi", "chromium"]

# 直式裁切可選的目標比例（寬 / 高）
VERTICAL_RATIOS: dict[str, float] = {
    "9:16 (Shorts / Reels / TikTok)": 9 / 16,
    "4:5 (IG 貼文)": 4 / 5,
    "1:1 (正方形)": 1.0,
}

VERTICAL_ANCHORS: dict[str, str] = {
    "置中": "center",
    "靠左": "left",
    "靠右": "right",
}

# 橫轉直的三種做法：裁掉兩側，或把完整畫面縮進去、上下用背景補滿
VERTICAL_FILLS: dict[str, str] = {
    "裁切兩側（畫面填滿，會切掉內容）": "crop",
    "模糊背景（保留完整畫面）": "blur",
    "上下黑邊（保留完整畫面）": "black",
}

# 輸出容器。mkv 什麼編碼都裝得下，webm 只收 VP8/VP9/AV1 + Vorbis/Opus。
VIDEO_FORMATS: dict[str, str] = {
    "MP4（相容性最好）": "mp4",
    "MKV（不轉檔，保留原始分軌）": "mkv",
    "WebM（VP9 / Opus）": "webm",
}

# 各容器的分軌挑選條件：先挑原生就是該格式的分軌，避免多做一次轉檔，
# 挑不到才退回「最佳畫質 + 交給 ffmpeg 轉」。
FORMAT_SELECTORS: dict[str, str] = {
    "mp4": "bestvideo*[ext=mp4]+bestaudio[ext=m4a]/bestvideo*+bestaudio/best",
    "mkv": "bestvideo*+bestaudio/best",
    "webm": "bestvideo*[ext=webm]+bestaudio[ext=webm]/bestvideo*+bestaudio/best",
}

# 後製一定要重新編碼，webm 裝不下 H.264，所以改寫成 mp4
PROCESSABLE_SUFFIXES = (".mp4", ".mkv", ".mov")

# Windows 檔名不允許的字元（也涵蓋路徑分隔符號）
INVALID_FILENAME_CHARS = '\\/:*?"<>|'

DEFAULT_FILENAME_TEMPLATE = "%(title)s"

# yt-dlp 的樣板欄位，例如 %(title)s、%(upload_date>%Y-%m-%d)s、%(autonumber)03d
_TEMPLATE_FIELD = re.compile(r"%\([^)]*\)[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsBjlqDSU]")

# 硬體編碼器。auto 會依序挑第一個實際可用的，全都不行就退回 CPU。
ENCODERS: dict[str, str] = {
    "自動（優先硬體加速）": "auto",
    "CPU (libx264)": "cpu",
    "NVIDIA (NVENC)": "nvenc",
    "Intel (QSV)": "qsv",
    "AMD (AMF)": "amf",
}

# 偏好順序：品質與相容性折衷後的經驗排序
HW_PREFERENCE = ("nvenc", "qsv", "amf")

ENCODER_CODECS: dict[str, str] = {
    "cpu": "libx264",
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv",
    "amf": "h264_amf",
}

# Windows 下用 CREATE_NO_WINDOW 避免每次呼叫 ffmpeg 都閃出黑色視窗
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class Cancelled(Exception):
    """使用者中止任務時拋出。"""


@dataclass
class ClipSettings:
    """自動剪輯 / 去水印設定。"""

    enabled: bool = False
    mode: str = "crop"  # "crop" / "blur" / "delogo"

    # crop 模式：四邊要裁掉的像素
    crop_top: int = 80
    crop_bottom: int = 0
    crop_left: int = 0
    crop_right: int = 0

    # blur / delogo 模式：要處理的矩形區域 (左上角 x, y 與寬高)
    blur_x: int = 0
    blur_y: int = 0
    blur_w: int = 240
    blur_h: int = 80
    blur_strength: int = 12  # 僅 blur 模式使用

    # 直式短影音轉換
    vertical: bool = False
    vertical_ratio: str = "9:16"
    vertical_anchor: str = "center"  # center / left / right，只有 crop 填法用得到
    vertical_fill: str = "crop"  # crop / blur / black
    vertical_blur: int = 20  # blur 填法的高斯模糊 sigma

    keep_original: bool = True

    # 編碼設定
    encoder: str = "auto"
    quality: int = 18  # CPU 是 CRF，硬體編碼器換算成各自的品質參數

    @property
    def needs_processing(self) -> bool:
        """去水印與直式裁切各自獨立，任一開啟就要送進 FFmpeg。"""
        return self.enabled or self.vertical

    @property
    def vertical_pads(self) -> bool:
        """填滿方式是補背景（保留完整畫面），而不是裁掉兩側。"""
        return self.vertical and self.vertical_fill in ("blur", "black")

    @property
    def uses_region(self) -> bool:
        """blur 與 delogo 共用同一組矩形區域參數。"""
        return self.mode in ("blur", "delogo")


@dataclass
class DownloadSettings:
    """下載設定。"""

    urls: list[str] = field(default_factory=list)
    output_dir: Path = OUTPUT_DIR
    use_cookies: bool = False
    browser: str = "edge"
    # yt-dlp 檔名樣板，不含副檔名
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    container: str = "mp4"  # mp4 / mkv / webm


def validate_filename_template(template: str) -> str | None:
    """檢查檔名樣板，回傳錯誤訊息，合法則回傳 None。

    只擋「一定會讓 yt-dlp 寫檔失敗」的寫法：空字串、非法字元、
    自己補副檔名。樣板欄位本身寫錯由 yt-dlp 自己報。
    """
    name = template.strip()
    if not name:
        return "檔名樣板不可留空。"

    # %(title)s 這類欄位裡的字元不算，先把欄位挖掉再檢查字面文字
    literal = _TEMPLATE_FIELD.sub("", name)
    bad = sorted({c for c in literal if c in INVALID_FILENAME_CHARS})
    if bad:
        return f"檔名不可含有 {' '.join(bad)} 這些字元。"
    if literal.lower().endswith((".mp4", ".mkv", ".webm")):
        return "檔名樣板不用自己加副檔名，程式會依所選格式補上。"
    # 落單的 % 會讓 yt-dlp 解析樣板時直接丟例外（%% 才是字面上的百分號）
    if "%" in literal.replace("%%", ""):
        return "單獨的 % 是樣板語法的開頭；要顯示百分號請打兩個 %%。"
    return None


def has_template_field(template: str) -> bool:
    """樣板裡有沒有 yt-dlp 欄位；沒有的話多部影片會撞名。"""
    return bool(_TEMPLATE_FIELD.search(template))


def _fallback_dirs() -> list[Path]:
    """PATH 找不到時的候補目錄。

    winget 安裝完只會改登錄檔的 PATH，既有的終端機不會生效，
    所以直接把常見安裝位置也納入搜尋，省去使用者重開終端機。
    """
    dirs = [BASE_DIR / "bin"]
    if sys.platform == "win32":
        local = Path.home() / "AppData" / "Local"
        winget = local / "Microsoft" / "WinGet"
        dirs.append(winget / "Links")
        packages = winget / "Packages"
        if packages.is_dir():
            for keyword in ("FFmpeg", "Deno"):
                for pkg in packages.glob(f"*{keyword}*"):
                    dirs.append(pkg)
                    dirs.extend(sorted(pkg.glob("*/bin")))
        dirs.append(local / "Programs" / "deno")
        dirs.append(Path("C:/ffmpeg/bin"))
        dirs.append(Path("C:/ProgramData/chocolatey/bin"))
    return dirs


def ensure_tools_on_path() -> list[str]:
    """把候補目錄補進本行程的 PATH，回傳實際新增的項目。

    yt-dlp 是靠 PATH 找 deno（YouTube 的 JS 挑戰需要），所以不能只靠
    find_ffmpeg 那種明確路徑傳遞。
    """
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    added = [
        str(d) for d in _fallback_dirs()
        if d.is_dir() and str(d) not in parts
    ]
    if added:
        os.environ["PATH"] = os.pathsep.join(added + parts)
    return added


def _find_tool(name: str) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    filenames = [f"{name}.exe", name] if sys.platform == "win32" else [name]
    for directory in _fallback_dirs():
        for filename in filenames:
            candidate = directory / filename
            if candidate.is_file():
                return str(candidate)
    return None


def find_ffmpeg() -> str | None:
    return _find_tool("ffmpeg")


def find_ffprobe() -> str | None:
    return _find_tool("ffprobe")


@functools.lru_cache(maxsize=1)
def available_encoders() -> tuple[str, ...]:
    """問 FFmpeg 有哪些硬體編碼器可用。

    只看它「有沒有編進去」，不代表這台機器的顯卡真的支援——
    實際能不能跑要等編碼失敗才知道，所以 processor 那邊還有一層退回機制。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return ("cpu",)
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        ).stdout
    except Exception:
        return ("cpu",)

    found = [name for name in HW_PREFERENCE if ENCODER_CODECS[name] in out]
    return tuple(found) + ("cpu",)


def resolve_encoder(choice: str, exclude: set[str] | None = None) -> str:
    """把使用者的選擇換算成實際要用的編碼器代號。

    exclude 用來排除本次執行已經證實跑不動的編碼器。
    """
    blocked = exclude or set()
    usable = [e for e in available_encoders() if e not in blocked]
    if not usable:
        return "cpu"
    if choice == "auto":
        return usable[0]
    return choice if choice in usable else "cpu"


def save_settings(data: dict) -> None:
    """把 GUI 設定寫成 JSON。寫檔失敗不該讓程式掛掉，所以吞掉例外。"""
    try:
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def load_settings() -> dict:
    """讀回上次的設定。檔案不存在或損毀都回傳空字典，讓 GUI 用預設值。"""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
