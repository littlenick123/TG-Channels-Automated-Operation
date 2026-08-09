from __future__ import annotations

import logging
from datetime import datetime

from .config import AppConfig, ChannelGroupConfig
from .database import StateDatabase
from .media import MediaProcessor
from .models import GroupRunResult, RunSummary
from .reporting import BotReporter
from .service import AutomationService
from .telegram import TelegramGateway

LOGGER = logging.getLogger(__name__)


class MultiChannelRunner:
    def __init__(
        self,
        config: AppConfig,
        telegram: TelegramGateway,
        media: MediaProcessor,
        reporter: BotReporter,
    ):
        self.config = config
        self.telegram = telegram
        self.media = media
        self.reporter = reporter

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
        await self.reporter.send(
            "⚠️ 频道组已跳过\n"
            f"组名：{group.name}\n"
            f"源频道：{group.source_channel}\n"
            f"目标频道：{group.target_channel}\n"
            f"错误：{reason}\n"
            f"已完成：{published}/{group.daily_success_count}\n"
            f"剩余目标：{remaining}"
        )

    async def run_once(
        self, groups: tuple[ChannelGroupConfig, ...]
    ) -> list[GroupRunResult]:
        results: list[GroupRunResult] = []
        for group in groups:
            LOGGER.info("开始处理频道组 %s", group.name)
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
                summary = await service.run_once()
                results.append(GroupRunResult(group=group, summary=summary))
                LOGGER.info(
                    "频道组 %s 完成：成功 %s/%s",
                    group.name,
                    summary.published,
                    group.daily_success_count,
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("频道组 %s 失败，已跳过", group.name)
                if database is not None:
                    try:
                        run_date = datetime.now(self.config.timezone).date().isoformat()
                        published = database.published_count(
                            str(group.source_channel), run_date
                        )
                    except Exception:
                        LOGGER.exception("读取频道组 %s 的完成数量失败", group.name)
                results.append(
                    GroupRunResult(
                        group=group,
                        skipped_reason=reason,
                        published_before_skip=published,
                    )
                )
                await self._report_skipped(group, reason, published)
            finally:
                if database is not None:
                    database.close()
        run_date = datetime.now(self.config.timezone).date().isoformat()
        await self.reporter.send(self.format_summary(results, run_date=run_date))
        return results

    @staticmethod
    def format_summary(
        results: list[GroupRunResult], *, run_date: str | None = None
    ) -> str:
        resolved_date = run_date or next(
            (
                result.summary.run_date
                for result in results
                if result.summary is not None
            ),
            "unknown",
        )
        blocks = [f"Telegram 多频道自动运营任务 {resolved_date}"]
        for result in results:
            group = result.group
            if result.summary is None:
                blocks.append(
                    f"[{group.name}] 已跳过\n"
                    f"成功：{result.published}/{group.daily_success_count}\n"
                    f"原因：{result.skipped_reason or '未知错误'}"
                )
                continue
            summary: RunSummary = result.summary
            blocks.append(
                f"[{group.name}] {'完成' if result.succeeded else '未达目标'}\n"
                f"成功：{summary.published}/{group.daily_success_count}\n"
                f"本次尝试：{summary.attempted}\n"
                f"永久跳过：{summary.rejected}\n"
                f"可重试失败：{summary.retryable_failures}\n"
                f"恢复确认：{summary.reconciled}"
            )
        return "\n\n".join(blocks)
