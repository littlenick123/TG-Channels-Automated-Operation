from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from .config import AppConfig

LOGGER = logging.getLogger(__name__)


def next_run_at(
    now: datetime,
    daily_time: str,
    *,
    catch_up_today: bool,
) -> datetime:
    """Return the next daily execution time in ``now``'s timezone."""
    hour, minute = (int(part) for part in daily_time.split(":"))
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled_today:
        return scheduled_today
    if catch_up_today:
        return now
    return scheduled_today + timedelta(days=1)


async def run_scheduler(
    config: AppConfig,
    run_job: Callable[[], Awaitable[int]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] | None = None,
    max_runs: int | None = None,
) -> int:
    """Run the daily job forever, with one startup catch-up when already due."""
    now_factory = now or (lambda: datetime.now(config.timezone))
    catch_up_today = True
    completed_runs = 0

    while max_runs is None or completed_runs < max_runs:
        current = now_factory()
        target = next_run_at(
            current,
            config.daily_time,
            catch_up_today=catch_up_today,
        )
        delay = max(0.0, (target - current).total_seconds())
        if delay:
            LOGGER.info(
                "下一次任务：%s（%s，等待 %.0f 秒）",
                target.isoformat(timespec="seconds"),
                config.timezone.key,
                delay,
            )
            await sleep(delay)
        else:
            LOGGER.info("今日计划时间已到，立即执行一次补跑")

        try:
            code = await run_job()
        except Exception:
            LOGGER.exception("本次定时任务发生未处理异常；调度器将在下次继续运行")
            code = 1
        completed_runs += 1
        catch_up_today = False
        if code == 0:
            LOGGER.info("本次定时任务完成")
        else:
            LOGGER.warning("本次定时任务退出码为 %s；调度器将在下次继续运行", code)

    return 0


async def run_continuous_scheduler(
    config: AppConfig,
    run_cycle: Callable[[], Awaitable[int]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_cycles: int | None = None,
) -> int:
    """Run channel groups in strict cycles until the process is stopped."""
    completed_cycles = 0

    while max_cycles is None or completed_cycles < max_cycles:
        try:
            published = await run_cycle()
        except Exception:
            LOGGER.exception("本轮循环发生未处理异常；等待后继续下一轮")
            published = 0
        completed_cycles += 1
        if published > 0:
            LOGGER.info("本轮成功发布 %s 组，立即开始下一轮", published)
            continue
        if max_cycles is not None and completed_cycles >= max_cycles:
            break
        LOGGER.info(
            "本轮没有成功发布，等待 %s 秒后开始下一轮",
            config.continuous_idle_seconds,
        )
        await sleep(config.continuous_idle_seconds)
    return 0
