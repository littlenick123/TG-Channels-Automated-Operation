from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ChannelGroupConfig


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    message_id: int
    grouped_id: int | None
    caption: str
    is_video: bool
    is_photo: bool
    width: int | None
    height: int | None
    duration: float | None
    file_size: int | None
    published_at: datetime


@dataclass(frozen=True, slots=True)
class MediaGroup:
    source_channel: str
    grouped_id: int
    message_ids: tuple[int, ...]
    video_message_id: int
    caption: str
    width: int
    height: int
    duration: float | None
    file_size: int
    status: str
    selected_date: str | None = None
    attempts: int = 0
    upload_started_at: str | None = None
    attempt_caption_plain: str | None = None


@dataclass(frozen=True, slots=True)
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    rotation: int = 0
    has_audio: bool = True

    @property
    def display_width(self) -> int:
        return self.height if abs(self.rotation) % 180 == 90 else self.width

    @property
    def display_height(self) -> int:
        return self.width if abs(self.rotation) % 180 == 90 else self.height


@dataclass(frozen=True, slots=True)
class CaptionResult:
    html: str
    plain: str
    tags: tuple[str, ...]
    intro: str | None


@dataclass(slots=True)
class RunSummary:
    run_date: str
    published: int = 0
    attempted: int = 0
    rejected: int = 0
    retryable_failures: int = 0
    reconciled: int = 0


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    message_ids: tuple[int, ...]
    grouped_id: int


@dataclass(frozen=True, slots=True)
class GroupRunResult:
    group: ChannelGroupConfig
    summary: RunSummary | None = None
    skipped_reason: str | None = None
    published_before_skip: int = 0

    @property
    def published(self) -> int:
        return (
            self.summary.published
            if self.summary is not None
            else self.published_before_skip
        )

    @property
    def succeeded(self) -> bool:
        return (
            self.skipped_reason is None
            and self.summary is not None
            and self.summary.published >= self.group.daily_success_count
        )
