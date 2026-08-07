from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.database import StateDatabase
from channel_operator.models import DeliveryReceipt, MessageSnapshot, VideoInfo
from channel_operator.service import AutomationService


class FakeTelegram:
    def __init__(self, snapshot: MessageSnapshot):
        self.snapshot = snapshot
        self.sent_files = None
        self.sent_caption = None
        self.notifications = []

    async def scan_messages(self, min_id):
        if self.snapshot.message_id > min_id:
            yield self.snapshot

    async def download_video(self, message_id, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"source")
        return destination

    async def send_album(self, files, caption_html, caption_plain, upload_started_at):
        self.sent_files = [path.name for path in files]
        self.sent_caption = caption_html
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
    assert list(config.work_dir.iterdir()) == []
    assert database.published_count(str(config.source_channel), summary.run_date) == 1
    database.close()
