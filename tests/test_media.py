from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from channel_operator.media import THUMBNAIL_MAX_BYTES, MediaProcessor

WATERMARK_FONT = next(
    (
        path
        for path in (
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
        )
        if path.is_file()
    ),
    None,
)


def test_screenshot_time_rules():
    assert MediaProcessor.thumbnail_time(200) == 20
    assert MediaProcessor.screenshot_times(200) == (30, 100, 170)
    assert MediaProcessor.thumbnail_time(100) == 10
    assert MediaProcessor.screenshot_times(100) == (15, 50, 85)


def test_transcode_bounds_follow_configured_output_height():
    assert MediaProcessor.transcode_bounds(720, landscape=True) == (1280, 720)
    assert MediaProcessor.transcode_bounds(720, landscape=False) == (720, 1280)
    assert MediaProcessor.transcode_bounds(480, landscape=True) == (854, 480)
    assert MediaProcessor.transcode_bounds(480, landscape=False) == (480, 854)


def test_first_third_duration():
    assert MediaProcessor.first_third_duration(90) == 30
    assert MediaProcessor.first_third_duration(10.5) == 3.5


@pytest.mark.skipif(WATERMARK_FONT is None, reason="没有可用的中日韩字体")
def test_watermark_size_scales_with_output_height_and_shrinks_long_text(app_config):
    processor_720 = MediaProcessor(
        app_config(output_height=720, watermark_font_file=WATERMARK_FONT)
    )
    processor_480 = MediaProcessor(
        app_config(output_height=480, watermark_font_file=WATERMARK_FONT)
    )

    assert processor_720.watermark_style("短水印", 1280) == (50, 4)
    assert processor_480.watermark_style("短水印", 854) == (34, 3)
    long_size, _ = processor_480.watermark_style("很长的水印文字" * 20, 854)
    assert long_size < 34


@pytest.mark.parametrize(
    ("output_height", "expected_size"),
    [(720, (1280, 720)), (480, (854, 480))],
)
@pytest.mark.asyncio
async def test_transcode_and_extract_three_frames(
    app_config,
    tmp_path: Path,
    output_height: int,
    expected_size: tuple[int, int],
):
    config = app_config(output_height=output_height)
    processor = MediaProcessor(config)
    source = tmp_path / "source.mp4"
    process = await asyncio.create_subprocess_exec(
        config.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=10:duration=2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(source),
    )
    assert await process.wait() == 0

    source_info = await processor.probe(source)
    processor.validate_source(source_info)
    clipped = tmp_path / "source_first_third.mkv"
    clipped_info = await processor.cut_first_third(source, clipped, source_info)
    output = tmp_path / "video.mp4"
    output_info = await processor.transcode(clipped, output, clipped_info)
    frames = await processor.screenshots(output, output_info.duration, tmp_path)
    thumbnail = await processor.thumbnail(
        output, output_info.duration, tmp_path / "video_thumb.jpg"
    )

    assert (output_info.width, output_info.height) == expected_size
    assert clipped_info.duration == pytest.approx(source_info.duration / 3, abs=0.25)
    assert len(frames) == 3
    assert all(frame.exists() and frame.stat().st_size > 0 for frame in frames)
    assert 0 < thumbnail.stat().st_size <= THUMBNAIL_MAX_BYTES


@pytest.mark.skipif(WATERMARK_FONT is None, reason="没有可用的中日韩字体")
@pytest.mark.asyncio
async def test_watermark_is_centered_only_during_last_ten_seconds(
    app_config,
    tmp_path: Path,
):
    config = app_config(output_height=360, watermark_font_file=WATERMARK_FONT)
    processor = MediaProcessor(config)
    source = tmp_path / "black.mp4"
    process = await asyncio.create_subprocess_exec(
        config.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:size=640x360:rate=5:duration=12",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(source),
    )
    assert await process.wait() == 0

    info = await processor.probe(source)
    output = tmp_path / "watermarked.mp4"
    await processor.transcode(
        source,
        output,
        info,
        watermark_text="测试 Watermark: 100% 'ok'",
    )
    before = tmp_path / "before.jpg"
    during = tmp_path / "during.jpg"
    for timestamp, destination in ((1, before), (6, during)):
        process = await asyncio.create_subprocess_exec(
            config.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(output),
            "-frames:v",
            "1",
            str(destination),
        )
        assert await process.wait() == 0

    with Image.open(before) as image:
        assert max(channel[1] for channel in image.convert("RGB").getextrema()) < 32
    with Image.open(during) as image:
        rgb = image.convert("RGB")
        assert max(channel[1] for channel in rgb.getextrema()) > 200
        watermark_bounds = rgb.convert("L").point(lambda value: value > 100).getbbox()
        assert watermark_bounds is not None
        left, top, right, bottom = watermark_bounds
        assert (left + right) / 2 == pytest.approx(rgb.width / 2, abs=5)
        assert (top + bottom) / 2 == pytest.approx(rgb.height / 2, abs=5)
    assert not (tmp_path / "watermark_text.txt").exists()
