from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from channel_operator.config import (
    AppConfig,
    ChannelGroupConfig,
    ReportingConfig,
)


@pytest.fixture
def app_config(tmp_path: Path):
    group = ChannelGroupConfig(
        name="test_group",
        source_channel=-100111,
        target_channel=-100222,
        database_path=tmp_path / "data" / "operator.db",
        daily_success_count=4,
    )
    config = AppConfig(
        api_id=12345,
        api_hash="test-hash",
        phone="+8613800000000",
        session_path=tmp_path / "data" / "session",
        channel_groups=(group,),
        reporting=ReportingConfig(
            "123456:test-token", (123456789,), "测试服务器"
        ),
        keep_tags=("#必留",),
        drop_tags=("#删除",),
        caption_limit=1024,
        intro_footer="",
        timezone=ZoneInfo("Asia/Shanghai"),
        daily_time="00:01",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        ffmpeg_threads=1,
        crf=23,
        preset="ultrafast",
        audio_bitrate="128k",
        minimum_source_short_edge=1080,
        album_settle_seconds=0,
        disk_reserve_bytes=0,
        database_dir=tmp_path / "data",
        work_dir=tmp_path / "work",
        max_candidates_per_run=12,
        max_runtime_hours=1,
        download_concurrency=4,
        download_stall_timeout_seconds=120,
        download_low_speed_window_seconds=60,
        download_low_speed_limit_kib_per_second=800,
        flood_sleep_threshold_seconds=60,
        retry_delays_seconds=(0,),
    )

    def factory(**changes):
        group_fields = {
            key: changes.pop(key)
            for key in (
                "name",
                "source_channel",
                "target_channel",
                "database_path",
                "daily_success_count",
                "remark",
            )
            if key in changes
        }
        selected_group = replace(group, **group_fields)
        return replace(config, channel_groups=(selected_group,), **changes)

    return factory
