from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.config import ChannelGroupConfig
from channel_operator.database import StateDatabase
from channel_operator.models import DeliveryReceipt, MessageSnapshot, VideoInfo
from channel_operator.runner import MultiChannelRunner
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
    first = replace(config.channel_groups[0], name="channel_b")
    second = ChannelGroupConfig(
        name="channel_c",
        source_channel=first.source_channel,
        target_channel=-100333,
        database_path=first.database_path.with_name("channel_c.db"),
        daily_success_count=1,
    )
    return replace(config, channel_groups=(first, second))


@pytest.mark.asyncio
async def test_runner_processes_groups_strictly_in_configuration_order(app_config):
    config = two_group_config(app_config)
    events = []
    reporter = FakeReporter()
    runner = MultiChannelRunner(
        config,
        GatewayFactory(events),
        FakeMedia(config, events),
        reporter,
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
        "频道组已跳过" in message and "channel_b" in message
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
