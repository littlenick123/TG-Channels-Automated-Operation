from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors, utils
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputFile,
    InputMediaUploadedDocument,
)

from channel_operator.models import DeliveryReceipt, VideoInfo
from channel_operator.telegram import (
    ChannelGroupUnavailable,
    TelegramError,
    TelegramGateway,
)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.uploads = []

    async def get_entity(self, entity):
        return entity

    async def send_file(self, entity, files, **kwargs):
        self.calls.append((entity, files, kwargs))
        return [SimpleNamespace(id=index, grouped_id=777) for index in range(10, 14)]

    async def upload_file(self, file):
        uploaded = InputFile(
            id=len(self.uploads) + 1,
            parts=1,
            name=Path(file).name,
            md5_checksum="test",
        )
        self.uploads.append((file, uploaded))
        return uploaded


class FlakyClient(FakeClient):
    async def send_file(self, entity, files, **kwargs):
        self.calls.append((entity, files, kwargs))
        raise OSError("response was lost")


class UploadRuntimeErrorClient(FakeClient):
    def __init__(self, message: str, failures: int):
        super().__init__()
        self.message = message
        self.failures = failures
        self.upload_attempts = []

    async def upload_file(self, file):
        path = Path(file)
        self.upload_attempts.append((path, path.exists()))
        if self.failures:
            self.failures -= 1
            raise RuntimeError(self.message)
        return await super().upload_file(file)


class PrivateChannelClient(FakeClient):
    async def get_entity(self, entity):
        raise errors.ChannelPrivateError(request=None)


class ResumableDownloadClient(FakeClient):
    def __init__(self, data: bytes):
        super().__init__()
        self.data = data
        self.message = SimpleNamespace(document=SimpleNamespace(size=len(data)))
        self.download_calls = []

    async def get_messages(self, entity, ids):
        return self.message

    def iter_download(
        self,
        message,
        *,
        offset,
        stride,
        limit,
        request_size,
        chunk_size,
        file_size,
    ):
        call_number = len(self.download_calls) + 1
        self.download_calls.append(
            {
                "offset": offset,
                "stride": stride,
                "limit": limit,
                "request_size": request_size,
                "chunk_size": chunk_size,
                "file_size": file_size,
            }
        )

        async def stream():
            position = offset
            for index in range(limit):
                if call_number == 1 and index >= 1:
                    raise OSError("simulated interrupted download")
                if position >= len(self.data):
                    return
                end = min(position + chunk_size, len(self.data))
                yield self.data[position:end]
                position += stride

        return stream()


class StallingDownloadClient(ResumableDownloadClient):
    def iter_download(
        self,
        message,
        *,
        offset,
        stride,
        limit,
        request_size,
        chunk_size,
        file_size,
    ):
        call_number = len(self.download_calls) + 1
        self.download_calls.append(
            {
                "offset": offset,
                "stride": stride,
                "limit": limit,
                "request_size": request_size,
                "chunk_size": chunk_size,
                "file_size": file_size,
            }
        )

        async def stream():
            position = offset
            for index in range(limit):
                if call_number == 2 and index >= 1:
                    await asyncio.Future()
                if position >= len(self.data):
                    return
                end = min(position + chunk_size, len(self.data))
                yield self.data[position:end]
                position += stride

        return stream()


class ReconcilingGateway(TelegramGateway):
    async def find_matching_album(self, started_at, caption_plain):
        assert caption_plain == "简介"
        return DeliveryReceipt((20, 21, 22, 23), 888)


class NoReceiptGateway(TelegramGateway):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reconciliation_checks = 0

    async def find_matching_album(self, started_at, caption_plain):
        self.reconciliation_checks += 1
        return None


class ResumableGateway(TelegramGateway):
    @staticmethod
    def _video_metadata(message):
        return True, 1920, 1080, 180.0


@pytest.mark.asyncio
async def test_send_album_is_one_call_in_video_then_image_order(app_config, tmp_path: Path):
    client = FakeClient()
    config = app_config()
    gateway = TelegramGateway(config, config.channel_groups[0], client=client)
    files = [
        tmp_path / "video.mp4",
        tmp_path / "frame_1.jpg",
        tmp_path / "frame_2.jpg",
        tmp_path / "frame_3.jpg",
    ]
    thumbnail = tmp_path / "video_thumb.jpg"
    video_info = VideoInfo(files[0], 180.5, 1280, 720, has_audio=True)

    receipt = await gateway.send_album(
        files,
        "<blockquote>简介</blockquote>",
        "简介",
        "2026-08-02T00:00:00+00:00",
        video_info=video_info,
        thumbnail=thumbnail,
    )

    assert len(client.calls) == 1
    _, sent_files, options = client.calls[0]
    assert isinstance(sent_files[0], InputMediaUploadedDocument)
    assert sent_files[1:] == [str(path) for path in files[1:]]
    assert sent_files[0].file is client.uploads[0][1]
    assert sent_files[0].thumb is client.uploads[1][1]
    assert bytes(sent_files[0])
    assert utils.get_input_media(sent_files[0]) is sent_files[0]
    video_attribute = next(
        attribute
        for attribute in sent_files[0].attributes
        if isinstance(attribute, DocumentAttributeVideo)
    )
    filename_attribute = next(
        attribute
        for attribute in sent_files[0].attributes
        if isinstance(attribute, DocumentAttributeFilename)
    )
    assert (video_attribute.w, video_attribute.h) == (1280, 720)
    assert video_attribute.duration == 180.5
    assert video_attribute.supports_streaming is True
    assert filename_attribute.file_name == "video.mp4"
    assert options["caption"] == ["<blockquote>简介</blockquote>", "", "", ""]
    assert options["supports_streaming"] is True
    assert receipt.message_ids == (10, 11, 12, 13)
    assert receipt.grouped_id == 777


@pytest.mark.asyncio
async def test_ambiguous_send_checks_target_before_retrying(app_config, tmp_path: Path):
    client = FlakyClient()
    config = app_config()
    gateway = ReconcilingGateway(config, config.channel_groups[0], client=client)
    files = [
        tmp_path / "video.mp4",
        tmp_path / "frame_1.jpg",
        tmp_path / "frame_2.jpg",
        tmp_path / "frame_3.jpg",
    ]
    video_info = VideoInfo(files[0], 180, 1280, 720, has_audio=True)

    receipt = await gateway.send_album(
        files,
        "<blockquote>简介</blockquote>",
        "简介",
        "2026-08-02T00:00:00+00:00",
        video_info=video_info,
        thumbnail=tmp_path / "video_thumb.jpg",
    )

    assert len(client.calls) == 1
    assert receipt == DeliveryReceipt((20, 21, 22, 23), 888)


def _album_files(tmp_path: Path) -> tuple[list[Path], Path, VideoInfo]:
    files = [
        tmp_path / "video.mp4",
        tmp_path / "frame_1.jpg",
        tmp_path / "frame_2.jpg",
        tmp_path / "frame_3.jpg",
    ]
    thumbnail = tmp_path / "video_thumb.jpg"
    for path in [*files, thumbnail]:
        path.write_bytes(b"test")
    return files, thumbnail, VideoInfo(files[0], 180, 1280, 720, has_audio=True)


@pytest.mark.asyncio
async def test_failed_upload_part_retries_the_existing_transcoded_file(
    app_config, tmp_path: Path
):
    client = UploadRuntimeErrorClient("Failed to upload file part 235.", failures=3)
    config = app_config(retry_delays_seconds=(0, 0, 0))
    gateway = NoReceiptGateway(config, config.channel_groups[0], client=client)
    files, thumbnail, video_info = _album_files(tmp_path)

    receipt = await gateway.send_album(
        files,
        "<blockquote>简介</blockquote>",
        "简介",
        "2026-08-10T00:00:00+00:00",
        video_info=video_info,
        thumbnail=thumbnail,
    )

    assert receipt == DeliveryReceipt((10, 11, 12, 13), 777)
    assert [path for path, _ in client.upload_attempts[:4]] == [files[0]] * 4
    assert all(existed for _, existed in client.upload_attempts)
    assert gateway.reconciliation_checks == 3
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_failed_upload_part_is_retryable_only_until_attempts_are_exhausted(
    app_config, tmp_path: Path
):
    client = UploadRuntimeErrorClient("Failed to upload file part 7.", failures=4)
    config = app_config(retry_delays_seconds=(0, 0, 0))
    gateway = NoReceiptGateway(config, config.channel_groups[0], client=client)
    files, thumbnail, video_info = _album_files(tmp_path)

    with pytest.raises(TelegramError, match="发送目标媒体组重试耗尽"):
        await gateway.send_album(
            files,
            "<blockquote>简介</blockquote>",
            "简介",
            "2026-08-10T00:00:00+00:00",
            video_info=video_info,
            thumbnail=thumbnail,
        )

    assert len(client.upload_attempts) == 4
    assert gateway.reconciliation_checks == 5
    assert all(path.exists() for path in [*files, thumbnail])


@pytest.mark.asyncio
async def test_unrelated_runtime_error_is_not_treated_as_upload_failure(
    app_config, tmp_path: Path
):
    client = UploadRuntimeErrorClient("unexpected local bug", failures=1)
    config = app_config(retry_delays_seconds=(0, 0, 0))
    gateway = NoReceiptGateway(config, config.channel_groups[0], client=client)
    files, thumbnail, video_info = _album_files(tmp_path)

    with pytest.raises(RuntimeError, match="unexpected local bug"):
        await gateway.send_album(
            files,
            "<blockquote>简介</blockquote>",
            "简介",
            "2026-08-10T00:00:00+00:00",
            video_info=video_info,
            thumbnail=thumbnail,
        )

    assert len(client.upload_attempts) == 1
    assert gateway.reconciliation_checks == 0


@pytest.mark.asyncio
async def test_download_resumes_partial_file_after_network_error(app_config, tmp_path: Path):
    request_size = 512 * 1024
    data = (
        b"a" * request_size
        + b"b" * request_size
        + b"c" * request_size
        + b"d" * request_size
        + b"e" * request_size
        + b"tail"
    )
    client = ResumableDownloadClient(data)
    config = app_config(
        retry_delays_seconds=(0,),
        download_concurrency=2,
        flood_sleep_threshold_seconds=60,
    )
    gateway = ResumableGateway(
        config,
        config.channel_groups[0],
        client=client,
    )
    destination = tmp_path / "source_video.mp4"

    result = await gateway.download_video(6256, destination)

    assert result == destination
    assert destination.read_bytes() == data
    assert not destination.with_name("source_video.mp4.part").exists()
    assert [call["offset"] for call in client.download_calls] == [
        0,
        request_size,
        request_size * 2,
        request_size * 3,
    ]
    assert all(call["request_size"] == request_size for call in client.download_calls)
    assert all(call["stride"] == request_size * 2 for call in client.download_calls)
    assert all(call["file_size"] == len(data) for call in client.download_calls)
    assert client.flood_sleep_threshold == 60


@pytest.mark.asyncio
async def test_stalled_download_lane_is_cancelled_and_resumes_from_partial_file(
    app_config, tmp_path: Path, caplog
):
    request_size = 512 * 1024
    data = (
        b"a" * request_size
        + b"b" * request_size
        + b"c" * request_size
        + b"d" * request_size
        + b"tail"
    )
    client = StallingDownloadClient(data)
    config = app_config(
        retry_delays_seconds=(0,),
        download_concurrency=2,
        download_stall_timeout_seconds=0.02,
    )
    gateway = ResumableGateway(config, config.channel_groups[0], client=client)
    destination = tmp_path / "source_video.mp4"
    caplog.set_level("INFO")

    result = await gateway.download_video(3116, destination)

    assert result == destination
    assert destination.read_bytes() == data
    assert not destination.with_name("source_video.mp4.part").exists()
    assert [call["offset"] for call in client.download_calls] == [
        0,
        request_size,
        request_size * 2,
        request_size * 3,
    ]
    assert "连续 0.02 秒无进展" in caplog.text
    assert f"从 {request_size * 2}/{len(data)} 字节继续" in caplog.text
    assert "进度 100.0%" in caplog.text


@pytest.mark.asyncio
async def test_private_channel_error_is_promoted_to_group_failure(app_config):
    config = app_config()
    gateway = TelegramGateway(
        config,
        config.channel_groups[0],
        client=PrivateChannelClient(),
    )

    with pytest.raises(ChannelGroupUnavailable, match="无法访问"):
        await gateway._source_entity()
