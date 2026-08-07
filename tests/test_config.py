from __future__ import annotations

from pathlib import Path

import pytest

from channel_operator.config import ConfigError, load_config

BASE_CONFIG = """
[telegram]
source_channel = -1001
target_channel = -1002
[content]
keep_tags = ["#保留"]
drop_tags = ["#删除"]
[schedule]
daily_time = "00:01"
[processing]
ffmpeg_threads = 3
[runtime]
database_path = "./data/state.db"
work_dir = "./work"
"""


def test_load_config_resolves_paths_and_lists(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")

    config = load_config(path)

    assert config.keep_tags == ("#保留",)
    assert config.daily_time == "00:01"
    assert config.database_path == (tmp_path / "data/state.db").resolve()


def test_overlapping_tag_lists_fail_fast(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG.replace("#删除", "保留"), encoding="utf-8")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")

    with pytest.raises(ConfigError, match="不能重叠"):
        load_config(path)
