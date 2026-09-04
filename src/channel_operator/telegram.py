from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from telethon import TelegramClient, errors
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputMediaUploadedDocument,
)

from .config import AppConfig, ChannelGroupConfig
from .models import DeliveryReceipt, MessageSnapshot, VideoInfo

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
DOWNLOAD_REQUEST_SIZE = 512 * 1024
DOWNLOAD_PROGRESS_INTERVAL_SECONDS = 30
DOWNLOAD_STREAM_CLOSE_TIMEOUT_SECONDS = 10
UPLOAD_PART_FAILURE_RE = re.compile(r"^Failed to upload file part \d+\.$")
FLOOD_WAIT_ERRORS = (errors.FloodWaitError, errors.FloodPremiumWaitError)
CHANNEL_GROUP_ERRORS = (
    errors.ChannelBannedError,
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChannelPublicGroupNaError,
    errors.ChatForwardsRestrictedError,
    errors.ChatGuestSendForbiddenError,
    errors.ChatSendMediaForbiddenError,
    errors.ChatSendPhotosForbiddenError,
    errors.ChatSendVideosForbiddenError,
    errors.ChatWriteForbiddenError,
    errors.ChatAdminRequiredError,
    errors.MessageAuthorRequiredError,
    errors.UserBannedInChannelError,
    errors.UserNotParticipantError,
    errors.PeerIdInvalidError,
    errors.ChatForbiddenError,
)


class _RollingDownloadSpeed:
    def __init__(self, window_seconds: float, started_at: float, start_offset: int):
        self.window_seconds = window_seconds
        self.samples: deque[tuple[float, int]] = deque([(started_at, start_offset)])

    def sample(self, sampled_at: float, offset: int) -> float | None:
        self.samples.append((sampled_at, offset))
        cutoff = sampled_at - self.window_seconds
        while len(self.samples) >= 2 and self.samples[1][0] <= cutoff:
            self.samples.popleft()
        baseline_at, baseline_offset = self.samples[0]
        elapsed = sampled_at - baseline_at
        if elapsed < self.window_seconds:
            return None
        return max(0, offset - baseline_offset) / elapsed / 1024


class TelegramError(RuntimeError):
    """Raised when Telegram access or delivery fails."""


class DownloadStalledError(OSError):
    """Raised when a Telegram download batch makes no progress in time."""


class DownloadTooSlowError(TelegramError):
    """Raised when actual writes stay below the configured rolling speed."""

    def __init__(
        self,
        message_id: int,
        speed_kib_per_second: float,
        limit_kib_per_second: float,
        window_seconds: float,
    ):
        self.message_id = message_id
        self.speed_kib_per_second = speed_kib_per_second
        self.limit_kib_per_second = limit_kib_per_second
        self.window_seconds = window_seconds
        super().__init__(
            f"下载消息 {message_id} 最近 {window_seconds:g} 秒平均速度 "
            f"{speed_kib_per_second:.1f} KiB/s，低于阈值 "
            f"{limit_kib_per_second:g} KiB/s"
        )


class SourceMediaUnavailable(TelegramError):
    """Raised when a selected source message was removed or changed."""


class ChannelGroupUnavailable(TelegramError):
    """Raised when a configured source or target channel cannot be used."""


class DeliveryUncertainError(ChannelGroupUnavailable):
    """Raised when delivery may have produced more than one target album."""


class StagingMediaUnavailable(TelegramError):
    """Raised when a persisted staging album was deleted or changed."""


class TelegramGateway:
    def __init__(
        self,
        config: AppConfig,
        group: ChannelGroupConfig | None = None,
        client: TelegramClient | None = None,
        entity_cache: dict[str | int, Any] | None = None,
    ):
        self.config = config
        self.group = group
        config.session_path.parent.mkdir(parents=True, exist_ok=True)
        if client is None:
            self.client = TelegramClient(
                str(config.session_path),
                config.api_id,
                config.api_hash,
                flood_sleep_threshold=config.flood_sleep_threshold_seconds,
            )
        else:
            self.client = client
            self.client.flood_sleep_threshold = config.flood_sleep_threshold_seconds
        self._entities = entity_cache if entity_cache is not None else {}

    def for_group(self, group: ChannelGroupConfig) -> TelegramGateway:
        return TelegramGateway(
            self.config,
            group,
            client=self.client,
            entity_cache=self._entities,
        )

    def _require_group(self) -> ChannelGroupConfig:
        if self.group is None:
            raise RuntimeError("当前 TelegramGateway 尚未绑定频道组")
        return self.group

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
        return await self._resolve_entity(self._require_group().source_channel)

    async def _target_entity(self) -> Any:
        return await self._resolve_entity(self._require_group().target_channel)

    async def _staging_entity(self) -> Any:
        return await self._resolve_entity(self.config.delivery.staging_channel)

    async def _resolve_entity(self, identifier: str | int) -> Any:
        if identifier in self._entities:
            return self._entities[identifier]
        try:
            entity = await self.client.get_entity(identifier)
        except CHANNEL_GROUP_ERRORS as exc:
            raise ChannelGroupUnavailable(
                f"频道 {identifier} 无法访问：{type(exc).__name__}: {exc}"
            ) from exc
        except ValueError:
            # A fresh Telethon session may not yet know the access hash for a
            # private channel referenced only by its numeric ID. Loading dialogs
            # populates the session entity cache before the second lookup.
            try:
                await self.client.get_dialogs(limit=None)
                entity = await self.client.get_entity(identifier)
            except CHANNEL_GROUP_ERRORS as exc:
                raise ChannelGroupUnavailable(
                    f"频道 {identifier} 无法访问：{type(exc).__name__}: {exc}"
                ) from exc
            except ValueError as exc:
                raise ChannelGroupUnavailable(f"无法解析频道 {identifier}") from exc
        self._entities[identifier] = entity
        return entity

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
        try:
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
                    grouped_id=(
                        int(message.grouped_id)
                        if message.grouped_id is not None
                        else None
                    ),
                    caption=str(message.raw_text or ""),
                    is_video=is_video,
                    is_photo=getattr(message, "photo", None) is not None,
                    width=width,
                    height=height,
                    duration=duration,
                    file_size=(
                        int(document.size)
                        if is_video and document is not None
                        else None
                    ),
                    published_at=message.date,
                )
        except CHANNEL_GROUP_ERRORS as exc:
            raise ChannelGroupUnavailable(
                f"读取源频道失败：{type(exc).__name__}: {exc}"
            ) from exc

    async def _retry(self, operation: Callable[[], Awaitable[T]], description: str) -> T:
        attempts = len(self.config.retry_delays_seconds) + 1
        last_error: Exception | None = None
        for index in range(attempts):
            try:
                return await operation()
            except FLOOD_WAIT_ERRORS as exc:
                last_error = exc
                LOGGER.warning(
                    "%s 触发 %s，等待 %s 秒后从断点继续",
                    description,
                    type(exc).__name__,
                    exc.seconds,
                )
                if index + 1 < attempts:
                    await asyncio.sleep(max(1, exc.seconds))
            except (
                TimeoutError,
                errors.TimedOutError,
                errors.ServerError,
                errors.RpcCallFailError,
                errors.FileReferenceExpiredError,
                errors.FilerefUpgradeNeededError,
                OSError,
            ) as exc:
                last_error = exc
                LOGGER.warning("%s 第 %s 次尝试失败：%s", description, index + 1, exc)
                if index + 1 < attempts:
                    delay = self.config.retry_delays_seconds[index]
                    if delay:
                        await asyncio.sleep(delay)
        raise TelegramError(f"{description} 重试耗尽：{last_error}") from last_error

    @staticmethod
    async def _next_download_chunk(stream: Any) -> bytes | memoryview:
        try:
            return await stream.__anext__()
        except StopAsyncIteration as exc:
            raise OSError("Telegram 下载流在到达预期文件末尾前结束") from exc

    @staticmethod
    def _consume_future_result(future: asyncio.Future[Any]) -> None:
        with suppress(BaseException):
            future.exception()

    async def _settle_cancelled_tasks(
        self, tasks: list[asyncio.Task[Any]], timeout: float
    ) -> int:
        if not tasks:
            return 0
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in done:
            self._consume_future_result(task)
        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_future_result)
        return len(pending)

    async def _close_download_streams(
        self, streams: list[Any], message_id: int
    ) -> None:
        close_tasks: list[asyncio.Task[Any]] = []
        for stream in streams:
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if close is None:
                continue
            try:
                result = close()
            except Exception as exc:
                LOGGER.debug("关闭下载消息 %s 的 Telegram 下载流失败：%s", message_id, exc)
                continue
            if inspect.isawaitable(result):
                close_tasks.append(asyncio.create_task(result))
        if not close_tasks:
            return

        close_timeout = min(
            DOWNLOAD_STREAM_CLOSE_TIMEOUT_SECONDS,
            self.config.download_stall_timeout_seconds,
        )
        done, pending = await asyncio.wait(close_tasks, timeout=close_timeout)
        for task in done:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                continue
            if error is not None:
                LOGGER.debug(
                    "关闭下载消息 %s 的 Telegram 下载流失败：%s", message_id, error
                )
        if pending:
            for task in pending:
                task.cancel()
                task.add_done_callback(self._consume_future_result)
            LOGGER.warning(
                "关闭下载消息 %s 的 %s 条 Telegram 下载流超过 %.1f 秒，已放弃等待",
                message_id,
                len(pending),
                close_timeout,
            )

    async def _monitor_download_speed(
        self,
        message_id: int,
        current_offset: Callable[[], int],
        expected_size: int,
        started_at: float,
        start_offset: int,
    ) -> None:
        window = self.config.download_low_speed_window_seconds
        limit = self.config.download_low_speed_limit_kib_per_second
        sample_interval = min(5.0, window / 12)
        tracker = _RollingDownloadSpeed(window, started_at, start_offset)
        while True:
            await asyncio.sleep(sample_interval)
            offset = current_offset()
            if offset >= expected_size:
                return
            speed = tracker.sample(time.monotonic(), offset)
            if speed is not None and speed < limit:
                raise DownloadTooSlowError(message_id, speed, limit, window)

    async def _reconnect_after_slow_download(self, message_id: int) -> None:
        LOGGER.warning(
            "下载消息 %s 触发低速保护，正在重连 Telegram 以清除低速媒体连接",
            message_id,
        )
        try:
            await self.client.disconnect()
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise TelegramError("重连后 Telethon 会话未授权")
        except Exception as exc:
            raise TelegramError(
                f"低速下载后重连 Telegram 失败：{type(exc).__name__}: {exc}"
            ) from exc
        LOGGER.info("下载消息 %s 触发低速保护后 Telegram 重连成功", message_id)

    async def _download_message(
        self, message: Any, message_id: int, destination: Path
    ) -> Path:
        document = getattr(message, "document", None)
        expected_size = int(getattr(document, "size", 0) or 0)
        if expected_size <= 0:
            raise SourceMediaUnavailable(f"源消息 {message_id} 缺少有效文件大小")

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        if destination.exists():
            if destination.stat().st_size == expected_size:
                return destination
            destination.unlink()

        partial_size = partial.stat().st_size if partial.exists() else 0
        if partial_size > expected_size:
            LOGGER.warning(
                "下载消息 %s 的临时文件大于源文件，放弃旧断点并重新开始", message_id
            )
            partial.unlink()
            partial_size = 0

        offset = partial_size - (partial_size % DOWNLOAD_REQUEST_SIZE)
        if offset != partial_size:
            LOGGER.info(
                "下载消息 %s 将未对齐断点从 %s 回退到 %s 字节",
                message_id,
                partial_size,
                offset,
            )

        mode = "r+b" if partial.exists() else "w+b"
        with partial.open(mode) as handle:
            handle.truncate(offset)
            handle.seek(offset)
            if offset:
                LOGGER.info(
                    "下载消息 %s 从 %s/%s 字节继续",
                    message_id,
                    offset,
                    expected_size,
                )
            remaining_chunks = (
                expected_size - offset + DOWNLOAD_REQUEST_SIZE - 1
            ) // DOWNLOAD_REQUEST_SIZE
            concurrency = min(self.config.download_concurrency, remaining_chunks)
            stride = concurrency * DOWNLOAD_REQUEST_SIZE
            streams: list[Any] = []
            attempt_started_at = time.monotonic()
            attempt_start_offset = offset
            last_progress_log_at = attempt_started_at
            low_speed_monitor = asyncio.create_task(
                self._monitor_download_speed(
                    message_id,
                    lambda: offset,
                    expected_size,
                    attempt_started_at,
                    attempt_start_offset,
                )
            )
            if concurrency:
                LOGGER.info(
                    "下载消息 %s 使用 %s 路并发，分片大小 %s 字节",
                    message_id,
                    concurrency,
                    DOWNLOAD_REQUEST_SIZE,
                )
                for lane in range(concurrency):
                    stream_offset = offset + lane * DOWNLOAD_REQUEST_SIZE
                    limit = (expected_size - stream_offset + stride - 1) // stride
                    streams.append(
                        self.client.iter_download(
                            message,
                            offset=stream_offset,
                            stride=stride,
                            limit=limit,
                            request_size=DOWNLOAD_REQUEST_SIZE,
                            chunk_size=DOWNLOAD_REQUEST_SIZE,
                            file_size=expected_size,
                        )
                    )

            try:
                while offset < expected_size:
                    remaining_chunks = (
                        expected_size - offset + DOWNLOAD_REQUEST_SIZE - 1
                    ) // DOWNLOAD_REQUEST_SIZE
                    active_streams = streams[: min(len(streams), remaining_chunks)]
                    tasks = [
                        asyncio.create_task(self._next_download_chunk(stream))
                        for stream in active_streams
                    ]
                    batch = asyncio.gather(*tasks)
                    try:
                        done, _ = await asyncio.wait(
                            {batch, low_speed_monitor},
                            timeout=self.config.download_stall_timeout_seconds,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if low_speed_monitor in done:
                            monitor_error = low_speed_monitor.exception()
                            if monitor_error is not None:
                                batch.cancel()
                                for task in tasks:
                                    if not task.done():
                                        task.cancel()
                                batch.add_done_callback(self._consume_future_result)
                                raise monitor_error
                            raise OSError(
                                f"下载消息 {message_id} 的低速监控意外提前结束"
                            )
                        if batch not in done:
                            batch.cancel()
                            for task in tasks:
                                if not task.done():
                                    task.cancel()
                            batch.add_done_callback(self._consume_future_result)
                            raise DownloadStalledError(
                                f"下载消息 {message_id} 连续 "
                                f"{self.config.download_stall_timeout_seconds:g} 秒无进展，"
                                f"已保存断点 {offset}/{expected_size} 字节"
                            )
                        chunks = batch.result()
                    except BaseException as exc:
                        if not batch.done():
                            batch.cancel()
                            batch.add_done_callback(self._consume_future_result)
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        pending = await self._settle_cancelled_tasks(
                            tasks,
                            min(
                                DOWNLOAD_STREAM_CLOSE_TIMEOUT_SECONDS,
                                self.config.download_stall_timeout_seconds,
                            ),
                        )
                        if isinstance(exc, DownloadStalledError) and pending:
                            LOGGER.warning(
                                "下载消息 %s 有 %s 条分片任务取消后仍未退出",
                                message_id,
                                pending,
                            )
                        raise

                    for chunk in chunks:
                        expected_chunk_size = min(
                            DOWNLOAD_REQUEST_SIZE, expected_size - offset
                        )
                        if len(chunk) != expected_chunk_size:
                            raise OSError(
                                "下载分片大小不匹配："
                                f"偏移 {offset}，预期 {expected_chunk_size} 字节，"
                                f"实际 {len(chunk)} 字节"
                            )
                        written = handle.write(chunk)
                        if written != len(chunk):
                            raise OSError(
                                "写入下载临时文件不完整："
                                f"预期 {len(chunk)} 字节，实际 {written} 字节"
                            )
                        offset += written
                    now = time.monotonic()
                    if (
                        now - last_progress_log_at >= DOWNLOAD_PROGRESS_INTERVAL_SECONDS
                        or offset >= expected_size
                    ):
                        elapsed = max(now - attempt_started_at, 0.001)
                        speed = (offset - attempt_start_offset) / elapsed
                        LOGGER.info(
                            "下载消息 %s 进度 %.1f%%（%s/%s 字节），"
                            "本次平均速度 %.2f MiB/s",
                            message_id,
                            offset * 100 / expected_size,
                            offset,
                            expected_size,
                            speed / (1024 * 1024),
                        )
                        last_progress_log_at = now
            finally:
                if not low_speed_monitor.done():
                    low_speed_monitor.cancel()
                await self._settle_cancelled_tasks(
                    [low_speed_monitor],
                    min(
                        DOWNLOAD_STREAM_CLOSE_TIMEOUT_SECONDS,
                        self.config.download_stall_timeout_seconds,
                    ),
                )
                await self._close_download_streams(streams, message_id)
            handle.flush()
            os.fsync(handle.fileno())

        actual_size = partial.stat().st_size
        if actual_size != expected_size:
            raise OSError(
                f"下载文件大小不匹配：预期 {expected_size} 字节，实际 {actual_size} 字节"
            )
        partial.replace(destination)
        return destination

    async def download_video(self, message_id: int, destination: Path) -> Path:
        source = await self._source_entity()

        async def operation() -> Path:
            message = await self.client.get_messages(source, ids=message_id)
            if message is None or not self._video_metadata(message)[0]:
                raise SourceMediaUnavailable(f"源消息 {message_id} 不再包含有效视频")
            return await self._download_message(message, message_id, destination)

        try:
            return await self._retry(operation, f"下载消息 {message_id}")
        except DownloadTooSlowError:
            await self._reconnect_after_slow_download(message_id)
            raise
        except CHANNEL_GROUP_ERRORS as exc:
            raise ChannelGroupUnavailable(
                f"下载源频道媒体失败：{type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _receipt_from_album(result: Any, description: str) -> DeliveryReceipt:
        messages = list(result) if isinstance(result, (list, tuple)) else [result]
        messages = [message for message in messages if message is not None]
        if len(messages) != 4:
            raise TelegramError(
                f"{description}返回了 {len(messages)} 个媒体项，预期为 4"
            )
        ordered = sorted(messages, key=lambda message: int(message.id))
        grouped_ids = {
            int(message.grouped_id)
            for message in ordered
            if message.grouped_id is not None
        }
        if len(grouped_ids) != 1:
            raise TelegramError(f"{description}的四个媒体项没有共享同一个 grouped_id")
        return DeliveryReceipt(
            message_ids=tuple(int(message.id) for message in ordered),
            grouped_id=grouped_ids.pop(),
        )

    async def send_staging_album(
        self,
        files: list[Path],
        caption_html: str,
        route_id: str,
        upload_started_at: str,
        *,
        video_info: VideoInfo,
        thumbnail: Path,
    ) -> DeliveryReceipt:
        if len(files) != 4:
            raise TelegramError("中转媒体组必须恰好包含 1 个视频和 3 张图片")
        staging = await self._staging_entity()
        route_caption = f"#{self._require_group().name}\nroute_id={route_id}"

        async def operation() -> Any:
            uploaded_video = await self.client.upload_file(str(files[0]))
            uploaded_thumbnail = await self.client.upload_file(str(thumbnail))
            video_media = InputMediaUploadedDocument(
                file=uploaded_video,
                thumb=uploaded_thumbnail,
                mime_type="video/mp4",
                attributes=[
                    DocumentAttributeFilename(files[0].name),
                    DocumentAttributeVideo(
                        duration=video_info.duration,
                        w=video_info.display_width,
                        h=video_info.display_height,
                        supports_streaming=True,
                        nosound=not video_info.has_audio,
                    ),
                ],
                nosound_video=True,
            )
            return await self.client.send_file(
                staging,
                [video_media, *(str(path) for path in files[1:])],
                caption=[caption_html, "", "", route_caption],
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
            except FLOOD_WAIT_ERRORS as exc:
                last_error = exc
                LOGGER.warning(
                    "上传中转媒体组触发 %s，等待 %s 秒",
                    type(exc).__name__,
                    exc.seconds,
                )
                await asyncio.sleep(max(1, exc.seconds))
            except CHANNEL_GROUP_ERRORS as exc:
                raise ChannelGroupUnavailable(
                    f"向中转频道上传失败：{type(exc).__name__}: {exc}"
                ) from exc
            except RuntimeError as exc:
                if UPLOAD_PART_FAILURE_RE.fullmatch(str(exc)) is None:
                    raise
                last_error = exc
                LOGGER.warning(
                    "上传中转大文件分片失败，第 %s 次结果不确定：%s",
                    index + 1,
                    exc,
                )
                receipt = await self.find_matching_staging_album(
                    upload_started_at, route_id
                )
                if receipt is not None:
                    return receipt
            except (TimeoutError, errors.ServerError, errors.RpcCallFailError, OSError) as exc:
                last_error = exc
                LOGGER.warning(
                    "上传中转媒体组第 %s 次结果不确定：%s", index + 1, exc
                )
                receipt = await self.find_matching_staging_album(
                    upload_started_at, route_id
                )
                if receipt is not None:
                    return receipt
        if result is None:
            receipt = await self.find_matching_staging_album(upload_started_at, route_id)
            if receipt is not None:
                return receipt
            raise TelegramError(f"上传中转媒体组重试耗尽：{last_error}") from last_error
        return self._receipt_from_album(result, "Telegram 中转上传")

    async def find_matching_staging_album(
        self, started_at: str, route_id: str
    ) -> DeliveryReceipt | None:
        staging = await self._staging_entity()
        since = datetime.fromisoformat(started_at)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        albums: dict[int, list[Any]] = defaultdict(list)
        try:
            async for message in self.client.iter_messages(staging, limit=100):
                if message.date < since:
                    break
                if message.grouped_id is not None:
                    albums[int(message.grouped_id)].append(message)
        except CHANNEL_GROUP_ERRORS as exc:
            raise ChannelGroupUnavailable(
                f"核对中转频道失败：{type(exc).__name__}: {exc}"
            ) from exc
        marker = f"route_id={route_id}"
        matches: list[DeliveryReceipt] = []
        for messages in albums.values():
            ordered = sorted(messages, key=lambda item: item.id)
            videos = sum(self._video_metadata(message)[0] for message in ordered)
            photos = sum(getattr(message, "photo", None) is not None for message in ordered)
            if (
                len(ordered) == 4
                and videos == 1
                and photos == 3
                and self._video_metadata(ordered[0])[0]
                and marker in str(ordered[-1].raw_text or "")
            ):
                matches.append(self._receipt_from_album(ordered, "中转恢复检查"))
        if len(matches) > 1:
            raise DeliveryUncertainError(
                f"中转频道存在多个 route_id={route_id} 的媒体组，请人工核对"
            )
        return matches[0] if matches else None

    async def send_album(
        self,
        files: list[Path],
        caption_html: str,
        caption_plain: str,
        upload_started_at: str,
        *,
        video_info: VideoInfo,
        thumbnail: Path,
    ) -> DeliveryReceipt:
        if len(files) != 4:
            raise TelegramError("目标媒体组必须恰好包含 1 个视频和 3 张图片")
        target = await self._target_entity()

        async def operation() -> Any:
            uploaded_video = await self.client.upload_file(str(files[0]))
            uploaded_thumbnail = await self.client.upload_file(str(thumbnail))
            video_media = InputMediaUploadedDocument(
                file=uploaded_video,
                thumb=uploaded_thumbnail,
                mime_type="video/mp4",
                attributes=[
                    DocumentAttributeFilename(files[0].name),
                    DocumentAttributeVideo(
                        duration=video_info.duration,
                        w=video_info.display_width,
                        h=video_info.display_height,
                        supports_streaming=True,
                        nosound=not video_info.has_audio,
                    ),
                ],
                nosound_video=True,
            )
            return await self.client.send_file(
                target,
                [video_media, *(str(path) for path in files[1:])],
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
            except FLOOD_WAIT_ERRORS as exc:
                last_error = exc
                LOGGER.warning(
                    "发送目标媒体组触发 %s，等待 %s 秒",
                    type(exc).__name__,
                    exc.seconds,
                )
                await asyncio.sleep(max(1, exc.seconds))
            except CHANNEL_GROUP_ERRORS as exc:
                raise ChannelGroupUnavailable(
                    f"向目标频道发布失败：{type(exc).__name__}: {exc}"
                ) from exc
            except RuntimeError as exc:
                if UPLOAD_PART_FAILURE_RE.fullmatch(str(exc)) is None:
                    raise
                last_error = exc
                LOGGER.warning(
                    "上传大文件分片失败，第 %s 次完整上传结果不确定：%s",
                    index + 1,
                    exc,
                )
                receipt = await self.find_matching_album(
                    upload_started_at, caption_plain
                )
                if receipt is not None:
                    return receipt
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
        try:
            async for message in self.client.iter_messages(target, limit=100):
                if message.date < since:
                    break
                if message.grouped_id is not None:
                    albums[int(message.grouped_id)].append(message)
        except CHANNEL_GROUP_ERRORS as exc:
            raise ChannelGroupUnavailable(
                f"核对目标频道失败：{type(exc).__name__}: {exc}"
            ) from exc
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

    async def doctor(self) -> dict[str, str]:
        group = self._require_group()
        source = await self._source_entity()
        staging = await self._staging_entity()
        if bool(getattr(staging, "noforwards", False)):
            raise ChannelGroupUnavailable("中转频道启用了禁止保存内容，机器人无法复制媒体")
        try:
            permissions = await self.client.get_permissions(staging, "me")
        except CHANNEL_GROUP_ERRORS as exc:
            raise ChannelGroupUnavailable(
                f"检查中转频道权限失败：{type(exc).__name__}: {exc}"
            ) from exc
        can_post = bool(
            getattr(permissions, "is_creator", False)
            or getattr(permissions, "post_messages", False)
        )
        if not can_post:
            raise ChannelGroupUnavailable("当前用户没有中转频道发帖权限")
        me = await self.client.get_me()
        return {
            "group": group.name,
            "account": str(me.id),
            "source": str(getattr(source, "title", group.source_channel)),
            "staging": str(
                getattr(staging, "title", self.config.delivery.staging_channel)
            ),
            "staging_post_permission": "ok",
            "staging_content_protection": "off",
        }


class _BotApiCallError(TelegramError):
    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
        safe_to_retry: bool = False,
        uncertain: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.safe_to_retry = safe_to_retry
        self.uncertain = uncertain


class BotDeliveryGateway:
    """Copies staged albums through the official Telegram Bot API."""

    def __init__(
        self,
        config: AppConfig,
        group: ChannelGroupConfig | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        owns_client: bool | None = None,
        bot_identity: dict[str, Any] | None = None,
    ):
        self.config = config
        self.group = group
        self._owns_client = client is None if owns_client is None else owns_client
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0, read=20.0, write=20.0, pool=10.0)
        )
        self._base_url = f"https://api.telegram.org/bot{config.reporting.bot_token}"
        self._bot_identity = bot_identity

    def for_group(self, group: ChannelGroupConfig) -> BotDeliveryGateway:
        return BotDeliveryGateway(
            self.config,
            group,
            client=self.client,
            owns_client=False,
            bot_identity=self._bot_identity,
        )

    def _require_group(self) -> ChannelGroupConfig:
        if self.group is None:
            raise RuntimeError("当前 BotDeliveryGateway 尚未绑定频道组")
        return self.group

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self.client.post(f"{self._base_url}/{method}", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _BotApiCallError(
                f"Bot API 连接失败：{type(exc).__name__}",
                safe_to_retry=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise _BotApiCallError(
                f"Bot API 结果不确定：{type(exc).__name__}", uncertain=True
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise _BotApiCallError(
                f"Bot API 返回无效 JSON（HTTP {response.status_code}）",
                uncertain=True,
            ) from exc
        if not isinstance(body, dict):
            raise _BotApiCallError("Bot API 返回格式无效", uncertain=True)
        if response.is_error or not body.get("ok"):
            description = str(body.get("description") or f"HTTP {response.status_code}")
            parameters = body.get("parameters")
            retry_after = None
            if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
                try:
                    retry_after = int(parameters["retry_after"])
                except (TypeError, ValueError):
                    retry_after = None
            try:
                error_code = int(body.get("error_code", response.status_code))
            except (TypeError, ValueError):
                error_code = response.status_code
            raise _BotApiCallError(
                description,
                error_code=error_code,
                retry_after=retry_after,
                safe_to_retry=error_code == 429,
                uncertain=error_code >= 500,
            )
        return body.get("result")

    @staticmethod
    def _is_channel_error(exc: _BotApiCallError) -> bool:
        message = str(exc).casefold()
        markers = (
            "chat not found",
            "bot is not a member",
            "bot was kicked",
            "not enough rights",
            "administrator rights",
            "have no rights",
            "forbidden",
            "protected content",
            "can't be forwarded",
            "can't be copied",
        )
        return exc.error_code == 403 or any(marker in message for marker in markers)

    async def _ensure_bot(self) -> dict[str, Any]:
        if self._bot_identity is None:
            result = await self._call("getMe", {})
            if not isinstance(result, dict) or not result.get("is_bot"):
                raise TelegramError("TG_REPORT_BOT_TOKEN 未识别为机器人")
            self._bot_identity = result
        return self._bot_identity

    async def login(self) -> None:
        bot = await self._ensure_bot()
        LOGGER.info("Telegram 分发机器人验证成功：bot_id=%s", bot.get("id"))

    async def connect(self) -> None:
        await self._ensure_bot()

    async def disconnect(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def copy_album(
        self,
        staging_message_ids: tuple[int, ...],
        delivery_started_at: str,
    ) -> DeliveryReceipt:
        del delivery_started_at
        if len(staging_message_ids) != 4:
            raise StagingMediaUnavailable("数据库没有保存完整的四项中转消息 ID")
        message_ids = tuple(sorted(int(item) for item in staging_message_ids))
        group = self._require_group()
        payload = {
            "chat_id": group.target_channel,
            "from_chat_id": self.config.delivery.staging_channel,
            "message_ids": list(message_ids),
            "remove_caption": True,
        }
        last_error: _BotApiCallError | None = None
        for index, delay in enumerate((0, *self.config.retry_delays_seconds)):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await self._call("copyMessages", payload)
            except _BotApiCallError as exc:
                if self._is_channel_error(exc):
                    raise ChannelGroupUnavailable(
                        f"机器人无法复制中转媒体：{exc}"
                    ) from exc
                message = str(exc).casefold()
                if "message to copy not found" in message or "message_id_invalid" in message:
                    raise StagingMediaUnavailable(f"中转媒体已不完整：{exc}") from exc
                if exc.uncertain:
                    raise DeliveryUncertainError(
                        "Bot API 复制请求的结果无法确认，"
                        "为避免重复发布已暂停当前频道组"
                    ) from exc
                last_error = exc
                LOGGER.warning(
                    "Bot API 复制媒体组第 %s 次失败：%s", index + 1, exc
                )
                if exc.retry_after:
                    await asyncio.sleep(max(1, exc.retry_after))
                if not exc.safe_to_retry and index + 1 >= len(
                    (0, *self.config.retry_delays_seconds)
                ):
                    break
                continue
            if not isinstance(result, list):
                raise DeliveryUncertainError("Bot API copyMessages 返回格式无效")
            try:
                copied_ids = tuple(int(item["message_id"]) for item in result)
            except (KeyError, TypeError, ValueError) as exc:
                raise DeliveryUncertainError(
                    "Bot API copyMessages 没有返回完整的目标消息 ID"
                ) from exc
            if len(copied_ids) != 4 or tuple(sorted(copied_ids)) != copied_ids:
                raise DeliveryUncertainError(
                    f"Bot API 复制后返回 {len(copied_ids)} 项，预期为有序的 4 项"
                )
            return DeliveryReceipt(copied_ids, copied_ids[0])
        raise TelegramError(f"Bot API 复制媒体组重试耗尽：{last_error}") from last_error

    async def recover_delivery(
        self,
        staging_message_ids: tuple[int, ...],
        delivery_started_at: str,
    ) -> DeliveryReceipt | None:
        del staging_message_ids, delivery_started_at
        raise DeliveryUncertainError(
            "上次 Bot API 复制在返回目标消息 ID 前中断，"
            "机器人不能扫描频道历史，请人工核对目标频道"
        )

    async def apply_caption(
        self,
        receipt: DeliveryReceipt,
        caption_html: str,
        caption_plain: str,
    ) -> DeliveryReceipt:
        group = self._require_group()
        payload = {
            "chat_id": group.target_channel,
            "message_id": receipt.message_ids[0],
            "caption": caption_html,
            "parse_mode": "HTML",
        }
        last_error: _BotApiCallError | None = None
        for index, delay in enumerate((0, *self.config.retry_delays_seconds)):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await self._call("editMessageCaption", payload)
            except _BotApiCallError as exc:
                message = str(exc).casefold()
                if "message is not modified" in message:
                    return receipt
                if "message to edit not found" in message:
                    raise DeliveryUncertainError(
                        "目标媒体组的首条视频已不存在，请人工核对"
                    ) from exc
                if self._is_channel_error(exc):
                    raise ChannelGroupUnavailable(
                        f"机器人无法编辑目标文案：{exc}"
                    ) from exc
                last_error = exc
                LOGGER.warning(
                    "Bot API 写入目标文案第 %s 次失败：%s", index + 1, exc
                )
                if exc.retry_after:
                    await asyncio.sleep(max(1, exc.retry_after))
                continue
            if not isinstance(result, dict):
                raise TelegramError("Bot API editMessageCaption 返回格式无效")
            if int(result.get("message_id", 0)) != receipt.message_ids[0]:
                raise DeliveryUncertainError("目标文案返回了不同的消息 ID")
            if "video" not in result:
                raise DeliveryUncertainError("目标媒体组第一项不是视频")
            media_group_id = result.get("media_group_id")
            if not media_group_id:
                raise DeliveryUncertainError("目标媒体组没有 media_group_id")
            if str(result.get("caption") or "") != caption_plain:
                raise TelegramError("目标媒体组文案校验失败")
            return DeliveryReceipt(receipt.message_ids, str(media_group_id))
        raise TelegramError(f"Bot API 写入目标文案重试耗尽：{last_error}") from last_error

    async def doctor(self) -> dict[str, str]:
        group = self._require_group()
        bot = await self._ensure_bot()
        bot_id = int(bot["id"])
        try:
            staging = await self._call(
                "getChat", {"chat_id": self.config.delivery.staging_channel}
            )
            staging_member = await self._call(
                "getChatMember",
                {"chat_id": self.config.delivery.staging_channel, "user_id": bot_id},
            )
            target = await self._call("getChat", {"chat_id": group.target_channel})
            target_member = await self._call(
                "getChatMember", {"chat_id": group.target_channel, "user_id": bot_id}
            )
        except _BotApiCallError as exc:
            raise ChannelGroupUnavailable(f"Bot API 频道权限检查失败：{exc}") from exc
        if not isinstance(staging, dict) or not isinstance(staging_member, dict):
            raise ChannelGroupUnavailable("Bot API 中转频道检查返回格式无效")
        if bool(staging.get("has_protected_content")):
            raise ChannelGroupUnavailable("中转频道启用了禁止保存内容")
        if staging_member.get("status") not in {"administrator", "creator", "member"}:
            raise ChannelGroupUnavailable("分发机器人不在中转频道中")
        if not isinstance(target, dict) or not isinstance(target_member, dict):
            raise ChannelGroupUnavailable("Bot API 目标频道检查返回格式无效")
        is_creator = target_member.get("status") == "creator"
        if target_member.get("status") != "administrator" and not is_creator:
            raise ChannelGroupUnavailable("分发机器人不是目标频道管理员")
        if not is_creator and not bool(target_member.get("can_post_messages")):
            raise ChannelGroupUnavailable("分发机器人没有目标频道发帖权限")
        if not is_creator and not bool(target_member.get("can_edit_messages")):
            raise ChannelGroupUnavailable("分发机器人没有目标频道编辑消息权限")
        return {
            "delivery_bot": str(bot_id),
            "staging": str(staging.get("title") or self.config.delivery.staging_channel),
            "target": str(target.get("title") or group.target_channel),
            "target_post_permission": "ok",
            "target_edit_permission": "ok",
        }
