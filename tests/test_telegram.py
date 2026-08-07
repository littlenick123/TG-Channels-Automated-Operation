from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from channel_operator.models import DeliveryReceipt
from channel_operator.telegram import TelegramGateway


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get_entity(self, entity):
        return entity

    async def send_file(self, entity, files, **kwargs):
        self.calls.append((entity, files, kwargs))
        return [SimpleNamespace(id=index, grouped_id=777) for index in range(10, 14)]


class FlakyClient(FakeClient):
    async def send_file(self, entity, files, **kwargs):
        self.calls.append((entity, files, kwargs))
        raise OSError("response was lost")


class ReconcilingGateway(TelegramGateway):
    async def find_matching_album(self, started_at, caption_plain):
        assert caption_plain == "简介"
        return DeliveryReceipt((20, 21, 22, 23), 888)


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

    receipt = await gateway.send_album(
        files,
        "<blockquote>简介</blockquote>",
        "简介",
        "2026-08-02T00:00:00+00:00",
    )

    assert len(client.calls) == 1
    _, sent_files, options = client.calls[0]
    assert sent_files == [str(path) for path in files]
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

    receipt = await gateway.send_album(
        files,
        "<blockquote>简介</blockquote>",
        "简介",
        "2026-08-02T00:00:00+00:00",
    )

    assert len(client.calls) == 1
    assert receipt == DeliveryReceipt((20, 21, 22, 23), 888)
