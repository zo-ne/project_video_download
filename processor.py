"""FFmpeg 後製：邊緣裁切、區域模糊 / 去標誌、直式轉換。"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable

from config import (
    Cancelled,
    ClipSettings,
    ENCODER_CODECS,
    NO_WINDOW,
    VERTICAL_RATIOS,
    find_ffmpeg,
    find_ffprobe,
    resolve_encoder,
)

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[float, str], None]

# FFmpeg 有把某個硬體編碼器編進去，不代表這台機器的驅動跑得動
# （例如顯卡驅動版本太舊）。第一次實際失敗後就記住，批次處理時
# 不必每個檔案都再浪費一次嘗試。
_broken_encoders: set[str] = set()


def build_filter(s: ClipSettings) -> str:
    """依設定串起 filter_complex，輸出標籤固定為 [v]。

    去水印（crop / blur）先做，直式裁切後做，兩者可以疊加。
    """
    segments: list[str] = []
    label = "0:v"
    step = 0

    if s.enabled and s.mode == "crop":
        w = f"trunc((iw-{s.crop_left}-{s.crop_right})/2)*2"
        h = f"trunc((ih-{s.crop_top}-{s.crop_bottom})/2)*2"
        out = f"s{step}"
        segments.append(f"[{label}]crop={w}:{h}:{s.crop_left}:{s.crop_top}[{out}]")
        label, step = out, step + 1
    elif s.enabled and s.mode == "blur":
        # 切兩路，一路裁出水印區域做模糊，再疊回原畫面
        x, y, w, h = s.blur_x, s.blur_y, s.blur_w, s.blur_h
        strength = max(1, s.blur_strength)
        base, tmp, fg, out = f"b{step}", f"t{step}", f"f{step}", f"s{step}"
        segments.append(
            f"[{label}]split=2[{base}][{tmp}];"
            f"[{tmp}]crop={w}:{h}:{x}:{y},boxblur={strength}:1[{fg}];"
            f"[{base}][{fg}]overlay={x}:{y}[{out}]"
        )
        label, step = out, step + 1
    elif s.enabled and s.mode == "delogo":
        # delogo 用周圍像素往內插補，對半透明浮水印通常比整塊模糊自然
        out = f"s{step}"
        segments.append(
            f"[{label}]delogo=x={s.blur_x}:y={s.blur_y}:"
            f"w={s.blur_w}:h={s.blur_h}[{out}]"
        )
        label, step = out, step + 1

    if s.vertical:
        ratio = VERTICAL_RATIOS.get(s.vertical_ratio, 9 / 16)
        r = f"{ratio:.6f}"
        # 畫布 = 剛好裝得下整個來源、且符合目標比例的最小矩形。
        # 全部用 ffmpeg 運算式在執行期算，不必先探測解析度。
        # 取偶數時往上進位：畫布只要比來源小 1px，pad 就會直接報錯
        canvas_w = f"ceil(max(iw\\,ih*{r})/2)*2"
        canvas_h = f"ceil(max(ih\\,iw/{r})/2)*2"
        out = f"s{step}"

        if s.vertical_fill == "black":
            segments.append(
                f"[{label}]pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black[{out}]"
            )
        elif s.vertical_fill == "blur":
            # 背景：等比放大到蓋滿畫布再裁掉溢出的部分，然後高斯模糊。
            # 放大後畫面比例不變，所以「畫布」在它眼中就是 min(iw, ih*r) x min(ih, iw/r)。
            sigma = max(1, min(1024, s.vertical_blur))
            base, tmp, bg = f"b{step}", f"t{step}", f"g{step}"
            segments.append(
                f"[{label}]split=2[{base}][{tmp}];"
                f"[{tmp}]scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
                f"crop=trunc(min(iw\\,ih*{r})/2)*2:trunc(min(ih\\,iw/{r})/2)*2,"
                f"gblur=sigma={sigma}[{bg}];"
                f"[{bg}][{base}]overlay=(W-w)/2:(H-h)/2[{out}]"
            )
        else:
            # 裁切：只裁寬度，本來就是直式（夠窄）的來源會原樣通過
            width = f"trunc(if(gt(iw\\,ih*{r})\\,ih*{r}\\,iw)/2)*2"
            anchor = {
                "left": "0",
                "right": "in_w-out_w",
            }.get(s.vertical_anchor, "(in_w-out_w)/2")
            segments.append(f"[{label}]crop={width}:ih:{anchor}:0[{out}]")

        label, step = out, step + 1

    if not segments:
        return "[0:v]null[v]"

    # 最後一段的輸出標籤改名為 [v]
    segments[-1] = segments[-1].rsplit(f"[{label}]", 1)[0] + "[v]"
    return ";".join(segments)


def validate(s: ClipSettings) -> str | None:
    """回傳錯誤訊息，設定合法則回傳 None。"""
    if s.enabled and s.mode == "crop":
        if min(s.crop_top, s.crop_bottom, s.crop_left, s.crop_right) < 0:
            return "裁切像素不可為負數。"
        if s.crop_top + s.crop_bottom == 0 and s.crop_left + s.crop_right == 0:
            return "裁切模式至少要有一邊大於 0。"
    elif s.enabled and s.uses_region:
        name = "模糊" if s.mode == "blur" else "去標誌"
        if s.blur_w <= 0 or s.blur_h <= 0:
            return f"{name}區域的寬與高必須大於 0。"
        if s.blur_x < 0 or s.blur_y < 0:
            return f"{name}區域座標不可為負數。"
        # delogo 需要靠周圍像素插補，區域貼齊邊界時 FFmpeg 會直接報錯
        if s.mode == "delogo" and (s.blur_x < 1 or s.blur_y < 1):
            return "去標誌區域需與畫面邊緣至少相距 1 像素（改用區域模糊可貼邊）。"
    return None


def probe_duration(path: Path) -> float:
    """取得影片長度（秒），失敗回傳 0。"""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return 0.0
    cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_entries", "format=duration", str(path),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def build_video_args(encoder: str, quality: int) -> list[str]:
    """各編碼器的品質參數名稱不同，這裡統一從一個 0-51 的數值換算。"""
    q = max(0, min(51, quality))
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(q)]
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(q)]
    if encoder == "amf":
        return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)]
    # CPU 才明確指定 pix_fmt；硬體編碼器有自己偏好的格式，交給它決定
    return ["-c:v", "libx264", "-crf", str(q), "-preset", "veryfast",
            "-pix_fmt", "yuv420p"]


def _run_ffmpeg(
    cmd: list[str],
    duration: float,
    progress: ProgressFn,
    cancel_event: threading.Event,
) -> tuple[int, str]:
    """跑一次 FFmpeg 並回報進度，回傳 (return code, stderr)。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_event.is_set():
                proc.terminate()
                raise Cancelled()
            line = line.strip()
            if line.startswith("out_time_us=") and duration > 0:
                raw = line.split("=", 1)[1]
                if raw.isdigit():
                    pct = min(100.0, int(raw) / 1_000_000 / duration * 100)
                    progress(pct, f"剪輯中 {pct:5.1f}%")
            elif line == "progress=end":
                progress(100.0, "剪輯完成")
        proc.wait()
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        return proc.returncode, stderr
    finally:
        if proc.poll() is None:
            proc.terminate()


def process(
    src: Path,
    settings: ClipSettings,
    log: LogFn,
    progress: ProgressFn,
    cancel_event: threading.Event,
) -> Path:
    """對 src 執行剪輯，回傳最終檔案路徑。

    keep_original=True 時輸出 `<原檔名>_edited.mp4`，否則就地取代原檔。
    硬體編碼失敗會自動退回 CPU 再試一次。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，請安裝並加入 PATH，或放到專案的 bin/ 目錄。")

    tmp = src.with_name(f"{src.stem}.processing.mp4")
    final = (
        src.with_name(f"{src.stem}_edited.mp4") if settings.keep_original else src
    )

    duration = probe_duration(src)
    vf = build_filter(settings)
    log(f"FFmpeg 濾鏡：{vf}", "info")

    encoder = resolve_encoder(settings.encoder, exclude=_broken_encoders)
    # 「編進 FFmpeg」不等於「這台機器跑得動」，所以硬體編碼保留一次退回機會
    attempts = [encoder] if encoder == "cpu" else [encoder, "cpu"]

    try:
        for attempt in attempts:
            log(f"編碼器：{ENCODER_CODECS[attempt]}", "info")
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-filter_complex", vf,
                "-map", "[v]", "-map", "0:a?",
                *build_video_args(attempt, settings.quality),
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                str(tmp),
            ]
            code, stderr = _run_ffmpeg(cmd, duration, progress, cancel_event)
            if code == 0:
                break
            if attempt is attempts[-1]:
                raise RuntimeError(f"FFmpeg 失敗 (code {code})：{stderr[:500]}")
            _broken_encoders.add(attempt)
            log(
                f"{ENCODER_CODECS[attempt]} 在這台機器上無法使用，改用 CPU"
                f"（本次執行不再嘗試）：{stderr[:200]}",
                "warn",
            )
            tmp.unlink(missing_ok=True)

        # 成功才覆蓋目標檔，避免中途失敗留下半成品
        if final.exists() and final != src:
            final.unlink()
        if not settings.keep_original:
            src.unlink()
        tmp.replace(final)
        return final
    finally:
        tmp.unlink(missing_ok=True)
