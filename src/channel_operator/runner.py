from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from .config import AppConfig, ChannelGroupConfig
from .database import DatabaseIdentityError, StateDatabase
from .indexing import SourceIndexCoordinator
from .media import MediaProcessor
from .models import GroupRunResult, RunSummary
from .reporting import BotReporter
from .service import AutomationService
from .telegram import BotDeliveryGateway, ChannelGroupUnavailable, TelegramGateway

LOGGER = logging.getLogger(__name__)


def format_duration(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(elapsed_seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if days or hours:
        parts.append(f"{hours}小时")
    if days or hours or minutes:
        parts.append(f"{minutes}分钟")
    parts.append(f"{seconds}秒")
    return "".join(parts)


class MultiChannelRunner:
    def __init__(
        self,
        config: AppConfig,
        telegram: TelegramGateway,
        media: MediaProcessor,
        reporter: BotReporter,
        delivery: BotDeliveryGateway | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.telegram = telegram
        self.delivery = delivery or telegram
        self.media = media
        self.reporter = reporter
        self.clock = clock
        self.now = now or (lambda: datetime.now(self.config.timezone))

    @staticmethod
    def _database(group: ChannelGroupConfig) -> StateDatabase:
        return StateDatabase(
            group.database_path,
            group_name=group.name,
            source_channel=group.source_channel,
            target_channel=group.target_channel,
        )

    async def _report_skipped(
        self,
        group: ChannelGroupConfig,
        reason: str,
        published: int,
    ) -> None:
        remaining = max(0, group.daily_success_count - published)
        remark_line = f"备注：{group.remark}\n" if group.remark else ""
        await self.reporter.send(
            "⚠️ 频道组已跳过\n"
            f"组名：{group.name}\n"
            f"{remark_line}"
            f"源频道：{group.source_channel}\n"
            f"目标频道：{group.target_channel}\n"
            f"错误：{reason}\n"
            f"已完成：{published}/{group.daily_success_count}\n"
            f"剩余目标：{remaining}"
        )

    async def run_once(
        self,
        groups: tuple[ChannelGroupConfig, ...],
        *,
        continuous: bool = False,
        send_summary: bool = True,
        paused_groups: dict[str, str] | None = None,
        safe_point: Callable[[], Awaitable[None]] | None = None,
    ) -> list[GroupRunResult]:
        started_at = self.clock()
        results: list[GroupRunResult] = []
        paused = paused_groups if paused_groups is not None else {}
        indexer = SourceIndexCoordinator(self.config)
        for group in groups:
            if continuous and group.name in paused:
                reason = paused[group.name]
                database = None
                try:
                    database = self._database(group)
                    database.record_daily_stats(
                        self.now().date().isoformat(),
                        paused_reason=reason,
                    )
                except Exception:
                    LOGGER.debug(
                        "记录暂停频道组 %s 的每日状态失败", group.display_name
                    )
                finally:
                    if database is not None:
                        database.close()
                results.append(
                    GroupRunResult(
                        group=group,
                        skipped_reason=reason,
                        published_before_skip=0,
                    )
                )
                continue
            LOGGER.info("开始处理频道组 %s", group.display_name)
            database: StateDatabase | None = None
            published = 0
            try:
                database = self._database(group)
                run_date = self.now().date().isoformat()
                published = database.published_count(str(group.source_channel), run_date)
                gateway = self.telegram.for_group(group)
                await gateway.doctor()
                delivery = self.delivery.for_group(group)
                if self.delivery is not self.telegram:
                    await delivery.doctor()
                await indexer.prepare_group(group, database, gateway)
                service = AutomationService(
                    self.config,
                    group,
                    database,
                    gateway,
                    self.media,
                    self.reporter,
                    delivery=delivery,
                )
                summary = await service.run_once(
                    continuous=continuous,
                    safe_point=safe_point,
                    index_before_run=False,
                )
                results.append(GroupRunResult(group=group, summary=summary))
                LOGGER.info(
                    "频道组 %s 完成：成功 %s/%s",
                    group.display_name,
                    summary.published,
                    group.daily_success_count,
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("频道组 %s 失败，已跳过", group.display_name)
                if database is not None:
                    try:
                        current_date = (
                            self.now().date().isoformat()
                        )
                        if continuous:
                            published = 0
                        else:
                            published = database.published_count(
                                str(group.source_channel), current_date
                            )
                    except Exception:
                        LOGGER.exception(
                            "读取频道组 %s 的完成数量失败", group.display_name
                        )
                results.append(
                    GroupRunResult(
                        group=group,
                        skipped_reason=reason,
                        published_before_skip=published,
                    )
                )
                pausable = (
                    isinstance(exc, (ChannelGroupUnavailable, DatabaseIdentityError))
                    or isinstance(exc, sqlite3.DatabaseError)
                    and not isinstance(exc, sqlite3.OperationalError)
                )
                if continuous and pausable:
                    paused[group.name] = reason
                    if database is not None:
                        try:
                            database.record_daily_stats(
                                self.now().date().isoformat(),
                                paused_reason=reason,
                            )
                        except Exception:
                            LOGGER.debug(
                                "记录频道组 %s 暂停原因失败", group.display_name
                            )
                await self._report_skipped(group, reason, published)
            finally:
                if database is not None:
                    database.close()
            if safe_point is not None:
                await safe_point()
        indexer.close()
        elapsed_seconds = max(0.0, self.clock() - started_at)
        completed_at = self.now()
        run_date = completed_at.date().isoformat()
        LOGGER.info("所有频道组处理完成，总耗时 %s", format_duration(elapsed_seconds))
        if send_summary:
            if continuous:
                report_text = self.format_continuous_cycle_summary(
                    results,
                    completed_at=completed_at,
                    elapsed_seconds=elapsed_seconds,
                )
            else:
                report_text = self.format_summary(
                    results,
                    run_date=run_date,
                    elapsed_seconds=elapsed_seconds,
                )
            await self.reporter.send(report_text)
        return results

    @staticmethod
    def _format_result_blocks(
        results: list[GroupRunResult],
        *,
        include_skipped_metrics: bool = False,
    ) -> list[str]:
        blocks: list[str] = []
        for result in results:
            group = result.group
            remark_line = f"备注：{group.remark}\n" if group.remark else ""
            if result.summary is None:
                metrics = (
                    "本次尝试：0\n"
                    "永久跳过：0\n"
                    "可重试失败：0\n"
                    "恢复确认：0\n"
                    if include_skipped_metrics
                    else ""
                )
                blocks.append(
                    f"[{group.name}] 已跳过\n"
                    f"{remark_line}"
                    f"成功：{result.published}/{group.daily_success_count}\n"
                    f"{metrics}"
                    f"原因：{result.skipped_reason or '未知错误'}"
                )
                continue
            summary: RunSummary = result.summary
            blocks.append(
                f"[{group.name}] {'完成' if result.succeeded else '未达目标'}\n"
                f"{remark_line}"
                f"成功：{summary.published}/{group.daily_success_count}\n"
                f"本次尝试：{summary.attempted}\n"
                f"永久跳过：{summary.rejected}\n"
                f"可重试失败：{summary.retryable_failures}\n"
                f"恢复确认：{summary.reconciled}"
            )
        return blocks

    @classmethod
    def format_continuous_cycle_summary(
        cls,
        results: list[GroupRunResult],
        *,
        completed_at: datetime,
        elapsed_seconds: float = 0.0,
    ) -> str:
        blocks = [
            "Telegram 多频道连续运营本轮汇总\n"
            f"本轮结束：{completed_at.isoformat(sep=' ', timespec='seconds')}\n"
            f"所有频道组总耗时：{format_duration(elapsed_seconds)}"
        ]
        blocks.extend(
            cls._format_result_blocks(results, include_skipped_metrics=True)
        )
        return "\n\n".join(blocks)

    @staticmethod
    def format_summary(
        results: list[GroupRunResult],
        *,
        run_date: str | None = None,
        elapsed_seconds: float = 0.0,
    ) -> str:
        resolved_date = run_date or next(
            (
                result.summary.run_date
                for result in results
                if result.summary is not None
            ),
            "unknown",
        )
        blocks = [
            f"Telegram 多频道自动运营任务 {resolved_date}\n"
            f"所有频道组总耗时：{format_duration(elapsed_seconds)}"
        ]
        blocks.extend(MultiChannelRunner._format_result_blocks(results))
        return "\n\n".join(blocks)
