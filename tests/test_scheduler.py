from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from channel_operator.scheduler import next_run_at, run_scheduler

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
