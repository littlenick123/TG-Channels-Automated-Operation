from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from .config import AppConfig, ChannelGroupConfig
from .database import DatabaseIdentityError, StateDatabase
from .media import MediaProcessor
from .models import DailyStats, GroupRunResult, RunSummary
from .reporting import BotReporter
from .service import AutomationService
from .telegram import ChannelGroupUnavailable, TelegramGateway

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
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.telegram = telegram
        self.media = media
        self.reporter = reporter
        self.clock = clock
        self._report_retry_not_before = 0.0
        self._fallback_report_cursor: date | None = None

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
        for group in groups:
            if continuous and group.name in paused:
                reason = paused[group.name]
                database = None
                try:
                    database = self._database(group)
                    database.record_daily_stats(
                        datetime.now(self.config.timezone).date().isoformat(),
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
                run_date = datetime.now(self.config.timezone).date().isoformat()
                published = database.published_count(str(group.source_channel), run_date)
                gateway = self.telegram.for_group(group)
                await gateway.doctor()
                service = AutomationService(
                    self.config,
                    group,
                    database,
                    gateway,
                    self.media,
                    self.reporter,
                )
                summary = await service.run_once(
                    continuous=continuous,
                    safe_point=safe_point,
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
                            datetime.now(self.config.timezone).date().isoformat()
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
                                datetime.now(self.config.timezone).date().isoformat(),
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
        elapsed_seconds = max(0.0, self.clock() - started_at)
        run_date = datetime.now(self.config.timezone).date().isoformat()
        LOGGER.info("所有频道组处理完成，总耗时 %s", format_duration(elapsed_seconds))
        if send_summary:
            await self.reporter.send(
                self.format_summary(
                    results,
                    run_date=run_date,
                    elapsed_seconds=elapsed_seconds,
                )
            )
        return results

    def _latest_due_report_date(self, now: datetime) -> date:
        hour, minute = (int(part) for part in self.config.daily_time.split(":"))
        scheduled_today = now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        days_back = 1 if now >= scheduled_today else 2
        return now.date() - timedelta(days=days_back)

    async def send_due_continuous_reports(
        self,
        groups: tuple[ChannelGroupConfig, ...],
        *,
        now: datetime | None = None,
        paused_groups: dict[str, str] | None = None,
    ) -> int:
        if self.clock() < self._report_retry_not_before:
            return 0
        current = now or datetime.now(self.config.timezone)
        latest_due = self._latest_due_report_date(current)
        databases: list[tuple[ChannelGroupConfig, StateDatabase | None]] = []
        paused = paused_groups or {}
        report_errors: dict[str, str] = {}
        try:
            cursors: list[date] = []
            for group in groups:
                try:
                    database = self._database(group)
                    cursor = database.continuous_report_cursor(
                        latest_due.isoformat()
                    )
                    cursors.append(date.fromisoformat(cursor))
                except Exception as exc:
                    database = None
                    report_errors[group.name] = f"{type(exc).__name__}: {exc}"
                    LOGGER.debug(
                        "读取频道组 %s 日报状态失败", group.display_name
                    )
                databases.append((group, database))
            if self._fallback_report_cursor is None:
                self._fallback_report_cursor = min(cursors, default=latest_due)
            cursors.append(self._fallback_report_cursor)
            report_date = min(cursors) + timedelta(days=1)
            sent = 0
            while report_date <= latest_due:
                rows = [
                    (
                        group,
                        (
                            database.daily_stats(report_date.isoformat())
                            if database is not None
                            else DailyStats(
                                stats_date=report_date.isoformat(),
                                paused_reason=(
                                    paused.get(group.name)
                                    or report_errors.get(group.name)
                                ),
                            )
                        ),
                    )
                    for group, database in databases
                ]
                rows = [
                    (
                        group,
                        (
                            stats
                            if stats.paused_reason or group.name not in paused
                            else DailyStats(
                                stats_date=stats.stats_date,
                                published=stats.published,
                                attempted=stats.attempted,
                                rejected=stats.rejected,
                                retryable_failures=stats.retryable_failures,
                                reconciled=stats.reconciled,
                                paused_reason=paused[group.name],
                            )
                        ),
                    )
                    for group, stats in rows
                ]
                if any(stats.has_activity for _, stats in rows):
                    delivered = await self.reporter.send(
                        self.format_continuous_daily_summary(report_date, rows)
                    )
                    if not delivered:
                        self._report_retry_not_before = (
                            self.clock()
                            + max(300, self.config.continuous_idle_seconds)
                        )
                        break
                    sent += 1
                for _, database in databases:
                    if database is not None:
                        database.set_continuous_report_cursor(
                            report_date.isoformat()
                        )
                self._fallback_report_cursor = report_date
                report_date += timedelta(days=1)
            return sent
        finally:
            for _, database in databases:
                if database is not None:
                    database.close()

    @staticmethod
    def format_continuous_daily_summary(
        report_date: date,
        rows: list[tuple[ChannelGroupConfig, DailyStats]],
    ) -> str:
        blocks = [f"Telegram 多频道连续运营日报 {report_date.isoformat()}"]
        for group, stats in rows:
            remark_line = f"备注：{group.remark}\n" if group.remark else ""
            pause_line = (
                f"\n暂停原因：{stats.paused_reason}" if stats.paused_reason else ""
            )
            blocks.append(
                f"[{group.name}]\n"
                f"{remark_line}"
                f"成功：{stats.published}\n"
                f"尝试：{stats.attempted}\n"
                f"永久跳过：{stats.rejected}\n"
                f"可重试失败：{stats.retryable_failures}\n"
                f"恢复确认：{stats.reconciled}"
                f"{pause_line}"
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
        for result in results:
            group = result.group
            remark_line = f"备注：{group.remark}\n" if group.remark else ""
            if result.summary is None:
                blocks.append(
                    f"[{group.name}] 已跳过\n"
                    f"{remark_line}"
                    f"成功：{result.published}/{group.daily_success_count}\n"
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
        return "\n\n".join(blocks)
