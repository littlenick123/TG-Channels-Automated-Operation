from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.database import StateDatabase
from channel_operator.models import DeliveryReceipt, MessageSnapshot, VideoInfo
from channel_operator.service import AutomationService


class FakeTelegram:
    def __init__(self, snapshots: MessageSnapshot | list[MessageSnapshot]):
        self.snapshots = snapshots if isinstance(snapshots, list) else [snapshots]
        self.sent_files = None
        self.sent_caption = None
        self.sent_thumbnail = None
        self.sent_video_info = None
        self.notifications = []
        self.downloaded_message_ids = []

    async def scan_messages(self, min_id):
        for snapshot in self.snapshots:
            if snapshot.message_id > min_id:
                yield snapshot

    async def download_video(self, message_id, destination):
        self.downloaded_message_ids.append(message_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
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
        self.sent_files = [path.name for path in files]
        self.sent_caption = caption_html
        self.sent_thumbnail = thumbnail.name
        self.sent_video_info = video_info
        return DeliveryReceipt((101, 102, 103, 104), 999)

    async def find_matching_album(self, started_at, caption_plain):
        return None

    async def notify(self, text):
        self.notifications.append(text)


class FakeMedia:
    def __init__(self, config):
        self.config = config

    def check_disk(self, source_size):
        return None

    async def probe(self, path):
        return VideoInfo(path, 180, 1920, 1080, has_audio=False)

    def validate_source(self, info):
        return None

    async def transcode(self, source, destination, info):
        destination.write_bytes(b"video")
        return VideoInfo(destination, 180, 1280, 720, has_audio=False)

    async def screenshots(self, video, duration, directory):
        frames = []
        for index in range(1, 4):
            frame = directory / f"frame_{index}.jpg"
            frame.write_bytes(b"image")
            frames.append(frame)
        return frames

    async def thumbnail(self, video, duration, destination):
        assert video.name == "video.mp4"
        assert duration == 180
        destination.write_bytes(b"thumbnail")
        return destination


@pytest.mark.asyncio
async def test_run_once_publishes_one_four_item_album_and_cleans_workdir(app_config):
    config = app_config(daily_success_count=1)
    snapshot = MessageSnapshot(
        message_id=1,
        grouped_id=123,
        caption="#必留 #一 #二 #三 #四 #五\n简介：测试简介",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    database = StateDatabase(config.database_path)
    telegram = FakeTelegram(snapshot)
    service = AutomationService(config, database, telegram, FakeMedia(config))

    summary = await service.run_once()

    assert summary.published == 1
    assert telegram.sent_files == ["video.mp4", "frame_1.jpg", "frame_2.jpg", "frame_3.jpg"]
    assert telegram.sent_caption.startswith("#必留")
    assert telegram.sent_thumbnail == "video_thumb.jpg"
    assert telegram.sent_video_info.display_width == 1280
    assert telegram.sent_video_info.display_height == 720
    assert list(config.work_dir.iterdir()) == []
    assert database.published_count(str(config.source_channel), summary.run_date) == 1
    database.close()


@pytest.mark.asyncio
async def test_empty_caption_is_rejected_before_download_and_replaced(app_config):
    config = app_config(daily_success_count=1)
    published_at = datetime.now(UTC) - timedelta(hours=1)
    empty = MessageSnapshot(
        message_id=1,
        grouped_id=111,
        caption="演员：没有标签也没有简介",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=published_at,
    )
    replacement = MessageSnapshot(
        message_id=2,
        grouped_id=222,
        caption="标签：#有效标签\n简介：替补简介",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=published_at,
    )
    database = StateDatabase(config.database_path)
    database.save_messages(str(config.source_channel), [empty, replacement], 2)
    database.refresh_groups(str(config.source_channel))
    database.begin_attempt(str(config.source_channel), empty.grouped_id, "2026-08-09")
    telegram = FakeTelegram([empty, replacement])
    service = AutomationService(config, database, telegram, FakeMedia(config))

    summary = await service.run_once()

    assert summary.published == 1
    assert summary.rejected == 1
    assert telegram.downloaded_message_ids == [replacement.message_id]
    assert database.counts(str(config.source_channel))["rejected"] == 1
    database.close()


@pytest.mark.asyncio
async def test_dry_run_filters_empty_caption_and_returns_replacement(app_config):
    config = app_config(daily_success_count=1)
    published_at = datetime.now(UTC) - timedelta(hours=1)
    snapshots = [
        MessageSnapshot(
            message_id=1,
            grouped_id=111,
            caption="其他：空文案",
            is_video=True,
            is_photo=False,
            width=1920,
            height=1080,
            duration=180,
            file_size=100,
            published_at=published_at,
        ),
        MessageSnapshot(
            message_id=2,
            grouped_id=222,
            caption="标签：#有效\n简介：内容",
            is_video=True,
            is_photo=False,
            width=1920,
            height=1080,
            duration=180,
            file_size=100,
            published_at=published_at,
        ),
    ]
    database = StateDatabase(config.database_path)
    service = AutomationService(config, database, FakeTelegram(snapshots), FakeMedia(config))

    previews = await service.dry_run()

    assert previews == [(222, "#有效\n\n内容")]
    assert "rejected" not in database.counts(str(config.source_channel))
    database.close()
