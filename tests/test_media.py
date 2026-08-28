from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from channel_operator.media import THUMBNAIL_MAX_BYTES, MediaProcessor


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
    output = tmp_path / "video.mp4"
    output_info = await processor.transcode(source, output, source_info)
    frames = await processor.screenshots(output, output_info.duration, tmp_path)
    thumbnail = await processor.thumbnail(
        output, output_info.duration, tmp_path / "video_thumb.jpg"
    )

    assert (output_info.width, output_info.height) == expected_size
    assert len(frames) == 3
    assert all(frame.exists() and frame.stat().st_size > 0 for frame in frames)
    assert 0 < thumbnail.stat().st_size <= THUMBNAIL_MAX_BYTES
