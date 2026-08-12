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

    async def send_album(
        self,
        files,
        caption_html,
        caption_plain,
        upload_started_at,
        *,
        video_info,
        thumbnail,
    ):
        self.events.append(f"send:{self.group.name}")
        return DeliveryReceipt((1, 2, 3, 4), 999)

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

    async def transcode(self, source, destination, info):
        group_name = destination.parent.parent.name
        self.events.append(f"transcode:{group_name}")
        destination.write_bytes(b"video")
        return VideoInfo(destination, 180, 1280, 720, has_audio=True)

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
        "scan:channel_c",
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
async def test_continuous_daily_report_is_persistent_and_not_duplicated(app_config):
    config = two_group_config(app_config)
    reporter = FakeReporter()
    runner = MultiChannelRunner(
        config,
        GatewayFactory([]),
        FakeMedia(config, []),
        reporter,
    )

    initialized = await runner.send_due_continuous_reports(
        config.channel_groups,
        now=datetime(2026, 8, 12, 0, 1, tzinfo=config.timezone),
    )
    for index, group in enumerate(config.channel_groups, start=1):
        database = StateDatabase(group.database_path)
        database.record_daily_stats(
            "2026-08-12",
            published=index,
            attempted=index + 1,
            paused_reason="频道被封" if index == 1 else None,
        )
        database.close()

    sent = await runner.send_due_continuous_reports(
        config.channel_groups,
        now=datetime(2026, 8, 13, 0, 1, tzinfo=config.timezone),
    )
    repeated = await runner.send_due_continuous_reports(
        config.channel_groups,
        now=datetime(2026, 8, 13, 12, 0, tzinfo=config.timezone),
    )

    assert initialized == 0
    assert sent == 1
    assert repeated == 0
    assert len(reporter.messages) == 1
    assert "连续运营日报 2026-08-12" in reporter.messages[0]
    assert "备注：欧美中文字幕" in reporter.messages[0]
    assert "暂停原因：频道被封" in reporter.messages[0]


@pytest.mark.asyncio
async def test_continuous_report_isolates_broken_group_database(app_config):
    config = two_group_config(app_config)
    first, second = config.channel_groups
    first.database_path.parent.mkdir(parents=True, exist_ok=True)
    first.database_path.write_bytes(b"not a sqlite database")
    reporter = FakeReporter()
    runner = MultiChannelRunner(
        config,
        GatewayFactory([]),
        FakeMedia(config, []),
        reporter,
    )
    healthy = StateDatabase(second.database_path)
    healthy.continuous_report_cursor("2026-08-11")
    healthy.record_daily_stats("2026-08-12", published=1, attempted=1)
    healthy.close()

    sent = await runner.send_due_continuous_reports(
        config.channel_groups,
        now=datetime(2026, 8, 13, 0, 1, tzinfo=config.timezone),
        paused_groups={first.name: "DatabaseError: 数据库损坏"},
    )

    assert sent == 1
    assert "[channel_b]" in reporter.messages[0]
    assert "暂停原因：DatabaseError: 数据库损坏" in reporter.messages[0]
    assert "[channel_c]" in reporter.messages[0]
    assert "成功：1" in reporter.messages[0]


@pytest.mark.asyncio
async def test_failed_continuous_report_keeps_cursor_and_waits_before_retry(app_config):
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
    )
    for group in config.channel_groups:
        database = StateDatabase(group.database_path)
        database.continuous_report_cursor("2026-08-11")
        database.record_daily_stats("2026-08-12", published=1)
        database.close()

    first = await runner.send_due_continuous_reports(
        config.channel_groups,
        now=datetime(2026, 8, 13, 0, 1, tzinfo=config.timezone),
    )
    second = await runner.send_due_continuous_reports(
        config.channel_groups,
        now=datetime(2026, 8, 13, 0, 10, tzinfo=config.timezone),
    )

    assert first == 0
    assert second == 0
    assert len(reporter.messages) == 1
    for group in config.channel_groups:
        database = StateDatabase(group.database_path)
        assert database.continuous_report_cursor("2026-01-01") == "2026-08-11"
        database.close()
