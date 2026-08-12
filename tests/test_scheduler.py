from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from channel_operator.scheduler import (
    next_run_at,
    run_continuous_scheduler,
    run_scheduler,
)

TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_next_run_waits_until_today_when_started_before_schedule():
    now = datetime(2026, 8, 9, 0, 0, tzinfo=TIMEZONE)

    result = next_run_at(now, "00:01", catch_up_today=True)

    assert result == datetime(2026, 8, 9, 0, 1, tzinfo=TIMEZONE)


def test_next_run_catches_up_immediately_after_schedule_on_startup():
    now = datetime(2026, 8, 9, 8, 30, tzinfo=TIMEZONE)

    result = next_run_at(now, "00:01", catch_up_today=True)

    assert result == now


def test_next_run_moves_to_tomorrow_after_first_run():
    now = datetime(2026, 8, 9, 8, 30, tzinfo=TIMEZONE)

    result = next_run_at(now, "00:01", catch_up_today=False)

    assert result == datetime(2026, 8, 10, 0, 1, tzinfo=TIMEZONE)


@pytest.mark.asyncio
async def test_scheduler_waits_and_invokes_job_once(app_config):
    config = app_config(daily_time="09:00")
    delays: list[float] = []
    calls: list[str] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fake_job() -> int:
        calls.append("run")
        return 2

    code = await run_scheduler(
        config,
        fake_job,
        sleep=fake_sleep,
        now=lambda: datetime(2026, 8, 9, 8, 0, tzinfo=TIMEZONE),
        max_runs=1,
    )

    assert code == 0
    assert delays == [3600]
    assert calls == ["run"]


@pytest.mark.asyncio
async def test_scheduler_survives_job_exception(app_config):
    config = app_config(daily_time="00:01")

    async def failing_job() -> int:
        raise RuntimeError("temporary failure")

    code = await run_scheduler(
        config,
        failing_job,
        now=lambda: datetime(2026, 8, 9, 8, 0, tzinfo=TIMEZONE),
        max_runs=1,
    )

    assert code == 0


@pytest.mark.asyncio
async def test_continuous_scheduler_starts_immediately_and_only_idles_empty_cycle(
    app_config,
):
    config = app_config(schedule_mode="continuous", continuous_idle_seconds=300)
    published = iter((2, 0, 1))
    cycles = []
    reports = []
    delays = []

    async def run_cycle():
        value = next(published)
        cycles.append(value)
        return value

    async def report_due(now):
        reports.append(now)

    async def sleep(delay):
        delays.append(delay)

    code = await run_continuous_scheduler(
        config,
        run_cycle,
        report_due,
        sleep=sleep,
        now=lambda: datetime(2026, 8, 12, 8, 0, tzinfo=TIMEZONE),
        max_cycles=3,
    )

    assert code == 0
    assert cycles == [2, 0, 1]
    assert delays == [300]
    assert len(reports) == 6


@pytest.mark.asyncio
async def test_continuous_scheduler_survives_cycle_exception_and_idles(app_config):
    config = app_config(schedule_mode="continuous", continuous_idle_seconds=60)
    delays = []

    async def failing_cycle():
        raise RuntimeError("temporary")

    async def report_due(now):
        return None

    async def sleep(delay):
        delays.append(delay)

    code = await run_continuous_scheduler(
        config,
        failing_cycle,
        report_due,
        sleep=sleep,
        now=lambda: datetime(2026, 8, 12, 8, 0, tzinfo=TIMEZONE),
        max_cycles=2,
    )

    assert code == 0
    assert delays == [60]
