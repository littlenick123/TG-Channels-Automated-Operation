from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel_operator.cli import _configure_logging, _parser, _run, _select_groups
from channel_operator.config import ChannelGroupConfig, ConfigError
from channel_operator.models import GroupRunResult, RunSummary


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


@pytest.mark.asyncio
async def test_continuous_run_once_enables_cycle_summary(app_config, monkeypatch):
    config = app_config(schedule_mode="continuous", daily_success_count=1)
    captured = {}

    class FakeTelegram:
        def __init__(self, loaded_config):
            assert loaded_config is config

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    class FakeDelivery(FakeTelegram):
        pass

    class FakeMedia:
        def __init__(self, loaded_config):
            assert loaded_config is config

    class FakeReporter:
        def __init__(self, reporting):
            assert reporting is config.reporting

        async def close(self):
            return None

    class FakeRunner:
        def __init__(
            self, loaded_config, telegram, media, reporter, *, delivery=None
        ):
            assert loaded_config is config
            assert delivery is not None

        async def run_once(self, groups, **kwargs):
            captured.update(kwargs)
            return [
                GroupRunResult(
                    group=groups[0],
                    summary=RunSummary(run_date="2026-08-16", published=1),
                )
            ]

    class FakeLock:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("channel_operator.cli.load_config", lambda path: config)
    monkeypatch.setattr("channel_operator.cli.TelegramGateway", FakeTelegram)
    monkeypatch.setattr("channel_operator.cli.BotDeliveryGateway", FakeDelivery)
    monkeypatch.setattr("channel_operator.cli.MediaProcessor", FakeMedia)
    monkeypatch.setattr("channel_operator.cli.BotReporter", FakeReporter)
    monkeypatch.setattr("channel_operator.cli.MultiChannelRunner", FakeRunner)
    monkeypatch.setattr("channel_operator.cli.ProcessLock", FakeLock)

    code = await _run(
        SimpleNamespace(
            config="config.toml",
            verbose=False,
            command="run-once",
            dry_run=False,
            group=None,
        )
    )

    assert code == 0
    assert captured == {"continuous": True, "send_summary": True}
