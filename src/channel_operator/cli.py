from __future__ import annotations

import argparse
import asyncio
import logging

from .config import AppConfig, ChannelGroupConfig, ConfigError, load_config
from .database import StateDatabase
from .indexing import SourceIndexCoordinator, canonical_source_key
from .locking import AlreadyRunningError, ProcessLock
from .media import MediaProcessor
from .reporting import BotReporter, ReporterError
from .runner import MultiChannelRunner
from .scheduler import run_continuous_scheduler, run_scheduler
from .service import AutomationService
from .telegram import BotDeliveryGateway, TelegramError, TelegramGateway


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon.client.users").setLevel(
        logging.NOTSET if verbose else logging.WARNING
    )
    # The Bot API token is part of the request URL. HTTPX's INFO/DEBUG request
    # logging would expose it, so all reporter failures are logged by our
    # sanitized wrapper instead.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram 频道自动运营")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="交互登录 Telegram 用户账号")
    doctor = commands.add_parser("doctor", help="检查配置、依赖和频道权限")
    doctor.add_argument("--group", help="只检查指定频道组")
    index = commands.add_parser("index", help="建立或更新源频道索引")
    index.add_argument("--group", help="只索引指定频道组")
    run = commands.add_parser("run-once", help="执行一次每日任务或一轮循环")
    run.add_argument("--dry-run", action="store_true", help="仅预览选材和文案")
    run.add_argument("--group", help="只运行指定频道组")
    commands.add_parser("schedule", help="按配置常驻执行每日或循环任务")
    return parser


def _select_groups(
    config: AppConfig, selected_name: str | None
) -> tuple[ChannelGroupConfig, ...]:
    if selected_name is None:
        return config.channel_groups
    selected = tuple(
        group for group in config.channel_groups if group.name == selected_name
    )
    if not selected:
        available = ", ".join(group.name for group in config.channel_groups)
        raise ConfigError(f"未知频道组 {selected_name!r}；可用频道组：{available}")
    return selected


def _database(group: ChannelGroupConfig) -> StateDatabase:
    return StateDatabase(
        group.database_path,
        group_name=group.name,
        source_channel=group.source_channel,
        target_channel=group.target_channel,
    )


async def _report_group_error(
    reporter: BotReporter, group: ChannelGroupConfig, operation: str, exc: Exception
) -> None:
    remark_line = f"备注：{group.remark}\n" if group.remark else ""
    await reporter.send(
        "⚠️ 频道组操作失败，已继续后续组\n"
        f"操作：{operation}\n"
        f"组名：{group.name}\n"
        f"{remark_line}"
        f"源频道：{group.source_channel}\n"
        f"目标频道：{group.target_channel}\n"
        f"错误：{type(exc).__name__}: {exc}"
    )


async def _run_continuous(config: AppConfig) -> int:
    config.work_dir.mkdir(parents=True, exist_ok=True)
    media = MediaProcessor(config)
    telegram = TelegramGateway(config)
    delivery = BotDeliveryGateway(config)
    reporter = BotReporter(config.reporting)
    paused_groups: dict[str, str] = {}
    try:
        await telegram.connect()
        await delivery.connect()
        runner = MultiChannelRunner(
            config, telegram, media, reporter, delivery=delivery
        )

        async def run_cycle() -> int:
            results = await runner.run_once(
                config.channel_groups,
                continuous=True,
                send_summary=True,
                paused_groups=paused_groups,
            )
            return sum(
                result.summary.published
                for result in results
                if result.summary is not None
            )

        lock_path = config.work_dir / ".channel-operator.lock"
        with ProcessLock(lock_path):
            return await run_continuous_scheduler(
                config,
                run_cycle,
            )
    finally:
        await reporter.close()
        await delivery.disconnect()
        await telegram.disconnect()


async def _run(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)

    if arguments.command == "schedule":
        if config.schedule_mode == "continuous":
            return await _run_continuous(config)

        async def run_job() -> int:
            daily_arguments = argparse.Namespace(
                config=arguments.config,
                verbose=arguments.verbose,
                command="run-once",
                dry_run=False,
                group=None,
            )
            return await _run(daily_arguments)

        return await run_scheduler(config, run_job)

    media = MediaProcessor(config)
    telegram = TelegramGateway(config)
    delivery = BotDeliveryGateway(config)

    if arguments.command == "login":
        try:
            await telegram.login()
            await delivery.login()
        finally:
            await delivery.disconnect()
            await telegram.disconnect()
        return 0

    groups = _select_groups(config, getattr(arguments, "group", None))
    reporter = BotReporter(config.reporting)
    try:
        await telegram.connect()
        if arguments.command in {"doctor", "run-once"}:
            await delivery.connect()
        if arguments.command == "doctor":
            config.work_dir.mkdir(parents=True, exist_ok=True)
            ffmpeg = await media.version(config.ffmpeg_path)
            ffprobe = await media.version(config.ffprobe_path)
            print(f"FFmpeg: {ffmpeg}")
            print(f"FFprobe: {ffprobe}")
            print(f"work_dir: {config.work_dir}")
            print(f"schedule_mode: {config.schedule_mode}")
            print(f"daily_time: {config.daily_time}")
            print(f"continuous_idle_seconds: {config.continuous_idle_seconds}")
            print(f"output_height: {config.output_height}")
            watermark_groups = [
                group
                for group in groups
                if (
                    config.watermark_text
                    if group.watermark_text is None
                    else group.watermark_text
                )
            ]
            if watermark_groups:
                await media.check_watermark_support()
                print(f"watermark_font_file: {config.watermark_font_file}")
                print(
                    "watermark_enabled_groups: "
                    + ", ".join(group.name for group in watermark_groups)
                )
            else:
                print("watermark_enabled_groups: none")
            print(f"download_concurrency: {config.download_concurrency}")
            print(
                "download_stall_timeout_seconds: "
                f"{config.download_stall_timeout_seconds:g}"
            )
            print(
                "download_low_speed_window_seconds: "
                f"{config.download_low_speed_window_seconds:g}"
            )
            print(
                "download_low_speed_limit_kib_per_second: "
                f"{config.download_low_speed_limit_kib_per_second:g}"
            )
            bot_username = await reporter.doctor()
            print(f"report_bot: @{bot_username}")
            print(f"report_server: {reporter.server_name}")
            print(f"staging_channel: {config.delivery.staging_channel}")
            failed = False
            checked_source_indexes: set[str] = set()
            source_indexes = SourceIndexCoordinator(config)
            for group in groups:
                database: StateDatabase | None = None
                try:
                    database = _database(group)
                    checks = await telegram.for_group(group).doctor()
                    checks.update(await delivery.for_group(group).doctor())
                    print(f"\n[{group.display_name}]")
                    for name, value in checks.items():
                        print(f"{name}: {value}")
                    print(f"database: {group.database_path}")
                    source_key = canonical_source_key(group.source_channel)
                    if source_key not in checked_source_indexes:
                        index_path, checkpoint, message_count = source_indexes.details(
                            group.source_channel
                        )
                        print(f"source_index: {index_path}")
                        print(f"source_index_checkpoint: {checkpoint}")
                        print(f"source_index_messages: {message_count}")
                        print("source_index_status: read-write-ok")
                        checked_source_indexes.add(source_key)
                except Exception as exc:
                    failed = True
                    logging.exception("频道组 %s 检查失败", group.display_name)
                    print(
                        f"\n[{group.display_name}] ERROR: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    await _report_group_error(reporter, group, "doctor", exc)
                finally:
                    if database is not None:
                        database.close()
            return 2 if failed else 0

        lock_path = config.work_dir / ".channel-operator.lock"
        with ProcessLock(lock_path):
            if arguments.command == "index":
                failed = False
                source_indexes = SourceIndexCoordinator(config)
                for group in groups:
                    database: StateDatabase | None = None
                    try:
                        database = _database(group)
                        gateway = telegram.for_group(group)
                        count = await source_indexes.prepare_group(
                            group,
                            database,
                            gateway,
                        )
                        print(
                            f"[{group.display_name}] 索引完成，共 {count} 个媒体组"
                        )
                    except Exception as exc:
                        failed = True
                        logging.exception("频道组 %s 索引失败", group.display_name)
                        await _report_group_error(reporter, group, "index", exc)
                    finally:
                        if database is not None:
                            database.close()
                source_indexes.close()
                return 2 if failed else 0
            if arguments.dry_run:
                failed = False
                source_indexes = SourceIndexCoordinator(config)
                for group in groups:
                    database: StateDatabase | None = None
                    try:
                        database = _database(group)
                        gateway = telegram.for_group(group)
                        await source_indexes.prepare_group(group, database, gateway)
                        service = AutomationService(
                            config,
                            group,
                            database,
                            gateway,
                            media,
                            reporter,
                            delivery=delivery.for_group(group),
                        )
                        previews = await service.dry_run(
                            continuous=config.schedule_mode == "continuous",
                            index_before_run=False,
                        )
                        print(f"\n[{group.display_name}]")
                        if not previews:
                            print("没有符合条件的未处理媒体组")
                        for grouped_id, caption in previews:
                            print(f"\n媒体组 {grouped_id}\n{caption or '[空文案]'}")
                    except Exception as exc:
                        failed = True
                        logging.exception("频道组 %s 预览失败", group.display_name)
                        await _report_group_error(reporter, group, "dry-run", exc)
                    finally:
                        if database is not None:
                            database.close()
                source_indexes.close()
                return 2 if failed else 0
            runner = MultiChannelRunner(
                config, telegram, media, reporter, delivery=delivery
            )
            continuous = config.schedule_mode == "continuous"
            results = await runner.run_once(
                groups,
                continuous=continuous,
                send_summary=True,
            )
            for result in results:
                status = "已跳过" if result.skipped_reason else "完成"
                print(
                    f"[{result.group.display_name}] {status}：成功 {result.published}/"
                    f"{result.group.daily_success_count}"
                )
            return 0 if all(result.succeeded for result in results) else 2
    finally:
        await reporter.close()
        await delivery.disconnect()
        await telegram.disconnect()


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    _configure_logging(arguments.verbose)
    try:
        code = asyncio.run(_run(arguments))
    except (ConfigError, TelegramError, ReporterError, AlreadyRunningError) as exc:
        logging.error("%s", exc)
        code = 1
    except KeyboardInterrupt:
        logging.warning("任务已由用户中断")
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
