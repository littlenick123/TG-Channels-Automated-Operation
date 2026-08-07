from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from telethon import TelegramClient, errors
from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo

from .config import AppConfig
from .models import DeliveryReceipt, MessageSnapshot

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class TelegramError(RuntimeError):
    """Raised when Telegram access or delivery fails."""


class SourceMediaUnavailable(TelegramError):
    """Raised when a selected source message was removed or changed."""


class TelegramGateway:
    def __init__(self, config: AppConfig, client: TelegramClient | None = None):
        self.config = config
        config.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client or TelegramClient(
            str(config.session_path),
            config.api_id,
            config.api_hash,
            flood_sleep_threshold=0,
        )
        self._source: Any = None
        self._target: Any = None

    async def login(self) -> None:
        await self.client.start(phone=self.config.phone)
        me = await self.client.get_me()
        session_filename = getattr(self.client.session, "filename", None)
        if os.name != "nt" and session_filename:
            session_file = Path(session_filename)
            if session_file.exists():
                session_file.chmod(0o600)
        LOGGER.info("Telegram 登录成功：user_id=%s", me.id)

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise TelegramError("Telethon 会话尚未登录，请先运行 login")

    async def disconnect(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()

    async def _source_entity(self) -> Any:
        if self._source is None:
            self._source = await self._resolve_entity(self.config.source_channel)
        return self._source

    async def _target_entity(self) -> Any:
        if self._target is None:
            self._target = await self._resolve_entity(self.config.target_channel)
        return self._target

    async def _resolve_entity(self, identifier: str | int) -> Any:
        try:
            return await self.client.get_entity(identifier)
        except ValueError:
            # A fresh Telethon session may not yet know the access hash for a
            # private channel referenced only by its numeric ID. Loading dialogs
            # populates the session entity cache before the second lookup.
            await self.client.get_dialogs(limit=None)
            return await self.client.get_entity(identifier)

    @staticmethod
    def _video_metadata(message: Any) -> tuple[bool, int | None, int | None, float | None]:
        document = getattr(message, "video", None)
        if document is None:
            return False, None, None, None
        if any(
            isinstance(attribute, DocumentAttributeAnimated) for attribute in document.attributes
        ):
            return False, None, None, None
        attribute = next(
            (
                attribute
                for attribute in document.attributes
                if isinstance(attribute, DocumentAttributeVideo)
            ),
            None,
        )
        if attribute is None:
            return False, None, None, None
        return True, int(attribute.w), int(attribute.h), float(attribute.duration)

    async def scan_messages(self, min_id: int) -> AsyncIterator[MessageSnapshot]:
        source = await self._source_entity()
        async for message in self.client.iter_messages(
            source,
            min_id=min_id,
            reverse=True,
            wait_time=1,
        ):
            is_video, width, height, duration = self._video_metadata(message)
            document = getattr(message, "document", None)
            yield MessageSnapshot(
                message_id=int(message.id),
                grouped_id=int(message.grouped_id) if message.grouped_id is not None else None,
                caption=str(message.raw_text or ""),
                is_video=is_video,
                is_photo=getattr(message, "photo", None) is not None,
                width=width,
                height=height,
                duration=duration,
                file_size=int(document.size) if is_video and document is not None else None,
                published_at=message.date,
            )

    async def _retry(self, operation: Callable[[], Awaitable[T]], description: str) -> T:
        delays = (0, *self.config.retry_delays_seconds)
        last_error: Exception | None = None
        for index, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await operation()
            except errors.FloodWaitError as exc:
                last_error = exc
                LOGGER.warning("%s 触发 FloodWait，等待 %s 秒", description, exc.seconds)
                await asyncio.sleep(max(1, exc.seconds))
                try:
                    return await operation()
                except errors.FloodWaitError as repeated:
                    last_error = repeated
            except (TimeoutError, errors.ServerError, errors.RpcCallFailError, OSError) as exc:
                last_error = exc
                LOGGER.warning("%s 第 %s 次尝试失败：%s", description, index + 1, exc)
        raise TelegramError(f"{description} 重试耗尽：{last_error}") from last_error

    async def download_video(self, message_id: int, destination: Path) -> Path:
        source = await self._source_entity()

        async def operation() -> Path:
            message = await self.client.get_messages(source, ids=message_id)
            if message is None or not self._video_metadata(message)[0]:
                raise SourceMediaUnavailable(f"源消息 {message_id} 不再包含有效视频")
            result = await self.client.download_media(message, file=str(destination))
            if not result:
                raise TelegramError(f"下载源消息 {message_id} 未返回文件路径")
            return Path(result)

        return await self._retry(operation, f"下载消息 {message_id}")

    async def send_album(
        self,
        files: list[Path],
        caption_html: str,
        caption_plain: str,
        upload_started_at: str,
    ) -> DeliveryReceipt:
        if len(files) != 4:
            raise TelegramError("目标媒体组必须恰好包含 1 个视频和 3 张图片")
        target = await self._target_entity()

        async def operation() -> Any:
            return await self.client.send_file(
                target,
                [str(path) for path in files],
                caption=[caption_html, "", "", ""],
                parse_mode="html",
                supports_streaming=True,
            )

        result: Any = None
        last_error: Exception | None = None
        delays = (0, *self.config.retry_delays_seconds)
        for index, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await operation()
                break
            except errors.FloodWaitError as exc:
                last_error = exc
                LOGGER.warning("发送目标媒体组触发 FloodWait，等待 %s 秒", exc.seconds)
                await asyncio.sleep(max(1, exc.seconds))
            except (TimeoutError, errors.ServerError, errors.RpcCallFailError, OSError) as exc:
                last_error = exc
                LOGGER.warning("发送目标媒体组第 %s 次尝试结果不确定：%s", index + 1, exc)
                receipt = await self.find_matching_album(upload_started_at, caption_plain)
                if receipt is not None:
                    return receipt
        if result is None:
            receipt = await self.find_matching_album(upload_started_at, caption_plain)
            if receipt is not None:
                return receipt
            raise TelegramError(f"发送目标媒体组重试耗尽：{last_error}") from last_error
        messages = list(result) if isinstance(result, (list, tuple)) else [result]
        if len(messages) != 4:
            raise TelegramError(f"Telegram 返回了 {len(messages)} 个媒体项，预期为 4")
        grouped_ids = {
            int(message.grouped_id) for message in messages if message.grouped_id is not None
        }
        if len(grouped_ids) != 1:
            raise TelegramError("Telegram 返回的四个媒体项没有共享同一个 grouped_id")
        return DeliveryReceipt(
            message_ids=tuple(int(message.id) for message in messages),
            grouped_id=grouped_ids.pop(),
        )

    async def find_matching_album(
        self, started_at: str, caption_plain: str
    ) -> DeliveryReceipt | None:
        target = await self._target_entity()
        since = datetime.fromisoformat(started_at)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        albums: dict[int, list[Any]] = defaultdict(list)
        async for message in self.client.iter_messages(target, limit=100):
            if message.date < since:
                break
            if message.grouped_id is not None:
                albums[int(message.grouped_id)].append(message)
        matches: list[DeliveryReceipt] = []
        for grouped_id, messages in albums.items():
            ordered = sorted(messages, key=lambda item: item.id)
            videos = sum(self._video_metadata(message)[0] for message in ordered)
            photos = sum(getattr(message, "photo", None) is not None for message in ordered)
            captions = [str(message.raw_text or "") for message in ordered if message.raw_text]
            if (
                len(ordered) == 4
                and videos == 1
                and photos == 3
                and self._video_metadata(ordered[0])[0]
                and captions == ([caption_plain] if caption_plain else [])
            ):
                matches.append(
                    DeliveryReceipt(tuple(int(message.id) for message in ordered), grouped_id)
                )
        return matches[0] if len(matches) == 1 else None

    async def notify(self, text: str) -> None:
        if not self.config.notify_saved_messages:
            return

        async def operation() -> Any:
            return await self.client.send_message("me", text)

        await self._retry(operation, "发送运行摘要")

    async def doctor(self) -> dict[str, str]:
        source = await self._source_entity()
        target = await self._target_entity()
        permissions = await self.client.get_permissions(target, "me")
        can_post = bool(
            getattr(permissions, "is_creator", False)
            or getattr(permissions, "is_admin", False)
            or getattr(permissions, "post_messages", False)
        )
        if not can_post:
            raise TelegramError("当前用户没有目标频道发帖权限")
        me = await self.client.get_me()
        return {
            "account": str(me.id),
            "source": str(getattr(source, "title", self.config.source_channel)),
            "target": str(getattr(target, "title", self.config.target_channel)),
            "post_permission": "ok",
        }
