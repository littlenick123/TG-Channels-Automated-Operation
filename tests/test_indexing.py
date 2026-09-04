from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.config import ChannelGroupConfig
from channel_operator.database import StateDatabase
from channel_operator.indexing import (
    SourceIndexCoordinator,
    canonical_source_key,
    source_index_path,
)
from channel_operator.models import MessageSnapshot


def snapshot(message_id: int, grouped_id: int) -> MessageSnapshot:
    return MessageSnapshot(
        message_id=message_id,
        grouped_id=grouped_id,
        caption="标签：#共享\n简介：共享源简介",
        is_video=True,
        is_photo=False,
        width=1920,
        height=1080,
        duration=180,
        file_size=1000,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )


class CountingGateway:
    def __init__(self, snapshots=(), error: Exception | None = None):
        self.snapshots = tuple(snapshots)
        self.error = error
        self.checkpoints: list[int] = []

    async def scan_messages(self, min_id):
        self.checkpoints.append(min_id)
        if self.error is not None:
            raise self.error
        for message in self.snapshots:
            if message.message_id > min_id:
                yield message


def shared_source_config(app_config):
    config = app_config(daily_success_count=1)
    first = replace(config.channel_groups[0], name="channel_b")
    second = ChannelGroupConfig(
        name="channel_c",
        source_channel=first.source_channel,
        target_channel=-100333,
        database_path=first.database_path.with_name("channel_c.db"),
        daily_success_count=1,
    )
    return replace(config, channel_groups=(first, second))


@pytest.mark.asyncio
async def test_same_source_is_scanned_once_and_group_state_stays_independent(app_config):
    config = shared_source_config(app_config)
    gateway = CountingGateway((snapshot(1, 100),))
    coordinator = SourceIndexCoordinator(config)
    first_db = StateDatabase(
        config.channel_groups[0].database_path,
        group_name="channel_b",
        source_channel=config.channel_groups[0].source_channel,
        target_channel=config.channel_groups[0].target_channel,
    )
    second_db = StateDatabase(
        config.channel_groups[1].database_path,
        group_name="channel_c",
        source_channel=config.channel_groups[1].source_channel,
        target_channel=config.channel_groups[1].target_channel,
    )
    try:
        await coordinator.prepare_group(config.channel_groups[0], first_db, gateway)
        await coordinator.prepare_group(config.channel_groups[1], second_db, gateway)

        assert gateway.checkpoints == [0]
        assert first_db.media_group_count(str(config.channel_groups[0].source_channel)) == 1
        assert second_db.media_group_count(str(config.channel_groups[1].source_channel)) == 1

        first_db.begin_attempt(str(config.channel_groups[0].source_channel), 100, "2026-09-04")
        first_db.mark_published(
            str(config.channel_groups[0].source_channel),
            100,
            [11, 12, 13, 14],
            500,
            "2026-09-04",
        )
        second_candidate = second_db.next_candidate(
            str(config.channel_groups[1].source_channel),
            "2026-09-04",
            1080,
            0,
            set(),
        )
        assert second_candidate is not None
        assert second_candidate.status == "indexed"
    finally:
        first_db.close()
        second_db.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_new_group_reuses_shared_history_without_full_rescan(app_config):
    config = shared_source_config(app_config)
    first_group, second_group = config.channel_groups
    first_gateway = CountingGateway((snapshot(7, 700),))
    first_coordinator = SourceIndexCoordinator(config)
    first_db = StateDatabase(
        first_group.database_path,
        group_name=first_group.name,
        source_channel=first_group.source_channel,
        target_channel=first_group.target_channel,
    )
    try:
        await first_coordinator.prepare_group(first_group, first_db, first_gateway)
    finally:
        first_db.close()
        first_coordinator.close()

    second_gateway = CountingGateway()
    second_coordinator = SourceIndexCoordinator(config)
    second_db = StateDatabase(
        second_group.database_path,
        group_name=second_group.name,
        source_channel=second_group.source_channel,
        target_channel=second_group.target_channel,
    )
    try:
        await second_coordinator.prepare_group(second_group, second_db, second_gateway)

        assert second_gateway.checkpoints == [7]
        assert second_db.media_group_count(str(second_group.source_channel)) == 1
    finally:
        second_db.close()
        second_coordinator.close()


@pytest.mark.asyncio
async def test_shared_index_bootstraps_from_existing_group_database(app_config):
    config = shared_source_config(app_config)
    first_group, second_group = config.channel_groups
    first_db = StateDatabase(
        first_group.database_path,
        group_name=first_group.name,
        source_channel=first_group.source_channel,
        target_channel=first_group.target_channel,
    )
    first_db.save_messages(str(first_group.source_channel), [snapshot(9, 900)], 9)
    first_db.refresh_groups(str(first_group.source_channel))
    first_db.close()

    assert not source_index_path(config.database_dir, first_group.source_channel).exists()
    gateway = CountingGateway()
    coordinator = SourceIndexCoordinator(config)
    second_db = StateDatabase(
        second_group.database_path,
        group_name=second_group.name,
        source_channel=second_group.source_channel,
        target_channel=second_group.target_channel,
    )
    try:
        await coordinator.prepare_group(second_group, second_db, gateway)

        assert gateway.checkpoints == [9]
        assert second_db.media_group_count(str(second_group.source_channel)) == 1
    finally:
        second_db.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_source_scan_failure_is_cached_but_other_source_can_continue(app_config):
    config = shared_source_config(app_config)
    first_group, second_group = config.channel_groups
    third_group = ChannelGroupConfig(
        name="channel_d",
        source_channel=-100444,
        target_channel=-100555,
        database_path=config.database_dir / "channel_d.db",
        daily_success_count=1,
    )
    config = replace(config, channel_groups=(*config.channel_groups, third_group))
    failing = CountingGateway(error=ConnectionError("源频道临时断开"))
    healthy = CountingGateway((snapshot(2, 200),))
    coordinator = SourceIndexCoordinator(config)

    async def prepare(group, gateway):
        database = StateDatabase(
            group.database_path,
            group_name=group.name,
            source_channel=group.source_channel,
            target_channel=group.target_channel,
        )
        try:
            return await coordinator.prepare_group(group, database, gateway)
        finally:
            database.close()

    try:
        with pytest.raises(ConnectionError, match="临时断开"):
            await prepare(first_group, failing)
        with pytest.raises(ConnectionError, match="临时断开"):
            await prepare(second_group, failing)
        assert failing.checkpoints == [0]

        assert await prepare(third_group, healthy) == 1
        assert healthy.checkpoints == [0]
        assert canonical_source_key(third_group.source_channel) != canonical_source_key(
            first_group.source_channel
        )
    finally:
        coordinator.close()
