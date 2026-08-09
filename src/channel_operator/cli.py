from __future__ import annotations

import argparse
import asyncio
import logging

from .config import AppConfig, ChannelGroupConfig, ConfigError, load_config
from .database import StateDatabase
from .locking import AlreadyRunningError, ProcessLock
from .media import MediaProcessor
from .reporting import BotReporter, ReporterError
from .runner import MultiChannelRunner
from .scheduler import run_scheduler
from .service import AutomationService
from .telegram import TelegramError, TelegramGateway


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
    run = commands.add_parser("run-once", help="执行一次每日任务")
    run.add_argument("--dry-run", action="store_true", help="仅预览选材和文案")
    run.add_argument("--group", help="只运行指定频道组")
    commands.add_parser("schedule", help="常驻运行并按配置时间每天执行")
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
    await reporter.send(
        "⚠️ 频道组操作失败，已继续后续组\n"
        f"操作：{operation}\n"
        f"组名：{group.name}\n"
        f"源频道：{group.source_channel}\n"
        f"目标频道：{group.target_channel}\n"
        f"错误：{type(exc).__name__}: {exc}"
    )


async def _run(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)

    if arguments.command == "schedule":
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

    if arguments.command == "login":
        try:
            await telegram.login()
        finally:
            await telegram.disconnect()
        return 0

    groups = _select_groups(config, getattr(arguments, "group", None))
    await telegram.connect()
    reporter = BotReporter(config.reporting)
    try:
        if arguments.command == "doctor":
            config.work_dir.mkdir(parents=True, exist_ok=True)
            ffmpeg = await media.version(config.ffmpeg_path)
            ffprobe = await media.version(config.ffprobe_path)
            print(f"FFmpeg: {ffmpeg}")
            print(f"FFprobe: {ffprobe}")
            print(f"work_dir: {config.work_dir}")
            print(f"download_concurrency: {config.download_concurrency}")
            bot_username = await reporter.doctor()
            print(f"report_bot: @{bot_username}")
            failed = False
            for group in groups:
                database: StateDatabase | None = None
                try:
                    database = _database(group)
                    checks = await telegram.for_group(group).doctor()
                    print(f"\n[{group.name}]")
                    for name, value in checks.items():
                        print(f"{name}: {value}")
                    print(f"database: {group.database_path}")
                except Exception as exc:
                    failed = True
                    logging.exception("频道组 %s 检查失败", group.name)
                    print(f"\n[{group.name}] ERROR: {type(exc).__name__}: {exc}")
                    await _report_group_error(reporter, group, "doctor", exc)
                finally:
                    if database is not None:
                        database.close()
            return 2 if failed else 0

        lock_path = config.work_dir / ".channel-operator.lock"
        with ProcessLock(lock_path):
            if arguments.command == "index":
                failed = False
                for group in groups:
                    database: StateDatabase | None = None
                    try:
                        database = _database(group)
                        service = AutomationService(
                            config,
                            group,
                            database,
                            telegram.for_group(group),
                            media,
                            reporter,
                        )
                        count = await service.index()
                        print(f"[{group.name}] 索引完成，共 {count} 个媒体组")
                    except Exception as exc:
                        failed = True
                        logging.exception("频道组 %s 索引失败", group.name)
                        await _report_group_error(reporter, group, "index", exc)
                    finally:
                        if database is not None:
                            database.close()
                return 2 if failed else 0
            if arguments.dry_run:
                failed = False
                for group in groups:
                    database: StateDatabase | None = None
                    try:
                        database = _database(group)
                        service = AutomationService(
                            config,
                            group,
                            database,
                            telegram.for_group(group),
                            media,
                            reporter,
                        )
                        previews = await service.dry_run()
                        print(f"\n[{group.name}]")
                        if not previews:
                            print("没有符合条件的未处理媒体组")
                        for grouped_id, caption in previews:
                            print(f"\n媒体组 {grouped_id}\n{caption or '[空文案]'}")
                    except Exception as exc:
                        failed = True
                        logging.exception("频道组 %s 预览失败", group.name)
                        await _report_group_error(reporter, group, "dry-run", exc)
                    finally:
                        if database is not None:
                            database.close()
                return 2 if failed else 0
            runner = MultiChannelRunner(config, telegram, media, reporter)
            results = await runner.run_once(groups)
            for result in results:
                status = "已跳过" if result.skipped_reason else "完成"
                print(
                    f"[{result.group.name}] {status}：成功 {result.published}/"
                    f"{result.group.daily_success_count}"
                )
            return 0 if all(result.succeeded for result in results) else 2
    finally:
        await reporter.close()
        await telegram.disconnect()


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The Bot API token is part of the request URL. HTTPX's INFO/DEBUG request
    # logging would expose it, so all reporter failures are logged by our
    # sanitized wrapper instead.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
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
