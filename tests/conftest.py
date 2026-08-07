from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from channel_operator.config import AppConfig


@pytest.fixture
def app_config(tmp_path: Path):
    config = AppConfig(
        api_id=12345,
        api_hash="test-hash",
        phone="+8613800000000",
        session_path=tmp_path / "data" / "session",
        source_channel=-100111,
        target_channel=-100222,
        keep_tags=("#必留",),
        drop_tags=("#删除",),
        caption_limit=1024,
        timezone=ZoneInfo("Asia/Shanghai"),
        daily_time="00:01",
        daily_success_count=4,
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        ffmpeg_threads=1,
        crf=23,
        preset="ultrafast",
        audio_bitrate="128k",
        minimum_source_short_edge=1080,
        album_settle_seconds=0,
        disk_reserve_bytes=0,
        database_path=tmp_path / "data" / "operator.db",
        work_dir=tmp_path / "work",
        max_candidates_per_run=12,
        max_runtime_hours=1,
        retry_delays_seconds=(0,),
        notify_saved_messages=False,
    )

    def factory(**changes):
        return replace(config, **changes)

    return factory
