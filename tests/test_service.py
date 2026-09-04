from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.database import StateDatabase
from channel_operator.models import DeliveryReceipt, MessageSnapshot, VideoInfo
from channel_operator.service import AutomationService
from channel_operator.telegram import (
    ChannelGroupUnavailable,
    DownloadTooSlowError,
    TelegramError,
)


class FakeTelegram:
    def __init__(self, snapshots: MessageSnapshot | list[MessageSnapshot]):
        self.snapshots = snapshots if isinstance(snapshots, list) else [snapshots]
        self.sent_files = None
        self.sent_caption = None
        self.sent_thumbnail = None
        self.sent_video_info = None
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



class FakeReporter:
    def __init__(self):
        self.messages = []

    async def send(self, text, *, strict=False):
        self.messages.append(text)
        return True


class UnavailableDownloadTelegram(FakeTelegram):
    async def download_video(self, message_id, destination):
        raise ChannelGroupUnavailable("源频道已被封禁")


class ExhaustedUploadTelegram(FakeTelegram):
    def __init__(self, snapshots):
        super().__init__(snapshots)
        self.upload_files_existed = False

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
        self.upload_files_existed = all(path.exists() for path in [*files, thumbnail])
        raise TelegramError("发送目标媒体组重试耗尽")


class SlowFirstDownloadTelegram(FakeTelegram):
    def __init__(self, snapshots, slow_message_id):
        super().__init__(snapshots)
        self.slow_message_id = slow_message_id

    async def download_video(self, message_id, destination):
        self.downloaded_message_ids.append(message_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if message_id == self.slow_message_id:
            destination.with_name("source_video.mp4.part").write_bytes(b"partial")
            raise DownloadTooSlowError(message_id, 123.4, 800, 60)
        destination.write_bytes(b"source")
        return destination


class FakeMedia:
    def __init__(self, config):
        self.config = config
        self.watermark_texts = []

    def check_disk(self, source_size):
        return None

    async def probe(self, path):
        return VideoInfo(path, 180, 1920, 1080, has_audio=False)

    def validate_source(self, info):
        return None

    async def cut_first_third(self, source, destination, info):
        destination.write_bytes(b"clipped")
        return VideoInfo(destination, 60, 1920, 1080, has_audio=False)

    async def transcode(self, source, destination, info, *, watermark_text=""):
        assert source.name == "source_first_third.mkv"
        self.watermark_texts.append(watermark_text)
        destination.write_bytes(b"video")
        return VideoInfo(destination, 60, 1280, 720, has_audio=False)

    async def screenshots(self, video, duration, directory):
        frames = []
        for index in range(1, 4):
            frame = directory / f"frame_{index}.jpg"
            frame.write_bytes(b"image")
            frames.append(frame)
        return frames

    async def thumbnail(self, video, duration, destination):
        assert video.name == "video.mp4"
        assert duration == 60
        destination.write_bytes(b"thumbnail")
        return destination


@pytest.mark.asyncio
async def test_run_once_publishes_one_four_item_album_and_cleans_workdir(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
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
    database = StateDatabase(group.database_path)
    telegram = FakeTelegram(snapshot)
    service = AutomationService(
        config, group, database, telegram, FakeMedia(config), FakeReporter()
    )

    summary = await service.run_once()

    assert summary.published == 1
    assert telegram.sent_files == ["video.mp4", "frame_1.jpg", "frame_2.jpg", "frame_3.jpg"]
    assert telegram.sent_caption.startswith("<b>#必留")
    assert telegram.sent_thumbnail == "video_thumb.jpg"
    assert telegram.sent_video_info.display_width == 1280
    assert telegram.sent_video_info.display_height == 720
    assert list(config.work_dir.iterdir()) == []
    assert database.published_count(str(group.source_channel), summary.run_date) == 1
    database.close()


@pytest.mark.asyncio
async def test_empty_caption_is_rejected_before_download_and_replaced(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    published_at = datetime.now(UTC) - timedelta(hours=1)
    empty = MessageSnapshot(
        message_id=1,
        grouped_id=111,
        caption="标签：@没有有效标签",
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
    database = StateDatabase(group.database_path)
    database.save_messages(str(group.source_channel), [empty, replacement], 2)
    database.refresh_groups(str(group.source_channel))
    database.begin_attempt(str(group.source_channel), empty.grouped_id, "2026-08-09")
    telegram = FakeTelegram([empty, replacement])
    service = AutomationService(
        config, group, database, telegram, FakeMedia(config), FakeReporter()
    )

    summary = await service.run_once()

    assert summary.published == 1
    assert summary.rejected == 1
    assert telegram.downloaded_message_ids == [replacement.message_id]
    assert database.counts(str(group.source_channel))["rejected"] == 1
    database.close()


@pytest.mark.asyncio
async def test_dry_run_filters_empty_caption_and_returns_replacement(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    published_at = datetime.now(UTC) - timedelta(hours=1)
    snapshots = [
        MessageSnapshot(
            message_id=1,
            grouped_id=111,
            caption="标签：@没有有效标签",
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
    database = StateDatabase(group.database_path)
    service = AutomationService(
        config,
        group,
        database,
        FakeTelegram(snapshots),
        FakeMedia(config),
        FakeReporter(),
    )

    previews = await service.dry_run()

    assert previews == [(222, "#有效\n内容")]
    assert "rejected" not in database.counts(str(group.source_channel))
    database.close()


@pytest.mark.parametrize(
    ("global_footer", "group_footer", "expected_caption"),
    [
        ("全局默认内容", None, "#有效\n全局默认内容"),
        ("全局默认内容", "频道专属内容", "#有效\n频道专属内容"),
        ("全局默认内容", "", "#有效"),
    ],
)
@pytest.mark.asyncio
async def test_channel_group_intro_footer_overrides_or_inherits_global_value(
    app_config,
    global_footer,
    group_footer,
    expected_caption,
):
    config = app_config(
        daily_success_count=1,
        intro_footer=global_footer,
        group_intro_footer=group_footer,
    )
    group = config.channel_groups[0]
    snapshot = MessageSnapshot(
        message_id=1,
        grouped_id=123,
        caption="标签：#有效",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    database = StateDatabase(group.database_path)
    service = AutomationService(
        config,
        group,
        database,
        FakeTelegram(snapshot),
        FakeMedia(config),
        FakeReporter(),
    )

    previews = await service.dry_run()

    assert previews == [(123, expected_caption)]
    database.close()


@pytest.mark.parametrize(
    ("global_watermark", "group_watermark", "expected_watermark"),
    [
        ("全局水印", None, "全局水印"),
        ("全局水印", "频道水印", "频道水印"),
        ("全局水印", "", ""),
    ],
)
@pytest.mark.asyncio
async def test_channel_group_watermark_overrides_or_inherits_global_value(
    app_config,
    global_watermark,
    group_watermark,
    expected_watermark,
):
    config = app_config(
        daily_success_count=1,
        watermark_text=global_watermark,
        group_watermark_text=group_watermark,
    )
    group = config.channel_groups[0]
    snapshot = MessageSnapshot(
        message_id=1,
        grouped_id=123,
        caption="标签：#有效",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    database = StateDatabase(group.database_path)
    media = FakeMedia(config)
    service = AutomationService(
        config,
        group,
        database,
        FakeTelegram(snapshot),
        media,
        FakeReporter(),
    )

    summary = await service.run_once()

    assert summary.published == 1
    assert media.watermark_texts == [expected_watermark]
    database.close()


@pytest.mark.asyncio
async def test_channel_failure_aborts_group_and_keeps_media_retryable(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    source = MessageSnapshot(
        message_id=1,
        grouped_id=333,
        caption="标签：#有效\n简介：内容",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    database = StateDatabase(group.database_path)
    service = AutomationService(
        config,
        group,
        database,
        UnavailableDownloadTelegram(source),
        FakeMedia(config),
        FakeReporter(),
    )

    with pytest.raises(ChannelGroupUnavailable):
        await service.run_once()

    assert database.counts(str(group.source_channel))["retryable"] == 1
    database.close()


@pytest.mark.asyncio
async def test_exhausted_upload_cleans_media_then_keeps_group_retryable(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    source = MessageSnapshot(
        message_id=1,
        grouped_id=444,
        caption="标签：#有效\n简介：内容",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    database = StateDatabase(group.database_path)
    telegram = ExhaustedUploadTelegram(source)
    service = AutomationService(
        config, group, database, telegram, FakeMedia(config), FakeReporter()
    )

    summary = await service.run_once()

    assert summary.retryable_failures == 1
    assert telegram.upload_files_existed is True
    assert list(config.work_dir.iterdir()) == []
    assert database.counts(str(group.source_channel))["retryable"] == 1
    database.close()


@pytest.mark.asyncio
async def test_low_speed_group_is_cleaned_marked_retryable_and_replaced(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    published_at = datetime.now(UTC) - timedelta(hours=1)
    slow = MessageSnapshot(
        message_id=10,
        grouped_id=555,
        caption="标签：#低速\n简介：内容",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=published_at,
    )
    replacement = MessageSnapshot(
        message_id=11,
        grouped_id=556,
        caption="标签：#替补\n简介：内容",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=published_at,
    )
    database = StateDatabase(group.database_path)
    database.save_messages(str(group.source_channel), [slow, replacement], 11)
    database.refresh_groups(str(group.source_channel))
    database.begin_attempt(str(group.source_channel), slow.grouped_id, "2026-08-11")
    telegram = SlowFirstDownloadTelegram([slow, replacement], slow.message_id)
    service = AutomationService(
        config, group, database, telegram, FakeMedia(config), FakeReporter()
    )

    summary = await service.run_once()

    assert summary.published == 1
    assert summary.retryable_failures == 1
    assert telegram.downloaded_message_ids == [slow.message_id, replacement.message_id]
    assert list(config.work_dir.iterdir()) == []
    assert database.counts(str(group.source_channel))["retryable"] == 1
    database.close()


@pytest.mark.asyncio
async def test_continuous_mode_uses_per_cycle_target_not_daily_total(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    published_at = datetime.now(UTC) - timedelta(hours=1)
    snapshots = [
        MessageSnapshot(
            message_id=index,
            grouped_id=600 + index,
            caption=f"标签：#循环{index}\n简介：内容{index}",
            is_video=True,
            is_photo=False,
            width=1920,
            height=1080,
            duration=180,
            file_size=100,
            published_at=published_at,
        )
        for index in (1, 2)
    ]
    database = StateDatabase(group.database_path)
    telegram = FakeTelegram(snapshots)
    service = AutomationService(
        config, group, database, telegram, FakeMedia(config), FakeReporter()
    )

    first = await service.run_once(continuous=True)
    second = await service.run_once(continuous=True)

    assert first.published == 1
    assert second.published == 1
    assert len(telegram.downloaded_message_ids) == 2
    assert database.published_count(str(group.source_channel), first.run_date) == 2
    stats = database.daily_stats(first.run_date)
    assert stats.published == 2
    assert stats.attempted == 2
    database.close()


@pytest.mark.asyncio
async def test_continuous_mode_does_not_retry_failed_media_on_same_date(app_config):
    config = app_config(daily_success_count=1)
    group = config.channel_groups[0]
    source = MessageSnapshot(
        message_id=20,
        grouped_id=620,
        caption="标签：#低速\n简介：内容",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=100,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    database = StateDatabase(group.database_path)
    telegram = SlowFirstDownloadTelegram(source, source.message_id)
    service = AutomationService(
        config, group, database, telegram, FakeMedia(config), FakeReporter()
    )

    first = await service.run_once(continuous=True)
    second = await service.run_once(continuous=True)

    assert first.retryable_failures == 1
    assert second.attempted == 0
    assert telegram.downloaded_message_ids == [source.message_id]
    database.close()
