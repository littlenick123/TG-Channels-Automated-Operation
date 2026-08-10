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
chat_ids = [123456789, 987654321]
[processing]
ffmpeg_threads = 3
[runtime]
database_dir = "./data"
work_dir = "./work"
[[channel_groups]]
name = "channel_b"
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
    assert config.daily_time == "00:01"
    assert [group.name for group in config.channel_groups] == ["channel_b"]
    assert config.database_dir == (tmp_path / "data").resolve()
    assert config.channel_groups[0].database_path == (
        tmp_path / "data/channel_b.db"
    ).resolve()
    assert config.channel_groups[0].daily_success_count == 4
    assert config.reporting.chat_ids == (123456789, 987654321)
    assert config.download_concurrency == 4
    assert config.flood_sleep_threshold_seconds == 60
    assert config.intro_footer == ""


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
