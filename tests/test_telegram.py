from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import utils
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputFile,
    InputMediaUploadedDocument,
)

from channel_operator.models import DeliveryReceipt, VideoInfo
from channel_operator.telegram import TelegramGateway


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
        request_size,
        chunk_size,
        file_size,
    ):
        call_number = len(self.download_calls) + 1
        self.download_calls.append(
            {
                "offset": offset,
                "request_size": request_size,
                "chunk_size": chunk_size,
                "file_size": file_size,
            }
        )

        async def stream():
            position = offset
            while position < len(self.data):
                if call_number == 1 and position >= request_size:
                    raise OSError("simulated interrupted download")
                end = min(position + chunk_size, len(self.data))
                yield self.data[position:end]
                position = end

        return stream()


class ReconcilingGateway(TelegramGateway):
    async def find_matching_album(self, started_at, caption_plain):
        assert caption_plain == "简介"
        return DeliveryReceipt((20, 21, 22, 23), 888)


class ResumableGateway(TelegramGateway):
    @staticmethod
    def _video_metadata(message):
        return True, 1920, 1080, 180.0


@pytest.mark.asyncio
async def test_send_album_is_one_call_in_video_then_image_order(app_config, tmp_path: Path):
    client = FakeClient()
    gateway = TelegramGateway(app_config(), client=client)
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
    gateway = ReconcilingGateway(app_config(), client=client)
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


@pytest.mark.asyncio
async def test_download_resumes_partial_file_after_network_error(app_config, tmp_path: Path):
    request_size = 512 * 1024
    data = b"a" * request_size + b"b" * request_size + b"tail"
    client = ResumableDownloadClient(data)
    gateway = ResumableGateway(
        app_config(retry_delays_seconds=(0,), flood_sleep_threshold_seconds=60),
        client=client,
    )
    destination = tmp_path / "source_video.mp4"

    result = await gateway.download_video(6256, destination)

    assert result == destination
    assert destination.read_bytes() == data
    assert not destination.with_name("source_video.mp4.part").exists()
    assert [call["offset"] for call in client.download_calls] == [0, request_size]
    assert all(call["request_size"] == request_size for call in client.download_calls)
    assert all(call["file_size"] == len(data) for call in client.download_calls)
    assert client.flood_sleep_threshold == 60
