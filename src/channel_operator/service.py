from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .captions import build_caption
from .config import AppConfig, ChannelGroupConfig
from .database import StateDatabase
from .media import InvalidSourceMedia, MediaProcessor
from .models import CaptionResult, DeliveryReceipt, MediaGroup, RunSummary
from .reporting import BotReporter
from .telegram import (
    BotDeliveryGateway,
    ChannelGroupUnavailable,
    DeliveryUncertainError,
    DownloadTooSlowError,
    SourceMediaUnavailable,
    StagingMediaUnavailable,
    TelegramError,
    TelegramGateway,
)

LOGGER = logging.getLogger(__name__)


class AutomationService:
    def __init__(
        self,
        config: AppConfig,
        group: ChannelGroupConfig,
        database: StateDatabase,
        telegram: TelegramGateway,
        media: MediaProcessor,
        reporter: BotReporter,
        delivery: BotDeliveryGateway | None = None,
    ):
        self.config = config
        self.group = group
        self.database = database
        self.telegram = telegram
        self.delivery = delivery or telegram
        self.media = media
        self.reporter = reporter
        self.source_key = str(group.source_channel)
        self.intro_footer = (
            config.intro_footer
            if group.intro_footer is None
            else group.intro_footer
        )
        self.watermark_text = (
            config.watermark_text
            if group.watermark_text is None
            else group.watermark_text
        )

    def _route_id(self, group: MediaGroup) -> str:
        return f"{self.group.name}:{self.group.source_channel}:{group.grouped_id}"

    def _published_date(self, run_date: str, continuous: bool) -> str:
        return self._today() if continuous else run_date

    def _mark_published(
        self,
        group: MediaGroup,
        receipt: DeliveryReceipt,
        run_date: str,
        *,
        continuous: bool,
        reconciled: bool = False,
    ) -> None:
        published_date = self._published_date(run_date, continuous)
        self.database.mark_published(
            self.source_key,
            group.grouped_id,
            list(receipt.message_ids),
            receipt.grouped_id,
            published_date,
        )
        if continuous:
            values = {"published": 1}
            if reconciled:
                values["reconciled"] = 1
            self._record_stats(stats_date=published_date, **values)

    async def index(self) -> int:
        checkpoint = self.database.checkpoint(self.source_key)
        batch = []
        newest_id = checkpoint
        scanned = 0
        async for message in self.telegram.scan_messages(checkpoint):
            batch.append(message)
            newest_id = max(newest_id, message.message_id)
            scanned += 1
            if len(batch) >= 200:
                self.database.save_messages(self.source_key, batch, newest_id)
                batch.clear()
                LOGGER.info("已扫描 %s 条新消息，检查点 %s", scanned, newest_id)
        if batch or newest_id != checkpoint:
            self.database.save_messages(self.source_key, batch, newest_id)
        group_count = self.database.refresh_groups(self.source_key)
        LOGGER.info(
            "频道组 %s 索引完成：新消息 %s 条，媒体组总数 %s",
            self.group.display_name,
            scanned,
            group_count,
        )
        return group_count

    def _today(self) -> str:
        return datetime.now(self.config.timezone).date().isoformat()

    def _record_stats(self, *, stats_date: str | None = None, **values: int) -> None:
        self.database.record_daily_stats(stats_date or self._today(), **values)

    async def dry_run(self, *, continuous: bool = False) -> list[tuple[int, str]]:
        await self.index()
        today = self._today()
        candidates = self.database.preview_candidates(
            self.source_key,
            self.config.minimum_source_short_edge,
            self.config.album_settle_seconds,
            self.config.max_candidates_per_run,
            retryable_before_date=today if continuous else None,
        )
        previews = []
        for candidate in candidates:
            caption = build_caption(
                candidate.caption,
                self.config.keep_tags,
                self.config.drop_tags,
                intro_footer=self.intro_footer,
                limit=self.config.caption_limit,
            )
            if not caption.plain.strip():
                LOGGER.info("预览跳过空文案媒体组 %s", candidate.grouped_id)
                continue
            previews.append((candidate.grouped_id, caption.plain))
            if len(previews) >= self.group.daily_success_count:
                break
        return previews

    def _work_directory(self, group: MediaGroup) -> Path:
        group_root = self.config.work_dir / self.group.name
        group_root.mkdir(parents=True, exist_ok=True)
        return group_root / f"{group.grouped_id}-{uuid.uuid4().hex[:8]}"

    def _cleanup(self, directory: Path) -> None:
        work_root = self.config.work_dir.resolve()
        target = directory.resolve()
        if target == work_root or work_root not in target.parents:
            raise RuntimeError(f"拒绝清理工作目录以外的路径：{target}")
        if target.exists():
            shutil.rmtree(target)
        group_root = (self.config.work_dir / self.group.name).resolve()
        if group_root.parent == work_root and group_root.exists():
            with suppress(OSError):
                group_root.rmdir()

    async def _reconcile(
        self, group: MediaGroup, run_date: str, *, continuous: bool
    ) -> bool:
        if group.status != "uploading" or not group.upload_started_at:
            return False
        try:
            receipt = await self.telegram.find_matching_album(
                group.upload_started_at, group.attempt_caption_plain or ""
            )
        except ChannelGroupUnavailable as exc:
            reason = (
                "旧版直传状态无法核对；主账号已无法访问目标频道，"
                "为避免重复发布已停止自动重发"
            )
            self.database.mark_failure(
                self.source_key, group.grouped_id, f"{reason}：{exc}", permanent=True
            )
            if continuous:
                self._record_stats(rejected=1)
            await self.reporter.send(
                f"⚠️ 频道组 {self.group.display_name}\n"
                f"源媒体组 {group.grouped_id} 的旧版上传结果无法确认。\n"
                f"{reason}，请人工核对。"
            )
            return False
        if receipt is None:
            self.database.mark_failure(
                self.source_key,
                group.grouped_id,
                "上次上传结果不确定，未自动重发，请人工核对目标频道",
                permanent=True,
            )
            if continuous:
                self._record_stats(rejected=1)
            await self.reporter.send(
                f"⚠️ 频道组 {self.group.display_name}\n"
                f"源媒体组 {group.grouped_id} 的上传结果不确定，"
                "已暂停该媒体组，请人工核对目标频道。"
            )
            return False
        published_date = self._today() if continuous else run_date
        self.database.mark_published(
            self.source_key,
            group.grouped_id,
            list(receipt.message_ids),
            receipt.grouped_id,
            published_date,
        )
        if continuous:
            self._record_stats(
                stats_date=published_date,
                published=1,
                reconciled=1,
            )
        return True

    async def _deliver_staged(
        self,
        group: MediaGroup,
        receipt: DeliveryReceipt,
        caption_html: str,
        caption_plain: str,
        run_date: str,
        *,
        continuous: bool,
        delivery_started_at: str | None = None,
        recovered: bool = False,
    ) -> bool:
        target_receipt: DeliveryReceipt | None = None
        if delivery_started_at:
            target_receipt = await self.delivery.recover_delivery(
                receipt.message_ids, delivery_started_at
            )
            recovered = recovered or target_receipt is not None
        if target_receipt is None:
            delivery_started_at = self.database.begin_delivery(
                self.source_key, group.grouped_id
            )
            target_receipt = await self.delivery.copy_album(
                receipt.message_ids, delivery_started_at
            )
        self.database.mark_caption_pending(
            self.source_key, group.grouped_id, target_receipt
        )
        try:
            verified_receipt = await self.delivery.apply_caption(
                target_receipt, caption_html, caption_plain
            )
            if verified_receipt is not None:
                target_receipt = verified_receipt
        except Exception as exc:
            self.database.mark_caption_pending(
                self.source_key, group.grouped_id, target_receipt, str(exc)
            )
            raise
        self._mark_published(
            group,
            target_receipt,
            run_date,
            continuous=continuous,
            reconciled=recovered,
        )
        return recovered

    async def _resume_staged(
        self, group: MediaGroup, run_date: str, *, continuous: bool
    ) -> bool:
        if group.status == "delivery_uncertain":
            raise DeliveryUncertainError(
                "上次机器人分发结果存在多个可能匹配项，请人工核对目标频道"
            )
        caption_html = group.attempt_caption_html or ""
        caption_plain = group.attempt_caption_plain or ""
        if not caption_plain.strip() or not caption_html.strip():
            raise TelegramError("数据库缺少中转媒体组的固定文案，无法安全恢复")
        if group.status == "staging_uploading":
            if not group.staging_upload_started_at:
                raise TelegramError("数据库缺少中转上传开始时间")
            staging_receipt = await self.telegram.find_matching_staging_album(
                group.staging_upload_started_at, self._route_id(group)
            )
            if staging_receipt is None:
                raise StagingMediaUnavailable(
                    "上次中转上传未找到匹配媒体组，将重新处理源素材"
                )
            self.database.mark_staged(
                self.source_key,
                group.grouped_id,
                self.config.delivery.staging_channel,
                staging_receipt,
            )
            return await self._deliver_staged(
                group,
                staging_receipt,
                caption_html,
                caption_plain,
                run_date,
                continuous=continuous,
                recovered=True,
            )
        staging_receipt = DeliveryReceipt(
            group.staging_message_ids,
            group.staging_grouped_id or 0,
        )
        if (
            len(staging_receipt.message_ids) != 4
            or not isinstance(staging_receipt.grouped_id, int)
            or staging_receipt.grouped_id <= 0
        ):
            raise StagingMediaUnavailable("数据库中的中转媒体组记录不完整")
        if group.status == "caption_pending" and group.destination_message_ids:
            target_receipt = DeliveryReceipt(
                group.destination_message_ids,
                group.destination_grouped_id or 0,
            )
            try:
                verified_receipt = await self.delivery.apply_caption(
                    target_receipt, caption_html, caption_plain
                )
                if verified_receipt is not None:
                    target_receipt = verified_receipt
            except Exception as exc:
                self.database.mark_caption_pending(
                    self.source_key, group.grouped_id, target_receipt, str(exc)
                )
                raise
            self._mark_published(
                group,
                target_receipt,
                run_date,
                continuous=continuous,
                reconciled=True,
            )
            return True
        return await self._deliver_staged(
            group,
            staging_receipt,
            caption_html,
            caption_plain,
            run_date,
            continuous=continuous,
            delivery_started_at=(
                group.delivery_started_at
                if group.status == "delivering"
                else None
            ),
            recovered=group.status
            in {"delivering", "delivery_retryable", "caption_pending"},
        )

    async def _process_group(
        self,
        group: MediaGroup,
        run_date: str,
        caption: CaptionResult,
        *,
        continuous: bool,
    ) -> None:
        directory = self._work_directory(group)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            self.database.set_status(self.source_key, group.grouped_id, "downloading")
            self.media.check_disk(group.file_size)
            source = await self.telegram.download_video(
                group.video_message_id, directory / "source_video.mp4"
            )
            source_info = await self.media.probe(source)
            self.media.validate_source(source_info)

            self.database.set_status(self.source_key, group.grouped_id, "transcoding")
            clipped = directory / "source_first_third.mkv"
            clipped_info = await self.media.cut_first_third(
                source, clipped, source_info
            )
            output = directory / "video.mp4"
            output_info = await self.media.transcode(
                clipped,
                output,
                clipped_info,
                watermark_text=self.watermark_text,
            )
            frames = await self.media.screenshots(output, output_info.duration, directory)
            thumbnail = await self.media.thumbnail(
                output, output_info.duration, directory / "video_thumb.jpg"
            )

            upload_started_at = self.database.begin_staging_upload(
                self.source_key,
                group.grouped_id,
                self.config.delivery.staging_channel,
                caption.html,
                caption.plain,
            )
            staging_receipt = await self.telegram.send_staging_album(
                [output, *frames],
                caption.html,
                self._route_id(group),
                upload_started_at,
                video_info=output_info,
                thumbnail=thumbnail,
            )
            self.database.mark_staged(
                self.source_key,
                group.grouped_id,
                self.config.delivery.staging_channel,
                staging_receipt,
            )
            await self._deliver_staged(
                group,
                staging_receipt,
                caption.html,
                caption.plain,
                run_date,
                continuous=continuous,
            )
        finally:
            self._cleanup(directory)

    async def run_once(
        self,
        *,
        continuous: bool = False,
        safe_point: Callable[[], Awaitable[None]] | None = None,
    ) -> RunSummary:
        await self.index()
        run_date = self._today()
        summary = RunSummary(
            run_date=run_date,
            published=(
                0
                if continuous
                else self.database.published_count(self.source_key, run_date)
            ),
        )
        attempted_groups: set[int] = set()
        deadline = time.monotonic() + self.config.max_runtime_hours * 3600

        while (
            summary.published < self.group.daily_success_count
            and summary.attempted < self.config.max_candidates_per_run
            and time.monotonic() < deadline
        ):
            if safe_point is not None:
                await safe_point()
            group = self.database.next_candidate(
                self.source_key,
                run_date,
                self.config.minimum_source_short_edge,
                self.config.album_settle_seconds,
                attempted_groups,
                retryable_before_date=self._today() if continuous else None,
            )
            if group is None:
                break
            attempted_groups.add(group.grouped_id)
            if group.status == "uploading":
                if await self._reconcile(group, run_date, continuous=continuous):
                    summary.published += 1
                    summary.reconciled += 1
                else:
                    summary.rejected += 1
                continue
            if group.status in {
                "staging_uploading",
                "staged",
                "delivering",
                "caption_pending",
                "delivery_retryable",
                "delivery_uncertain",
            }:
                try:
                    reconciled = await self._resume_staged(
                        group, run_date, continuous=continuous
                    )
                except StagingMediaUnavailable as exc:
                    LOGGER.warning("媒体组 %s 中转记录失效：%s", group.grouped_id, exc)
                    self.database.mark_failure(
                        self.source_key, group.grouped_id, str(exc), permanent=False
                    )
                    summary.retryable_failures += 1
                    if continuous:
                        self._record_stats(retryable_failures=1)
                except DeliveryUncertainError as exc:
                    self.database.mark_delivery_failure(
                        self.source_key,
                        group.grouped_id,
                        str(exc),
                        uncertain=True,
                    )
                    if continuous:
                        self._record_stats(retryable_failures=1)
                    raise
                except ChannelGroupUnavailable as exc:
                    if self.database.group_status(
                        self.source_key, group.grouped_id
                    ) != "caption_pending":
                        self.database.mark_delivery_failure(
                            self.source_key, group.grouped_id, str(exc)
                        )
                    if continuous:
                        self._record_stats(retryable_failures=1)
                    raise
                except Exception as exc:
                    LOGGER.exception("媒体组 %s 中转分发恢复失败", group.grouped_id)
                    if self.database.group_status(
                        self.source_key, group.grouped_id
                    ) != "caption_pending":
                        self.database.mark_delivery_failure(
                            self.source_key, group.grouped_id, str(exc)
                        )
                    summary.retryable_failures += 1
                    if continuous:
                        self._record_stats(retryable_failures=1)
                else:
                    summary.published += 1
                    if reconciled:
                        summary.reconciled += 1
                continue

            summary.attempted += 1
            attempt_date = self._today() if continuous else run_date
            self.database.begin_attempt(
                self.source_key, group.grouped_id, attempt_date
            )
            if continuous:
                self._record_stats(stats_date=attempt_date, attempted=1)
            caption = build_caption(
                group.caption,
                self.config.keep_tags,
                self.config.drop_tags,
                intro_footer=self.intro_footer,
                limit=self.config.caption_limit,
            )
            if not caption.plain.strip():
                reason = "标签过滤后没有标签，且未找到有效简介，处理后文案为空"
                LOGGER.warning("媒体组 %s 文案为空，已跳过并选择替补", group.grouped_id)
                self.database.mark_failure(
                    self.source_key, group.grouped_id, reason, permanent=True
                )
                summary.rejected += 1
                if continuous:
                    self._record_stats(rejected=1)
                continue
            try:
                remaining = max(1, deadline - time.monotonic())
                await asyncio.wait_for(
                    self._process_group(
                        group,
                        run_date,
                        caption,
                        continuous=continuous,
                    ),
                    timeout=remaining,
                )
            except (InvalidSourceMedia, SourceMediaUnavailable) as exc:
                LOGGER.warning("媒体组 %s 永久无效：%s", group.grouped_id, exc)
                self.database.mark_failure(
                    self.source_key, group.grouped_id, str(exc), permanent=True
                )
                summary.rejected += 1
                if continuous:
                    self._record_stats(rejected=1)
            except DeliveryUncertainError as exc:
                self.database.mark_delivery_failure(
                    self.source_key, group.grouped_id, str(exc), uncertain=True
                )
                if continuous:
                    self._record_stats(retryable_failures=1)
                raise
            except ChannelGroupUnavailable as exc:
                current_status = self.database.group_status(
                    self.source_key, group.grouped_id
                )
                if current_status == "caption_pending":
                    pass
                elif self.database.has_staging_album(self.source_key, group.grouped_id):
                    self.database.mark_delivery_failure(
                        self.source_key, group.grouped_id, str(exc)
                    )
                else:
                    self.database.mark_failure(
                        self.source_key, group.grouped_id, str(exc), permanent=False
                    )
                if continuous:
                    self._record_stats(retryable_failures=1)
                raise
            except DownloadTooSlowError as exc:
                LOGGER.warning(
                    "媒体组 %s 下载持续低速，已停止并选择替补：%s",
                    group.grouped_id,
                    exc,
                )
                self.database.mark_failure(
                    self.source_key, group.grouped_id, str(exc), permanent=False
                )
                summary.retryable_failures += 1
                if continuous:
                    self._record_stats(retryable_failures=1)
            except Exception as exc:
                LOGGER.exception("媒体组 %s 处理失败", group.grouped_id)
                current_status = self.database.group_status(
                    self.source_key, group.grouped_id
                )
                if current_status == "caption_pending":
                    pass
                elif self.database.has_staging_album(self.source_key, group.grouped_id):
                    self.database.mark_delivery_failure(
                        self.source_key, group.grouped_id, str(exc)
                    )
                else:
                    self.database.mark_failure(
                        self.source_key, group.grouped_id, str(exc), permanent=False
                    )
                summary.retryable_failures += 1
                if continuous:
                    self._record_stats(retryable_failures=1)
            else:
                summary.published += 1
        return summary
