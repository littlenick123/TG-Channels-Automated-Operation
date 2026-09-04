from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.config import ChannelGroupConfig
from channel_operator.database import StateDatabase
from channel_operator.models import DeliveryReceipt, MessageSnapshot, VideoInfo
from channel_operator.runner import MultiChannelRunner, format_duration
from channel_operator.telegram import ChannelGroupUnavailable


class FakeReporter:
    def __init__(self):
        self.messages = []

    async def send(self, text, *, strict=False):
        self.messages.append(text)
        return True


class BoundGateway:
    def __init__(self, group, events, *, unavailable=False):
        self.group = group
        self.events = events
        self.unavailable = unavailable

    async def doctor(self):
        self.events.append(f"doctor:{self.group.name}")
        if self.unavailable:
            raise ChannelGroupUnavailable("频道已被封禁")
        return {"group": self.group.name}

    async def scan_messages(self, min_id):
        self.events.append(f"scan:{self.group.name}")
        if min_id < 1:
            yield MessageSnapshot(
                message_id=1,
                grouped_id=100,
                caption="标签：#有效\n简介：测试",
                is_video=True,
                is_photo=False,
                width=1920,
                height=1080,
                duration=180,
                file_size=100,
                published_at=datetime.now(UTC) - timedelta(hours=1),
            )

    async def download_video(self, message_id, destination):
        self.events.append(f"download:{self.group.name}")
        destination.write_bytes(b"source")
        return destination

    async def send_staging_album(
        self,
        files,
        caption_html,
        route_id,
        upload_started_at,
        *,
        video_info,
        thumbnail,
    ):
        return DeliveryReceipt((1, 2, 3, 4), 999)

    async def find_matching_staging_album(self, started_at, route_id):
        return None

    async def copy_album(self, staging_message_ids, delivery_started_at):
        self.events.append(f"send:{self.group.name}")
        return DeliveryReceipt((11, 12, 13, 14), 1999)

    async def recover_delivery(self, staging_message_ids, delivery_started_at):
        return None

    async def apply_caption(self, receipt, caption_html, caption_plain):
        return None

    async def find_matching_album(self, started_at, caption_plain):
        return None


class GatewayFactory:
    def __init__(self, events, unavailable=()):
        self.events = events
        self.unavailable = set(unavailable)

    def for_group(self, group):
        return BoundGateway(
            group,
            self.events,
            unavailable=group.name in self.unavailable,
        )


class FakeMedia:
    def __init__(self, config, events):
        self.config = config
        self.events = events

    def check_disk(self, source_size):
        return None

    async def probe(self, path):
        return VideoInfo(path, 180, 1920, 1080, has_audio=True)

    def validate_source(self, info):
        return None

    async def cut_first_third(self, source, destination, info):
        destination.write_bytes(b"clipped")
        return VideoInfo(destination, 60, 1920, 1080, has_audio=True)

    async def transcode(self, source, destination, info, *, watermark_text=""):
        group_name = destination.parent.parent.name
        self.events.append(f"transcode:{group_name}")
        destination.write_bytes(b"video")
        return VideoInfo(destination, 60, 1280, 720, has_audio=True)

    async def screenshots(self, video, duration, directory):
        paths = []
        for index in range(1, 4):
            path = directory / f"frame_{index}.jpg"
            path.write_bytes(b"image")
            paths.append(path)
        return paths

    async def thumbnail(self, video, duration, destination):
        destination.write_bytes(b"thumb")
        return destination


def two_group_config(app_config):
    config = app_config(daily_success_count=1, max_candidates_per_run=2)
    first = replace(
        config.channel_groups[0], name="channel_b", remark="欧美中文字幕"
    )
    second = ChannelGroupConfig(
        name="channel_c",
        source_channel=first.source_channel,
        target_channel=-100333,
        database_path=first.database_path.with_name("channel_c.db"),
        daily_success_count=1,
        remark="欧美精选",
    )
    return replace(config, channel_groups=(first, second))


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0秒"),
        (59.9, "59秒"),
        (60, "1分钟0秒"),
        (3_723, "1小时2分钟3秒"),
        (93_784, "1天2小时3分钟4秒"),
        (-1, "0秒"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.asyncio
async def test_runner_processes_groups_strictly_in_configuration_order(app_config):
    config = two_group_config(app_config)
    events = []
    reporter = FakeReporter()
    clock_values = iter((100.0, 3_823.0))
    runner = MultiChannelRunner(
        config,
        GatewayFactory(events),
        FakeMedia(config, events),
        reporter,
        clock=lambda: next(clock_values),
    )

    results = await runner.run_once(config.channel_groups)

    assert all(result.succeeded for result in results)
    assert events.index("send:channel_b") < events.index("doctor:channel_c")
    assert events == [
        "doctor:channel_b",
        "scan:channel_b",
        "download:channel_b",
        "transcode:channel_b",
        "send:channel_b",
        "doctor:channel_c",
        "download:channel_c",
        "transcode:channel_c",
        "send:channel_c",
    ]
    assert "所有频道组总耗时：1小时2分钟3秒" in reporter.messages[-1]
    assert "[channel_b] 完成\n备注：欧美中文字幕" in reporter.messages[-1]
    assert "[channel_c] 完成\n备注：欧美精选" in reporter.messages[-1]
    for group in config.channel_groups:
        database = StateDatabase(group.database_path)
        assert database.published_count(str(group.source_channel), results[0].summary.run_date) == 1
        database.close()


@pytest.mark.asyncio
async def test_unavailable_group_is_reported_skipped_and_recovers_next_run(app_config):
    config = two_group_config(app_config)
    events = []
    reporter = FakeReporter()
    first_runner = MultiChannelRunner(
        config,
        GatewayFactory(events, unavailable={"channel_b"}),
        FakeMedia(config, events),
        reporter,
    )

    first_results = await first_runner.run_once(config.channel_groups)

    assert first_results[0].skipped_reason is not None
    assert first_results[1].succeeded is True
    assert "scan:channel_b" not in events
    assert "send:channel_c" in events
    assert any(
        "频道组已跳过" in message
        and "组名：channel_b" in message
        and "备注：欧美中文字幕" in message
        for message in reporter.messages
    )
    assert "channel_b" in reporter.messages[-1]
    assert "channel_c" in reporter.messages[-1]

    recovery_events = []
    recovery_runner = MultiChannelRunner(
        config,
        GatewayFactory(recovery_events),
        FakeMedia(config, recovery_events),
        reporter,
    )
    recovery_results = await recovery_runner.run_once(config.channel_groups)

    assert recovery_results[0].succeeded is True
    assert "send:channel_b" in recovery_events


@pytest.mark.asyncio
async def test_continuous_mode_pauses_unavailable_group_until_runner_restarts(app_config):
    config = two_group_config(app_config)
    events = []
    reporter = FakeReporter()
    paused_groups = {}
    runner = MultiChannelRunner(
        config,
        GatewayFactory(events, unavailable={"channel_b"}),
        FakeMedia(config, events),
        reporter,
    )

    await runner.run_once(
        config.channel_groups,
        continuous=True,
        send_summary=False,
        paused_groups=paused_groups,
    )
    first_doctor_count = events.count("doctor:channel_b")
    first_alert_count = sum("频道组已跳过" in message for message in reporter.messages)
    await runner.run_once(
        config.channel_groups,
        continuous=True,
        send_summary=False,
        paused_groups=paused_groups,
    )

    assert "channel_b" in paused_groups
    assert first_doctor_count == 1
    assert events.count("doctor:channel_b") == 1
    assert first_alert_count == 1
    assert sum("频道组已跳过" in message for message in reporter.messages) == 1


@pytest.mark.asyncio
async def test_continuous_mode_does_not_pause_temporary_group_error(app_config):
    config = two_group_config(app_config)
    events = []
    paused_groups = {}

    class TemporaryGateway(BoundGateway):
        async def doctor(self):
            self.events.append(f"doctor:{self.group.name}")
            if self.group.name == "channel_b":
                raise ConnectionError("temporary network error")
            return {"group": self.group.name}

    class TemporaryFactory:
        def for_group(self, group):
            return TemporaryGateway(group, events)

    runner = MultiChannelRunner(
        config,
        TemporaryFactory(),
        FakeMedia(config, events),
        FakeReporter(),
    )

    await runner.run_once(
        config.channel_groups,
        continuous=True,
        send_summary=False,
        paused_groups=paused_groups,
    )
    await runner.run_once(
        config.channel_groups,
        continuous=True,
        send_summary=False,
        paused_groups=paused_groups,
    )

    assert "channel_b" not in paused_groups
    assert events.count("doctor:channel_b") == 2


@pytest.mark.asyncio
async def test_continuous_cycle_sends_one_summary_after_all_groups(app_config):
    config = two_group_config(app_config)
    events = []
    reporter = FakeReporter()
    clock_values = iter((100.0, 3_823.0))
    completed_at = datetime(2026, 8, 12, 8, 9, 10, tzinfo=config.timezone)
    runner = MultiChannelRunner(
        config,
        GatewayFactory(events),
        FakeMedia(config, events),
        reporter,
        clock=lambda: next(clock_values),
        now=lambda: completed_at,
    )

    results = await runner.run_once(
        config.channel_groups,
        continuous=True,
    )

    assert all(result.succeeded for result in results)
    assert len(reporter.messages) == 1
    message = reporter.messages[0]
    assert "Telegram 多频道连续运营本轮汇总" in message
    assert "本轮结束：2026-08-12 08:09:10+08:00" in message
    assert "所有频道组总耗时：1小时2分钟3秒" in message
    assert "[channel_b] 完成\n备注：欧美中文字幕" in message
    assert "[channel_c] 完成\n备注：欧美精选" in message
    assert events.index("send:channel_b") < events.index("doctor:channel_c")


@pytest.mark.asyncio
async def test_continuous_zero_success_cycle_still_sends_summary(app_config):
    config = two_group_config(app_config)
    reporter = FakeReporter()
    clock_values = iter((10.0, 20.0))
    paused_groups = {
        config.channel_groups[0].name: "ChannelGroupUnavailable: 频道被封",
        config.channel_groups[1].name: "DatabaseError: 数据库损坏",
    }
    runner = MultiChannelRunner(
        config,
        GatewayFactory([]),
        FakeMedia(config, []),
        reporter,
        clock=lambda: next(clock_values),
        now=lambda: datetime(2026, 8, 12, 8, 0, tzinfo=config.timezone),
    )

    results = await runner.run_once(
        config.channel_groups,
        continuous=True,
        paused_groups=paused_groups,
    )

    assert sum(result.published for result in results) == 0
    assert len(reporter.messages) == 1
    message = reporter.messages[0]
    assert "[channel_b] 已跳过" in message
    assert "成功：0/1\n本次尝试：0" in message
    assert "永久跳过：0\n可重试失败：0\n恢复确认：0" in message
    assert "原因：ChannelGroupUnavailable: 频道被封" in message
    assert "[channel_c] 已跳过" in message
    assert "原因：DatabaseError: 数据库损坏" in message


@pytest.mark.asyncio
async def test_failed_continuous_cycle_report_does_not_fail_or_repeat_cycle(app_config):
    config = two_group_config(app_config)

    class FailingReporter(FakeReporter):
        async def send(self, text, *, strict=False):
            self.messages.append(text)
            return False

    reporter = FailingReporter()
    runner = MultiChannelRunner(
        config,
        GatewayFactory([]),
        FakeMedia(config, []),
        reporter,
        clock=lambda: 100.0,
        now=lambda: datetime(2026, 8, 12, 8, 0, tzinfo=config.timezone),
    )

    results = await runner.run_once(
        config.channel_groups,
        continuous=True,
    )

    assert all(result.succeeded for result in results)
    assert len(reporter.messages) == 1
