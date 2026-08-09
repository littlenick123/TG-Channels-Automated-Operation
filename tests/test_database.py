from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from channel_operator.database import DatabaseIdentityError, StateDatabase
from channel_operator.models import MessageSnapshot


def snapshot(
    message_id: int,
    grouped_id: int,
    *,
    video: bool = False,
    photo: bool = False,
    caption: str = "",
    width: int | None = None,
    height: int | None = None,
) -> MessageSnapshot:
    return MessageSnapshot(
        message_id=message_id,
        grouped_id=grouped_id,
        caption=caption,
        is_video=video,
        is_photo=photo,
        width=width,
        height=height,
        duration=180 if video else None,
        file_size=1000 if video else None,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )


def test_index_select_publish_and_never_reuse(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    database.save_messages(
        "source",
        [
            snapshot(1, 10, video=True, width=1920, height=1080, caption="#一"),
            snapshot(2, 10, photo=True),
            snapshot(3, 20, video=True, width=1280, height=720),
        ],
        3,
    )
    assert database.refresh_groups("source") == 2

    group = database.next_candidate("source", "2026-08-02", 1080, 0, set())
    assert group is not None
    assert group.grouped_id == 10
    assert group.message_ids == (1, 2)

    database.begin_attempt("source", 10, "2026-08-02")
    database.mark_published("source", 10, [101, 102, 103, 104], 999, "2026-08-02")

    assert database.published_count("source", "2026-08-02") == 1
    assert database.next_candidate("source", "2026-08-02", 1080, 0, set()) is None
    database.close()


def test_incremental_album_messages_are_merged(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    database.save_messages("source", [snapshot(1, 10, video=True, width=1920, height=1080)], 1)
    database.refresh_groups("source")
    database.save_messages("source", [snapshot(2, 10, photo=True)], 2)
    database.refresh_groups("source")

    group = database.next_candidate("source", "2026-08-02", 1080, 0, set())

    assert group is not None
    assert group.message_ids == (1, 2)
    assert database.checkpoint("source") == 2
    database.close()


def test_multiple_video_album_is_not_eligible(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    database.save_messages(
        "source",
        [
            snapshot(1, 10, video=True, width=1920, height=1080),
            snapshot(2, 10, video=True, width=1920, height=1080),
        ],
        2,
    )
    database.refresh_groups("source")

    assert database.next_candidate("source", "2026-08-02", 1080, 0, set()) is None
    database.close()


def test_database_identity_prevents_cross_channel_reuse(tmp_path):
    path = tmp_path / "channel_b.db"
    database = StateDatabase(
        path,
        group_name="channel_b",
        source_channel=-1001,
        target_channel=-1002,
    )
    database.close()

    with pytest.raises(DatabaseIdentityError, match="属于其他频道组"):
        StateDatabase(
            path,
            group_name="channel_c",
            source_channel=-1001,
            target_channel=-1003,
        )
