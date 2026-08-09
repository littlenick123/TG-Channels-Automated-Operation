from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from channel_operator.cli import _configure_logging, _parser, _select_groups
from channel_operator.config import ChannelGroupConfig, ConfigError


def test_group_selector_preserves_order_and_rejects_unknown(app_config):
    config = app_config()
    first = replace(config.channel_groups[0], name="channel_b")
    second = ChannelGroupConfig(
        name="channel_c",
        source_channel=first.source_channel,
        target_channel=-100333,
        database_path=first.database_path.with_name("channel_c.db"),
        daily_success_count=2,
    )
    config = replace(config, channel_groups=(first, second))

    assert _select_groups(config, None) == (first, second)
    assert _select_groups(config, "channel_c") == (second,)
    with pytest.raises(ConfigError, match="未知频道组"):
        _select_groups(config, "missing")


def test_schedule_command_is_available():
    arguments = _parser().parse_args(["--config", "config.toml", "schedule"])

    assert arguments.command == "schedule"
    assert arguments.config == "config.toml"


def test_default_logging_hides_short_telethon_flood_wait_info():
    logger = logging.getLogger("telethon.client.users")
    original_level = logger.level
    try:
        _configure_logging(verbose=False)
        assert logger.level == logging.WARNING

        _configure_logging(verbose=True)
        assert logger.level == logging.NOTSET
    finally:
        logger.setLevel(original_level)
