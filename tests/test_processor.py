"""build_filter / validate 的單元測試。

這兩個是純函式——輸入設定、輸出字串，不碰檔案也不開行程，
所以可以在沒有 FFmpeg 的環境下跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    FORMAT_SELECTORS,
    ClipSettings,
    has_template_field,
    validate_filename_template,
)
from processor import (  # noqa: E402
    build_filter,
    build_video_args,
    output_suffix,
    validate,
)

RATIO_916 = "9:16 (Shorts / Reels / TikTok)"
RATIO_11 = "1:1 (正方形)"


# ---------- build_filter ----------

def test_no_processing_passes_through():
    """兩個開關都關時要輸出 null 濾鏡，而不是空字串或壞掉的圖。"""
    assert build_filter(ClipSettings()) == "[0:v]null[v]"


def test_crop_only():
    f = build_filter(ClipSettings(enabled=True, mode="crop", crop_top=80))
    assert f == "[0:v]crop=trunc((iw-0-0)/2)*2:trunc((ih-80-0)/2)*2:0:80[v]"


def test_crop_four_sides_are_all_subtracted():
    f = build_filter(ClipSettings(
        enabled=True, mode="crop",
        crop_top=10, crop_bottom=20, crop_left=30, crop_right=40,
    ))
    assert "iw-30-40" in f
    assert "ih-10-20" in f
    assert f.endswith(":30:10[v]")


def test_crop_output_is_forced_even():
    """H.264 要求寬高為偶數，所以每個維度都要包 trunc(../2)*2。"""
    f = build_filter(ClipSettings(enabled=True, mode="crop", crop_top=45))
    assert f.count("trunc(") == 2
    assert f.count("/2)*2") == 2


def test_blur_splits_and_overlays_back():
    f = build_filter(ClipSettings(
        enabled=True, mode="blur", blur_x=100, blur_y=20, blur_w=240, blur_h=80,
    ))
    assert "split=2" in f
    assert "crop=240:80:100:20" in f
    assert "boxblur=12:1" in f
    assert "overlay=100:20" in f


def test_blur_strength_is_clamped_to_at_least_one():
    """boxblur 半徑 0 等於沒作用，使用者填 0 時當成 1 處理。"""
    f = build_filter(ClipSettings(enabled=True, mode="blur", blur_strength=0))
    assert "boxblur=1:1" in f


def test_delogo_uses_delogo_filter_not_boxblur():
    f = build_filter(ClipSettings(
        enabled=True, mode="delogo", blur_x=10, blur_y=20, blur_w=100, blur_h=50,
    ))
    assert f == "[0:v]delogo=x=10:y=20:w=100:h=50[v]"
    assert "boxblur" not in f


def test_vertical_only_crops_width_never_height():
    """高度必須原樣帶過，否則超長直式影片會被砍頭尾。"""
    f = build_filter(ClipSettings(vertical=True, vertical_ratio=RATIO_916))
    assert ":ih:" in f
    assert "0.562500" in f


def test_vertical_ratio_changes_the_expression():
    f916 = build_filter(ClipSettings(vertical=True, vertical_ratio=RATIO_916))
    f11 = build_filter(ClipSettings(vertical=True, vertical_ratio=RATIO_11))
    assert f916 != f11
    assert "1.000000" in f11


@pytest.mark.parametrize("anchor,expected", [
    ("center", "(in_w-out_w)/2"),
    ("left", ":0:0[v]"),
    ("right", "in_w-out_w"),
])
def test_vertical_anchor_positions(anchor, expected):
    f = build_filter(ClipSettings(
        vertical=True, vertical_ratio=RATIO_916, vertical_anchor=anchor,
    ))
    assert expected in f


def test_vertical_black_pads_instead_of_cropping():
    """黑邊填法必須保留完整畫面，不能出現任何 crop。"""
    f = build_filter(ClipSettings(
        vertical=True, vertical_ratio=RATIO_916, vertical_fill="black",
    ))
    assert f.startswith("[0:v]pad=")
    assert "crop" not in f
    assert ":black[v]" in f
    assert "(ow-iw)/2:(oh-ih)/2" in f


def test_vertical_blur_builds_background_and_overlays_original():
    f = build_filter(ClipSettings(
        vertical=True, vertical_ratio=RATIO_916, vertical_fill="blur",
        vertical_blur=25,
    ))
    assert "split=2" in f
    assert "force_original_aspect_ratio=increase" in f
    assert "gblur=sigma=25" in f
    assert "overlay=(W-w)/2:(H-h)/2[v]" in f


def test_vertical_blur_sigma_is_clamped():
    assert "gblur=sigma=1" in build_filter(ClipSettings(
        vertical=True, vertical_fill="blur", vertical_blur=0))
    assert "gblur=sigma=1024" in build_filter(ClipSettings(
        vertical=True, vertical_fill="blur", vertical_blur=99999))


def test_pad_canvas_rounds_up_to_even():
    """畫布比來源小 1px 的話 pad 會直接報錯，所以只能往上進位取偶數。"""
    f = build_filter(ClipSettings(vertical=True, vertical_fill="black"))
    assert f.count("ceil(") == 2
    assert "trunc(" not in f


def test_vertical_anchor_is_ignored_by_pad_fills():
    """補背景的填法一律置中，取景設定不該滲進濾鏡。"""
    for fill in ("black", "blur"):
        f = build_filter(ClipSettings(
            vertical=True, vertical_fill=fill, vertical_anchor="left",
        ))
        assert "in_w-out_w" not in f


def test_unknown_fill_falls_back_to_crop():
    f = build_filter(ClipSettings(vertical=True, vertical_fill="不存在的填法"))
    assert f.startswith("[0:v]crop=")


def test_pad_fills_chain_after_watermark_removal():
    """去水印的輸出要接進直式轉換，最後仍然只有一個 [v]。"""
    for fill in ("black", "blur"):
        f = build_filter(ClipSettings(
            enabled=True, mode="crop", crop_top=80,
            vertical=True, vertical_fill=fill,
        ))
        assert "[s0]" in f
        assert f.count("[v]") == 1
        assert f.endswith("[v]")


def test_vertical_pads_property():
    assert ClipSettings(vertical=True, vertical_fill="blur").vertical_pads is True
    assert ClipSettings(vertical=True, vertical_fill="black").vertical_pads is True
    assert ClipSettings(vertical=True, vertical_fill="crop").vertical_pads is False
    assert ClipSettings(vertical=False, vertical_fill="blur").vertical_pads is False


def test_unknown_ratio_falls_back_to_916():
    """設定檔被人手改壞時不該炸掉，退回預設比例即可。"""
    f = build_filter(ClipSettings(vertical=True, vertical_ratio="不存在的比例"))
    assert "0.562500" in f


def test_chained_filters_are_linked_in_order():
    """去水印的輸出要接到直式裁切的輸入，最後一段才命名為 [v]。"""
    f = build_filter(ClipSettings(
        enabled=True, mode="crop", crop_top=80,
        vertical=True, vertical_ratio=RATIO_916,
    ))
    assert f.count(";") == 1
    first, second = f.split(";")
    assert first.endswith("[s0]")
    assert second.startswith("[s0]")
    assert second.endswith("[v]")


def test_blur_chained_with_vertical_keeps_single_v_output():
    f = build_filter(ClipSettings(
        enabled=True, mode="blur",
        vertical=True, vertical_ratio=RATIO_916,
    ))
    assert f.count("[v]") == 1
    assert f.endswith("[v]")


def test_every_filter_graph_ends_with_v_label():
    """process() 固定 -map [v]，所以任何組合都必須產生這個標籤。"""
    combos = [
        ClipSettings(),
        ClipSettings(enabled=True, mode="crop", crop_top=1),
        ClipSettings(enabled=True, mode="blur"),
        ClipSettings(enabled=True, mode="delogo", blur_x=5, blur_y=5),
        ClipSettings(vertical=True),
        ClipSettings(enabled=True, mode="delogo", blur_x=5, blur_y=5, vertical=True),
    ]
    for s in combos:
        assert build_filter(s).endswith("[v]"), s


# ---------- validate ----------

def test_valid_settings_return_none():
    assert validate(ClipSettings(enabled=True, mode="crop", crop_top=80)) is None
    assert validate(ClipSettings(enabled=True, mode="blur")) is None
    assert validate(ClipSettings(vertical=True)) is None


def test_crop_with_all_zero_is_rejected():
    error = validate(ClipSettings(enabled=True, mode="crop", crop_top=0))
    assert error and "至少" in error


def test_negative_crop_is_rejected():
    error = validate(ClipSettings(enabled=True, mode="crop", crop_top=-5))
    assert error and "負數" in error


def test_zero_sized_region_is_rejected():
    assert validate(ClipSettings(enabled=True, mode="blur", blur_w=0)) is not None
    assert validate(ClipSettings(enabled=True, mode="blur", blur_h=0)) is not None


def test_delogo_touching_the_edge_is_rejected():
    """delogo 靠周圍像素插補，貼齊邊界時 FFmpeg 會直接報錯。"""
    error = validate(ClipSettings(enabled=True, mode="delogo", blur_x=0, blur_y=10))
    assert error is not None
    assert validate(ClipSettings(enabled=True, mode="delogo", blur_x=1, blur_y=1)) is None


def test_disabled_settings_skip_validation():
    """關閉去水印時，crop 參數再怎麼填都不該擋下使用者。"""
    assert validate(ClipSettings(enabled=False, mode="crop", crop_top=0)) is None


# ---------- needs_processing / uses_region ----------

def test_needs_processing_covers_both_switches():
    assert ClipSettings().needs_processing is False
    assert ClipSettings(enabled=True).needs_processing is True
    assert ClipSettings(vertical=True).needs_processing is True


def test_uses_region_is_true_for_blur_and_delogo_only():
    assert ClipSettings(mode="blur").uses_region is True
    assert ClipSettings(mode="delogo").uses_region is True
    assert ClipSettings(mode="crop").uses_region is False


# ---------- 檔名樣板 ----------

@pytest.mark.parametrize("template", [
    "%(title)s",
    "%(uploader)s - %(title)s",
    "%(upload_date)s_%(id)s",
    "我的影片 %(autonumber)03d",
    "%(title).60s",
    "固定檔名",
])
def test_valid_templates_pass(template):
    assert validate_filename_template(template) is None


def test_empty_template_is_rejected():
    assert validate_filename_template("   ") is not None


@pytest.mark.parametrize("template", [
    "影片/%(title)s",       # 路徑分隔符號會跑到別的資料夾
    "C:\\out\\%(title)s",
    "%(title)s?",
    "a<b>c",
])
def test_templates_with_illegal_chars_are_rejected(template):
    assert validate_filename_template(template) is not None


def test_field_syntax_is_not_mistaken_for_illegal_chars():
    """欄位裡的冒號等字元是樣板語法，不該被當成非法檔名字元。"""
    assert validate_filename_template("%(upload_date>%Y:%m:%d)s") is None


def test_stray_percent_is_rejected():
    """落單的 % 會讓 yt-dlp 解析樣板時直接丟例外，先擋下來。"""
    assert validate_filename_template("100% 純手工") is not None
    assert validate_filename_template("100%% 純手工") is None


def test_template_with_extension_is_rejected():
    error = validate_filename_template("%(title)s.mp4")
    assert error and "副檔名" in error


def test_has_template_field_detects_fixed_names():
    assert has_template_field("%(title)s") is True
    assert has_template_field("第 %(autonumber)03d 集") is True
    assert has_template_field("固定檔名") is False
    assert has_template_field("100% 純手工") is False


# ---------- 容器格式 ----------

def test_every_format_has_a_selector():
    for container in ("mp4", "mkv", "webm"):
        assert FORMAT_SELECTORS[container]


def test_selectors_all_have_a_fallback_branch():
    """挑不到指定格式的分軌時要能退回最佳畫質，不然乾脆下載失敗。"""
    for selector in FORMAT_SELECTORS.values():
        assert selector.endswith("/best")


@pytest.mark.parametrize("name,expected", [
    ("a.mp4", ".mp4"),
    ("a.mkv", ".mkv"),
    ("a.MKV", ".mkv"),
    ("a.mov", ".mov"),
    ("a.webm", ".mp4"),   # webm 裝不下 H.264
    ("a.flv", ".mp4"),
])
def test_output_suffix_avoids_incompatible_containers(name, expected):
    assert output_suffix(Path(name)) == expected


# ---------- build_video_args ----------

@pytest.mark.parametrize("encoder,codec", [
    ("cpu", "libx264"),
    ("nvenc", "h264_nvenc"),
    ("qsv", "h264_qsv"),
    ("amf", "h264_amf"),
])
def test_each_encoder_selects_its_codec(encoder, codec):
    args = build_video_args(encoder, 18)
    assert args[0] == "-c:v"
    assert args[1] == codec


def test_quality_is_clamped_to_valid_range():
    """CRF/CQ 合法範圍是 0-51，超出會讓 FFmpeg 直接拒絕。"""
    assert "51" in build_video_args("cpu", 999)
    assert "0" in build_video_args("cpu", -10)


def test_unknown_encoder_falls_back_to_cpu():
    assert build_video_args("神奇加速卡", 18)[1] == "libx264"
