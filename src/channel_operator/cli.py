from __future__ import annotations

import argparse
import asyncio
import logging

from .config import ConfigError, load_config
from .database import StateDatabase
from .locking import AlreadyRunningError, ProcessLock
from .media import MediaProcessor
from .service import AutomationService
from .telegram import TelegramError, TelegramGateway


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram 频道自动运营")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="交互登录 Telegram 用户账号")
    commands.add_parser("doctor", help="检查配置、依赖和频道权限")
    commands.add_parser("index", help="建立或更新源频道索引")
    run = commands.add_parser("run-once", help="执行一次每日任务")
    run.add_argument("--dry-run", action="store_true", help="仅预览选材和文案")
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    media = MediaProcessor(config)
    telegram = TelegramGateway(config)

    if arguments.command == "login":
        try:
            await telegram.login()
        finally:
            await telegram.disconnect()
        return 0

    await telegram.connect()
    database: StateDatabase | None = None
    try:
        if arguments.command == "doctor":
            config.database_path.parent.mkdir(parents=True, exist_ok=True)
            config.work_dir.mkdir(parents=True, exist_ok=True)
            database = StateDatabase(config.database_path)
            ffmpeg = await media.version(config.ffmpeg_path)
            ffprobe = await media.version(config.ffprobe_path)
            checks = await telegram.doctor()
            print(f"FFmpeg: {ffmpeg}")
            print(f"FFprobe: {ffprobe}")
            for name, value in checks.items():
                print(f"{name}: {value}")
            print(f"database: {config.database_path}")
            print(f"work_dir: {config.work_dir}")
            print(f"download_concurrency: {config.download_concurrency}")
            return 0

        lock_path = config.database_path.with_suffix(config.database_path.suffix + ".lock")
        with ProcessLock(lock_path):
            database = StateDatabase(config.database_path)
            service = AutomationService(config, database, telegram, media)
            if arguments.command == "index":
                count = await service.index()
                print(f"索引完成，共 {count} 个媒体组")
                return 0
            if arguments.dry_run:
                previews = await service.dry_run()
                if not previews:
                    print("没有符合条件的未处理媒体组")
                for grouped_id, caption in previews:
                    print(f"\n媒体组 {grouped_id}\n{caption or '[空文案]'}")
                return 0
            summary = await service.run_once()
            print(
                f"任务结束：{summary.run_date} 成功 {summary.published}/"
                f"{config.daily_success_count}，本次尝试 {summary.attempted}"
            )
            return 0 if summary.published >= config.daily_success_count else 2
    finally:
        if database is not None:
            database.close()
        await telegram.disconnect()


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        code = asyncio.run(_run(arguments))
    except (ConfigError, TelegramError, AlreadyRunningError) as exc:
        logging.error("%s", exc)
        code = 1
    except KeyboardInterrupt:
        logging.warning("任务已由用户中断")
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
