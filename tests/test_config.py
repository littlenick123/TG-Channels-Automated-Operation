from __future__ import annotations

from pathlib import Path

import pytest

from channel_operator.config import ConfigError, load_config

BASE_CONFIG = """
[content]
keep_tags = ["#保留"]
drop_tags = ["#删除"]
[schedule]
daily_time = "00:01"
[reporting]
server_name = "德国-G12"
chat_ids = [123456789, 987654321]
[delivery]
staging_channel = -100999
[processing]
ffmpeg_threads = 3
[runtime]
database_dir = "./data"
work_dir = "./work"
[[channel_groups]]
name = "channel_b"
remark = "欧美中文字幕"
source_channel = -1001
target_channel = -1002
daily_success_count = 4
"""


def test_load_config_resolves_paths_and_lists(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    config = load_config(path)

    assert config.keep_tags == ("#保留",)
    assert config.schedule_mode == "daily"
    assert config.daily_time == "00:01"
    assert config.continuous_idle_seconds == 300
    assert [group.name for group in config.channel_groups] == ["channel_b"]
    assert config.channel_groups[0].remark == "欧美中文字幕"
    assert config.channel_groups[0].intro_footer is None
    assert config.channel_groups[0].display_name == "channel_b（欧美中文字幕）"
    assert config.database_dir == (tmp_path / "data").resolve()
    assert config.channel_groups[0].database_path == (
        tmp_path / "data/channel_b.db"
    ).resolve()
    assert config.channel_groups[0].daily_success_count == 4
    assert config.reporting.chat_ids == (123456789, 987654321)
    assert config.reporting.server_name == "德国-G12"
    assert config.delivery.staging_channel == -100999
    assert config.delivery.bot_session_path == (
        tmp_path / "data/telegram-bot"
    ).resolve()
    assert config.download_concurrency == 4
    assert config.download_stall_timeout_seconds == 120
    assert config.download_low_speed_window_seconds == 60
    assert config.download_low_speed_limit_kib_per_second == 800
    assert config.flood_sleep_threshold_seconds == 60
    assert config.retry_delays_seconds == (30, 120, 360)
    assert config.intro_footer == ""
    assert config.output_height == 720
    assert config.watermark_text == ""
    assert config.channel_groups[0].watermark_text is None


def test_channel_group_remark_is_optional_and_does_not_change_database_name(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace('remark = "欧美中文字幕"\n', ""),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    group = load_config(path).channel_groups[0]

    assert group.remark == ""
    assert group.display_name == "channel_b"
    assert group.database_path.name == "channel_b.db"


@pytest.mark.parametrize(
    ("remark_value", "message"),
    [
        ('"第一行\\n第二行"', "必须是单行文本"),
        ("123", "必须是字符串"),
        (f'"{"中" * 101}"', "不能超过 100 个字符"),
    ],
)
def test_channel_group_remark_is_validated(
    remark_value: str,
    message: str,
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace('remark = "欧美中文字幕"', f"remark = {remark_value}"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_intro_footer_is_loaded_and_trimmed(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'drop_tags = ["#删除"]',
            'drop_tags = ["#删除"]\nintro_footer = "  固定追加内容  "',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    assert load_config(path).intro_footer == "固定追加内容"


def test_intro_footer_must_be_one_line(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'drop_tags = ["#删除"]',
            'drop_tags = ["#删除"]\nintro_footer = "第一行\\n第二行"',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="intro_footer 必须是单行文本"):
        load_config(path)


def test_channel_group_intro_footer_is_loaded_and_trimmed(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'remark = "欧美中文字幕"',
            'remark = "欧美中文字幕"\nintro_footer = "  频道专属内容  "',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    assert load_config(path).channel_groups[0].intro_footer == "频道专属内容"


@pytest.mark.parametrize(
    ("footer_value", "message"),
    [
        ('"第一行\\n第二行"', "必须是单行文本"),
        ("123", "必须是字符串"),
    ],
)
def test_channel_group_intro_footer_is_validated(
    footer_value: str,
    message: str,
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'remark = "欧美中文字幕"',
            f'remark = "欧美中文字幕"\nintro_footer = {footer_value}',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_keep_tags_accepts_large_priority_library(tmp_path: Path, monkeypatch):
    tags = ", ".join(f'"#标签{index}"' for index in range(50))
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace('keep_tags = ["#保留"]', f"keep_tags = [{tags}]"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    config = load_config(path)

    assert len(config.keep_tags) == 50
    assert config.keep_tags[0] == "#标签0"
    assert config.keep_tags[-1] == "#标签49"


def test_overlapping_tag_lists_fail_fast(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG.replace("#删除", "保留"), encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="不能重叠"):
        load_config(path)


def test_flood_sleep_threshold_must_be_positive(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'work_dir = "./work"',
            'work_dir = "./work"\nflood_sleep_threshold_seconds = 0',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="flood_sleep_threshold_seconds"):
        load_config(path)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (143, "144 到 2160"),
        (2162, "144 到 2160"),
        (481, "必须是偶数"),
        ('"invalid"', "必须是整数"),
    ],
)
def test_output_height_is_validated(
    value: int | str,
    message: str,
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            "ffmpeg_threads = 3",
            f"ffmpeg_threads = 3\noutput_height = {value}",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_output_height_is_loaded(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            "ffmpeg_threads = 3",
            "ffmpeg_threads = 3\noutput_height = 480",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    assert load_config(path).output_height == 480


def test_watermark_config_and_group_override_are_loaded(tmp_path: Path, monkeypatch):
    font = tmp_path / "font.ttc"
    font.write_bytes(b"test font placeholder")
    path = tmp_path / "config.toml"
    configured = BASE_CONFIG.replace(
        "ffmpeg_threads = 3",
        "ffmpeg_threads = 3\n"
        'watermark_text = "  全局水印  "\n'
        f'watermark_font_file = "{font.as_posix()}"',
    ).replace(
        'remark = "欧美中文字幕"',
        'remark = "欧美中文字幕"\nwatermark_text = "  频道水印  "',
    )
    path.write_text(configured, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    config = load_config(path)

    assert config.watermark_text == "全局水印"
    assert config.watermark_font_file == font
    assert config.channel_groups[0].watermark_text == "频道水印"


@pytest.mark.parametrize("scope", ["processing", "channel_group"])
def test_watermark_text_must_be_one_line(scope: str, tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    if scope == "processing":
        configured = BASE_CONFIG.replace(
            "ffmpeg_threads = 3",
            'ffmpeg_threads = 3\nwatermark_text = "第一行\\n第二行"',
        )
    else:
        configured = BASE_CONFIG.replace(
            'remark = "欧美中文字幕"',
            'remark = "欧美中文字幕"\nwatermark_text = "第一行\\n第二行"',
        )
    path.write_text(configured, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="watermark_text 必须是单行文本"):
        load_config(path)


def test_enabled_watermark_requires_existing_font(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            "ffmpeg_threads = 3",
            'ffmpeg_threads = 3\nwatermark_text = "测试水印"\n'
            'watermark_font_file = "missing-font.ttc"',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="视频水印字体文件不存在"):
        load_config(path)


def test_group_can_disable_global_watermark_without_font(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    configured = BASE_CONFIG.replace(
        "ffmpeg_threads = 3",
        'ffmpeg_threads = 3\nwatermark_text = "全局水印"\n'
        'watermark_font_file = "missing-font.ttc"',
    ).replace(
        'remark = "欧美中文字幕"',
        'remark = "欧美中文字幕"\nwatermark_text = ""',
    )
    path.write_text(configured, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    config = load_config(path)

    assert config.channel_groups[0].watermark_text == ""


@pytest.mark.parametrize("value", [0, 9])
def test_download_concurrency_must_be_between_one_and_eight(
    value: int, tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'work_dir = "./work"',
            f'work_dir = "./work"\ndownload_concurrency = {value}',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="download_concurrency"):
        load_config(path)


@pytest.mark.parametrize("value", [0, 3601])
def test_download_stall_timeout_must_be_between_one_and_3600_seconds(
    value: int, tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'work_dir = "./work"',
            f'work_dir = "./work"\ndownload_stall_timeout_seconds = {value}',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="download_stall_timeout_seconds"):
        load_config(path)


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("download_low_speed_window_seconds", 0, "window_seconds"),
        ("download_low_speed_window_seconds", 3601, "window_seconds"),
        ("download_low_speed_limit_kib_per_second", 0, "limit_kib_per_second"),
    ],
)
def test_download_low_speed_settings_must_be_positive(
    setting: str,
    value: int,
    message: str,
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'work_dir = "./work"',
            f'work_dir = "./work"\n{setting} = {value}',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_stall_timeout_cannot_be_shorter_than_low_speed_window(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'work_dir = "./work"',
            'work_dir = "./work"\n'
            "download_stall_timeout_seconds = 30\n"
            "download_low_speed_window_seconds = 60",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="不能小于"):
        load_config(path)


def test_channel_group_order_is_preserved_and_database_names_are_automatic(
    tmp_path: Path, monkeypatch
):
    second_group = """
[[channel_groups]]
name = "channel_c"
source_channel = -1001
target_channel = -1003
daily_success_count = 2
"""
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG + second_group, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    config = load_config(path)

    assert [group.name for group in config.channel_groups] == ["channel_b", "channel_c"]
    assert [group.database_path.name for group in config.channel_groups] == [
        "channel_b.db",
        "channel_c.db",
    ]


@pytest.mark.parametrize("mode", ["daily", "continuous"])
def test_schedule_mode_is_loaded(mode: str, tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'daily_time = "00:01"',
            f'mode = "{mode}"\ndaily_time = "00:01"\ncontinuous_idle_seconds = 60',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    config = load_config(path)

    assert config.schedule_mode == mode
    assert config.continuous_idle_seconds == 60


def test_invalid_schedule_mode_is_rejected(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'daily_time = "00:01"', 'mode = "loop"\ndaily_time = "00:01"'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="daily 或 continuous"):
        load_config(path)


@pytest.mark.parametrize("value", [0, 86_401, '"invalid"'])
def test_continuous_idle_seconds_is_validated(
    value: int | str, tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'daily_time = "00:01"',
            f'daily_time = "00:01"\ncontinuous_idle_seconds = {value}',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="continuous_idle_seconds"):
        load_config(path)


def test_cycle_success_count_is_rejected(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            "daily_success_count = 4",
            "daily_success_count = 4\ncycle_success_count = 2",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="不支持 cycle_success_count"):
        load_config(path)


def test_per_group_database_path_is_rejected(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'target_channel = -1002',
            'target_channel = -1002\ndatabase_path = "./legacy.db"',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="不再使用 database_path"):
        load_config(path)


@pytest.mark.parametrize("value", [0, 1025])
def test_caption_limit_must_be_between_one_and_1024(
    value: int, tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            'drop_tags = ["#删除"]',
            f'drop_tags = ["#删除"]\ncaption_limit = {value}',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="caption_limit"):
        load_config(path)


def test_legacy_single_channel_config_is_rejected(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "[telegram]\nsource_channel=-1001\ntarget_channel=-1002\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="旧版单频道配置"):
        load_config(path)


def test_report_bot_token_is_required(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.delenv("TG_REPORT_BOT_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="TG_REPORT_BOT_TOKEN"):
        load_config(path)


def test_staging_channel_is_required(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace("[delivery]\nstaging_channel = -100999\n", ""),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match="delivery.staging_channel"):
        load_config(path)


def test_bot_session_path_can_be_overridden(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TG_BOT_SESSION_PATH", "./sessions/delivery-bot")

    config = load_config(path)

    assert config.delivery.bot_session_path == (
        tmp_path / "sessions/delivery-bot"
    ).resolve()


def test_user_and_bot_sessions_must_use_different_paths(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TG_SESSION_PATH", "./data/shared")
    monkeypatch.setenv("TG_BOT_SESSION_PATH", "./data/shared")

    with pytest.raises(ConfigError, match="不能使用同一路径"):
        load_config(path)


def test_report_server_name_defaults_to_system_hostname(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace('server_name = "德国-G12"\n', ""),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")
    monkeypatch.setattr("channel_operator.config.socket.gethostname", lambda: "host-a")

    assert load_config(path).reporting.server_name == "host-a"


@pytest.mark.parametrize(
    ("server_name", "message"),
    [
        ('""', "不能为空"),
        ('"第一行\\n第二行"', "必须是单行文本"),
        ("123", "必须是字符串"),
        (f'"{"服" * 101}"', "不能超过 100 个字符"),
    ],
)
def test_report_server_name_is_validated(
    server_name: str,
    message: str,
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace('server_name = "德国-G12"', f"server_name = {server_name}"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_legacy_single_report_chat_id_is_supported(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace(
            "chat_ids = [123456789, 987654321]", "chat_id = 123456789"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    assert load_config(path).reporting.chat_ids == (123456789,)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("chat_ids = []", "非空整数数组"),
        ("chat_ids = [123, 123]", "不能包含重复值"),
        ("chat_ids = [123, -456]", "全部是正整数"),
        (
            "chat_id = 123\nchat_ids = [456]",
            "不能同时配置",
        ),
    ],
)
def test_report_chat_ids_validation(
    replacement: str, message: str, tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.toml"
    path.write_text(
        BASE_CONFIG.replace("chat_ids = [123456789, 987654321]", replacement),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_REPORT_BOT_TOKEN", "123:test")

    with pytest.raises(ConfigError, match=message):
        load_config(path)
