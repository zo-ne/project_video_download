"""程式進入點：python main.py"""

from __future__ import annotations

import sys
from pathlib import Path


def _check_dependencies() -> bool:
    missing = []
    try:
        import tkinter  # noqa: F401
    except ImportError:
        missing.append("tkinter（Windows 請重新安裝 Python 並勾選 tcl/tk；Linux 裝 python3-tk）")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append("yt-dlp（執行 pip install -r requirements.txt）")

    if missing:
        print("缺少必要套件：", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        # 套件是裝在特定直譯器底下的，換一個 Python 跑就會找不到，
        # 所以把實際用的直譯器印出來，省去猜測。
        print(f"\n目前的直譯器：{sys.executable}", file=sys.stderr)
        venv = Path(__file__).resolve().parent / ".venv" / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        if venv.exists() and Path(sys.executable) != venv:
            print(f"本專案的 venv：{venv}", file=sys.stderr)
            print("請改用上面這個直譯器執行。", file=sys.stderr)
        return False
    return True


def main() -> int:
    if not _check_dependencies():
        return 1

    from config import OUTPUT_DIR, ensure_tools_on_path

    # 先補 PATH 再載入 GUI，這樣啟動檢查看到的就是修正後的環境
    ensure_tools_on_path()
    from gui import main as run_gui

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
