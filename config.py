"""共用設定與資料結構。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "影片下載 / 自動微剪輯工具"
APP_VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

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

    # 直式短影音裁切：只裁寬度，畫面比目標更寬時才動作
    vertical: bool = False
    vertical_ratio: str = "9:16"
    vertical_anchor: str = "center"  # center / left / right

    keep_original: bool = True

    @property
    def needs_processing(self) -> bool:
        """去水印與直式裁切各自獨立，任一開啟就要送進 FFmpeg。"""
        return self.enabled or self.vertical

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
