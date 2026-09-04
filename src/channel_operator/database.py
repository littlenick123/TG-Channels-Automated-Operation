from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import DailyStats, DeliveryReceipt, MediaGroup, MessageSnapshot


class DatabaseIdentityError(RuntimeError):
    """Raised when a database belongs to a different channel group."""


class StateDatabase:
    def __init__(
        self,
        path: Path,
        *,
        group_name: str | None = None,
        source_channel: str | int | None = None,
        target_channel: str | int | None = None,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._migrate()
            identity = (group_name, source_channel, target_channel)
            if any(value is not None for value in identity):
                if any(value is None for value in identity):
                    raise ValueError("数据库身份必须同时提供频道组名称、源频道和目标频道")
                self._bind_identity(
                    str(group_name), str(source_channel), str(target_channel)
                )
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_messages (
                source_channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                is_video INTEGER NOT NULL,
                is_photo INTEGER NOT NULL,
                width INTEGER,
                height INTEGER,
                duration REAL,
                file_size INTEGER,
                published_at TEXT NOT NULL,
                PRIMARY KEY (source_channel, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_source_messages_group
                ON source_messages(source_channel, grouped_id, message_id);

            CREATE TABLE IF NOT EXISTS media_groups (
                source_channel TEXT NOT NULL,
                grouped_id TEXT NOT NULL,
                message_ids TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                video_count INTEGER NOT NULL,
                video_message_id INTEGER,
                caption TEXT NOT NULL DEFAULT '',
                width INTEGER,
                height INTEGER,
                duration REAL,
                file_size INTEGER,
                newest_message_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'indexed',
                selected_date TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                upload_started_at TEXT,
                attempt_caption_html TEXT,
                attempt_caption_plain TEXT,
                destination_message_ids TEXT,
                destination_grouped_id TEXT,
                published_date TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_channel, grouped_id)
            );

            CREATE INDEX IF NOT EXISTS idx_media_groups_status
                ON media_groups(source_channel, status, selected_date);

            CREATE TABLE IF NOT EXISTS daily_stats (
                stats_date TEXT PRIMARY KEY,
                published INTEGER NOT NULL DEFAULT 0,
                attempted INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                retryable_failures INTEGER NOT NULL DEFAULT 0,
                reconciled INTEGER NOT NULL DEFAULT 0,
                paused_reason TEXT
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(media_groups)")
        }
        additions = {
            "staging_channel": "TEXT",
            "staging_upload_started_at": "TEXT",
            "staging_message_ids": "TEXT",
            "staging_grouped_id": "TEXT",
            "staged_at": "TEXT",
            "delivery_started_at": "TEXT",
            "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "caption_applied": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE media_groups ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def _bind_identity(
        self, group_name: str, source_channel: str, target_channel: str
    ) -> None:
        expected = {
            "identity:group_name": group_name,
            "identity:source_channel": source_channel,
            "identity:target_channel": target_channel,
        }
        placeholders = ",".join("?" for _ in expected)
        rows = self.connection.execute(
            f"SELECT key, value FROM metadata WHERE key IN ({placeholders})",
            tuple(expected),
        ).fetchall()
        actual = {str(row["key"]): str(row["value"]) for row in rows}
        if actual and actual != expected:
            details = ", ".join(
                f"{key}={actual.get(key, '[缺失]')}（预期 {value}）"
                for key, value in expected.items()
                if actual.get(key) != value
            )
            raise DatabaseIdentityError(
                f"数据库 {self.path} 属于其他频道组：{details}"
            )
        if not actual:
            with self.transaction():
                self.connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    expected.items(),
                )

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def checkpoint(self, source_channel: str) -> int:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (f"checkpoint:{source_channel}",)
        ).fetchone()
        return int(row["value"]) if row else 0

    def save_messages(
        self, source_channel: str, messages: Iterable[MessageSnapshot], checkpoint: int
    ) -> None:
        values = [
            (
                source_channel,
                message.message_id,
                str(message.grouped_id),
                message.caption,
                int(message.is_video),
                int(message.is_photo),
                message.width,
                message.height,
                message.duration,
                message.file_size,
                message.published_at.astimezone(UTC).isoformat(),
            )
            for message in messages
            if message.grouped_id is not None
        ]
        with self.transaction():
            if values:
                self.connection.executemany(
                    """
                    INSERT INTO source_messages (
                        source_channel, message_id, grouped_id, caption, is_video, is_photo,
                        width, height, duration, file_size, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_channel, message_id) DO UPDATE SET
                        grouped_id=excluded.grouped_id,
                        caption=excluded.caption,
                        is_video=excluded.is_video,
                        is_photo=excluded.is_photo,
                        width=excluded.width,
                        height=excluded.height,
                        duration=excluded.duration,
                        file_size=excluded.file_size,
                        published_at=excluded.published_at
                    """,
                    values,
                )
            self.connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (f"checkpoint:{source_channel}", str(checkpoint)),
            )

    def refresh_groups(self, source_channel: str) -> int:
        rows = self.connection.execute(
            """
            SELECT * FROM source_messages
            WHERE source_channel = ?
            ORDER BY grouped_id, message_id
            """,
            (source_channel,),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[row["grouped_id"]].append(row)
        now = datetime.now(UTC).isoformat()
        with self.transaction():
            for grouped_id, items in grouped.items():
                videos = [item for item in items if item["is_video"]]
                video = videos[0] if len(videos) == 1 else None
                caption = next((item["caption"] for item in items if item["caption"]), "")
                self.connection.execute(
                    """
                    INSERT INTO media_groups (
                        source_channel, grouped_id, message_ids, item_count, video_count,
                        video_message_id, caption, width, height, duration, file_size,
                        newest_message_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_channel, grouped_id) DO UPDATE SET
                        message_ids=excluded.message_ids,
                        item_count=excluded.item_count,
                        video_count=excluded.video_count,
                        video_message_id=excluded.video_message_id,
                        caption=excluded.caption,
                        width=excluded.width,
                        height=excluded.height,
                        duration=excluded.duration,
                        file_size=excluded.file_size,
                        newest_message_at=excluded.newest_message_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source_channel,
                        grouped_id,
                        json.dumps([item["message_id"] for item in items]),
                        len(items),
                        len(videos),
                        video["message_id"] if video else None,
                        caption,
                        video["width"] if video else None,
                        video["height"] if video else None,
                        video["duration"] if video else None,
                        video["file_size"] if video else None,
                        max(item["published_at"] for item in items),
                        now,
                        now,
                    ),
                )
        return len(grouped)

    def _to_group(self, row: sqlite3.Row) -> MediaGroup:
        def message_ids(column: str) -> tuple[int, ...]:
            value = row[column]
            return tuple(int(item) for item in json.loads(value)) if value else ()

        return MediaGroup(
            source_channel=row["source_channel"],
            grouped_id=int(row["grouped_id"]),
            message_ids=tuple(json.loads(row["message_ids"])),
            video_message_id=int(row["video_message_id"]),
            caption=row["caption"],
            width=int(row["width"]),
            height=int(row["height"]),
            duration=float(row["duration"]) if row["duration"] is not None else None,
            file_size=int(row["file_size"] or 0),
            status=row["status"],
            selected_date=row["selected_date"],
            attempts=int(row["attempts"]),
            upload_started_at=row["upload_started_at"],
            attempt_caption_html=row["attempt_caption_html"],
            attempt_caption_plain=row["attempt_caption_plain"],
            staging_upload_started_at=row["staging_upload_started_at"],
            staging_message_ids=message_ids("staging_message_ids"),
            staging_grouped_id=(
                int(row["staging_grouped_id"])
                if row["staging_grouped_id"] is not None
                else None
            ),
            staged_at=row["staged_at"],
            delivery_started_at=row["delivery_started_at"],
            destination_message_ids=message_ids("destination_message_ids"),
            destination_grouped_id=(
                int(row["destination_grouped_id"])
                if row["destination_grouped_id"] is not None
                else None
            ),
            caption_applied=bool(row["caption_applied"]),
        )

    def next_candidate(
        self,
        source_channel: str,
        run_date: str,
        minimum_short_edge: int,
        settle_seconds: int,
        excluded: set[int],
        retryable_before_date: str | None = None,
    ) -> MediaGroup | None:
        cutoff = (datetime.now(UTC) - timedelta(seconds=settle_seconds)).isoformat()
        placeholders = ",".join("?" for _ in excluded)
        excluded_sql = (
            f"AND CAST(grouped_id AS INTEGER) NOT IN ({placeholders})" if excluded else ""
        )
        retryable_sql = ""
        retryable_parameters: list[Any] = []
        if retryable_before_date is not None:
            retryable_sql = (
                "AND (status NOT IN ('retryable', 'delivery_retryable') "
                "OR selected_date IS NULL "
                "OR selected_date < ?)"
            )
            retryable_parameters.append(retryable_before_date)
        parameters: list[Any] = [
            source_channel,
            minimum_short_edge,
            cutoff,
            *retryable_parameters,
            *sorted(excluded),
        ]
        row = self.connection.execute(
            f"""
            SELECT * FROM media_groups
            WHERE source_channel = ?
              AND status IN (
                  'selected', 'retryable', 'downloading', 'transcoding', 'uploading',
                  'staging_uploading', 'staged', 'delivering', 'caption_pending',
                  'delivery_retryable', 'delivery_uncertain'
              )
              AND video_count = 1
              AND MIN(width, height) >= ?
              AND newest_message_at <= ?
              {retryable_sql}
              {excluded_sql}
            ORDER BY CASE
                         WHEN status IN ('caption_pending', 'delivering', 'staged',
                                         'delivery_retryable', 'staging_uploading',
                                         'delivery_uncertain', 'uploading') THEN 0
                         ELSE 1
                     END,
                     selected_date, updated_at
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        if row is None:
            parameters = [source_channel, minimum_short_edge, cutoff, *sorted(excluded)]
            row = self.connection.execute(
                f"""
                SELECT * FROM media_groups
                WHERE source_channel = ?
                  AND status = 'indexed'
                  AND video_count = 1
                  AND MIN(width, height) >= ?
                  AND newest_message_at <= ?
                  {excluded_sql}
                ORDER BY RANDOM()
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return self._to_group(row) if row else None

    def preview_candidates(
        self,
        source_channel: str,
        minimum_short_edge: int,
        settle_seconds: int,
        limit: int,
        retryable_before_date: str | None = None,
    ) -> list[MediaGroup]:
        cutoff = (datetime.now(UTC) - timedelta(seconds=settle_seconds)).isoformat()
        retryable_sql = ""
        parameters: list[Any] = [source_channel, minimum_short_edge, cutoff]
        if retryable_before_date is not None:
            retryable_sql = (
                "AND (status NOT IN ('retryable', 'delivery_retryable') "
                "OR selected_date IS NULL "
                "OR selected_date < ?)"
            )
            parameters.append(retryable_before_date)
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT * FROM media_groups
            WHERE source_channel = ?
              AND status IN ('indexed', 'selected', 'retryable', 'downloading',
                             'transcoding', 'uploading', 'staging_uploading', 'staged',
                             'delivering', 'caption_pending', 'delivery_retryable',
                             'delivery_uncertain')
              AND video_count = 1 AND MIN(width, height) >= ? AND newest_message_at <= ?
              {retryable_sql}
            ORDER BY RANDOM() LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [self._to_group(row) for row in rows]

    def record_daily_stats(
        self,
        stats_date: str,
        *,
        published: int = 0,
        attempted: int = 0,
        rejected: int = 0,
        retryable_failures: int = 0,
        reconciled: int = 0,
        paused_reason: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO daily_stats (
                stats_date, published, attempted, rejected,
                retryable_failures, reconciled, paused_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stats_date) DO UPDATE SET
                published=daily_stats.published + excluded.published,
                attempted=daily_stats.attempted + excluded.attempted,
                rejected=daily_stats.rejected + excluded.rejected,
                retryable_failures=(
                    daily_stats.retryable_failures + excluded.retryable_failures
                ),
                reconciled=daily_stats.reconciled + excluded.reconciled,
                paused_reason=COALESCE(excluded.paused_reason, daily_stats.paused_reason)
            """,
            (
                stats_date,
                published,
                attempted,
                rejected,
                retryable_failures,
                reconciled,
                paused_reason,
            ),
        )
        self.connection.commit()

    def daily_stats(self, stats_date: str) -> DailyStats:
        row = self.connection.execute(
            "SELECT * FROM daily_stats WHERE stats_date = ?", (stats_date,)
        ).fetchone()
        if row is None:
            return DailyStats(stats_date=stats_date)
        return DailyStats(
            stats_date=str(row["stats_date"]),
            published=int(row["published"]),
            attempted=int(row["attempted"]),
            rejected=int(row["rejected"]),
            retryable_failures=int(row["retryable_failures"]),
            reconciled=int(row["reconciled"]),
            paused_reason=row["paused_reason"],
        )

    def continuous_report_cursor(self, default_date: str) -> str:
        key = "continuous:last_report_date"
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return str(row["value"])
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", (key, default_date)
        )
        self.connection.commit()
        return default_date

    def set_continuous_report_cursor(self, report_date: str) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES ('continuous:last_report_date', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (report_date,),
        )
        self.connection.commit()

    def begin_attempt(self, source_channel: str, grouped_id: int, run_date: str) -> None:
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='selected', selected_date=?, attempts=attempts+1,
                attempt_caption_html=NULL, attempt_caption_plain=NULL,
                upload_started_at=NULL, staging_channel=NULL,
                staging_upload_started_at=NULL, staging_message_ids=NULL,
                staging_grouped_id=NULL, staged_at=NULL,
                delivery_started_at=NULL, delivery_attempts=0,
                destination_message_ids=NULL, destination_grouped_id=NULL,
                caption_applied=0, last_error=NULL, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (run_date, datetime.now(UTC).isoformat(), source_channel, str(grouped_id)),
        )
        self.connection.commit()

    def set_status(self, source_channel: str, grouped_id: int, status: str) -> None:
        self.connection.execute(
            """
            UPDATE media_groups SET status=?, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (status, datetime.now(UTC).isoformat(), source_channel, str(grouped_id)),
        )
        self.connection.commit()

    def has_staging_album(self, source_channel: str, grouped_id: int) -> bool:
        row = self.connection.execute(
            """
            SELECT staging_message_ids FROM media_groups
            WHERE source_channel=? AND grouped_id=?
            """,
            (source_channel, str(grouped_id)),
        ).fetchone()
        return bool(row and row["staging_message_ids"])

    def group_status(self, source_channel: str, grouped_id: int) -> str | None:
        row = self.connection.execute(
            """
            SELECT status FROM media_groups
            WHERE source_channel=? AND grouped_id=?
            """,
            (source_channel, str(grouped_id)),
        ).fetchone()
        return str(row["status"]) if row else None

    def begin_upload(
        self, source_channel: str, grouped_id: int, caption_html: str, caption_plain: str
    ) -> str:
        started_at = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='uploading', upload_started_at=?, attempt_caption_html=?,
                attempt_caption_plain=?, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                started_at,
                caption_html,
                caption_plain,
                started_at,
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()
        return started_at

    def begin_staging_upload(
        self,
        source_channel: str,
        grouped_id: int,
        staging_channel: str | int,
        caption_html: str,
        caption_plain: str,
    ) -> str:
        started_at = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='staging_uploading', staging_channel=?,
                staging_upload_started_at=?, attempt_caption_html=?,
                attempt_caption_plain=?, last_error=NULL, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                str(staging_channel),
                started_at,
                caption_html,
                caption_plain,
                started_at,
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()
        return started_at

    def mark_staged(
        self,
        source_channel: str,
        grouped_id: int,
        staging_channel: str | int,
        receipt: DeliveryReceipt,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='staged', staging_channel=?, staging_message_ids=?,
                staging_grouped_id=?, staged_at=?, last_error=NULL, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                str(staging_channel),
                json.dumps(receipt.message_ids),
                str(receipt.grouped_id),
                now,
                now,
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()

    def begin_delivery(self, source_channel: str, grouped_id: int) -> str:
        started_at = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='delivering', delivery_started_at=?,
                delivery_attempts=delivery_attempts+1, last_error=NULL, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (started_at, started_at, source_channel, str(grouped_id)),
        )
        self.connection.commit()
        return started_at

    def mark_caption_pending(
        self,
        source_channel: str,
        grouped_id: int,
        receipt: DeliveryReceipt,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='caption_pending', destination_message_ids=?,
                destination_grouped_id=?, caption_applied=0, last_error=?, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                json.dumps(receipt.message_ids),
                str(receipt.grouped_id),
                error[:2000] if error else None,
                now,
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()

    def mark_delivery_failure(
        self,
        source_channel: str,
        grouped_id: int,
        error: str,
        *,
        uncertain: bool = False,
    ) -> None:
        status = "delivery_uncertain" if uncertain else "delivery_retryable"
        self.connection.execute(
            """
            UPDATE media_groups SET status=?, last_error=?, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                status,
                error[:2000],
                datetime.now(UTC).isoformat(),
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()

    def mark_published(
        self,
        source_channel: str,
        grouped_id: int,
        destination_message_ids: list[int],
        destination_grouped_id: int,
        published_date: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            UPDATE media_groups
            SET status='published', destination_message_ids=?, destination_grouped_id=?,
                published_date=?, caption_applied=1, last_error=NULL, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                json.dumps(destination_message_ids),
                str(destination_grouped_id),
                published_date,
                now,
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()

    def mark_failure(
        self, source_channel: str, grouped_id: int, error: str, *, permanent: bool
    ) -> None:
        status = "rejected" if permanent else "retryable"
        self.connection.execute(
            """
            UPDATE media_groups SET status=?, last_error=?, updated_at=?
            WHERE source_channel=? AND grouped_id=?
            """,
            (
                status,
                error[:2000],
                datetime.now(UTC).isoformat(),
                source_channel,
                str(grouped_id),
            ),
        )
        self.connection.commit()

    def published_count(self, source_channel: str, run_date: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM media_groups
            WHERE source_channel=? AND status='published' AND published_date=?
            """,
            (source_channel, run_date),
        ).fetchone()
        return int(row["count"])

    def counts(self, source_channel: str) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count FROM media_groups
            WHERE source_channel=? GROUP BY status
            """,
            (source_channel,),
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
