from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import AppConfig, ChannelGroupConfig
from .database import DatabaseIdentityError, StateDatabase
from .models import MessageSnapshot

if TYPE_CHECKING:
    from .telegram import TelegramGateway

LOGGER = logging.getLogger(__name__)


def canonical_source_key(source_channel: str | int) -> str:
    if isinstance(source_channel, int):
        return str(source_channel)
    return str(source_channel).strip().casefold()


def source_index_path(database_dir: Path, source_channel: str | int) -> Path:
    source_key = canonical_source_key(source_channel)
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:16]
    return database_dir / "source_indexes" / f"source_{digest}.db"


class SourceIndexDatabase:
    """Shared source-message cache without per-destination processing state."""

    def __init__(self, path: Path, source_channel: str | int):
        self.path = path
        self.source_key = canonical_source_key(source_channel)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        try:
            self._migrate()
            self._bind_identity()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

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

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_messages (
                message_id INTEGER PRIMARY KEY,
                grouped_id TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                is_video INTEGER NOT NULL,
                is_photo INTEGER NOT NULL,
                width INTEGER,
                height INTEGER,
                duration REAL,
                file_size INTEGER,
                published_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_shared_source_group
                ON source_messages(grouped_id, message_id);
            """
        )
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES ('schema_version', '1')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )
        self.connection.commit()

    def _bind_identity(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='identity:source_channel'"
        ).fetchone()
        if row is not None and str(row["value"]) != self.source_key:
            raise DatabaseIdentityError(
                f"共享源索引 {self.path} 属于源频道 {row['value']}，"
                f"预期 {self.source_key}"
            )
        if row is None:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("identity:source_channel", self.source_key),
            )
            self.connection.commit()

    def checkpoint(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='checkpoint'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def message_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM source_messages"
        ).fetchone()
        return int(row["count"])

    def bootstrap_complete(self) -> bool:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='bootstrap_complete'"
        ).fetchone()
        return bool(row and row["value"] == "1")

    @staticmethod
    def _snapshot_values(messages: Iterable[MessageSnapshot]) -> list[tuple[Any, ...]]:
        return [
            (
                message.message_id,
                str(message.grouped_id),
                message.caption,
                int(message.is_video),
                int(message.is_photo),
                message.width,
                message.height,
                message.duration,
                message.file_size,
                message.published_at.isoformat(),
            )
            for message in messages
            if message.grouped_id is not None
        ]

    def _upsert_values(self, values: Iterable[tuple[Any, ...]]) -> None:
        self.connection.executemany(
            """
            INSERT INTO source_messages (
                message_id, grouped_id, caption, is_video, is_photo,
                width, height, duration, file_size, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                grouped_id=excluded.grouped_id,
                caption=CASE
                    WHEN excluded.caption <> '' THEN excluded.caption
                    ELSE source_messages.caption
                END,
                is_video=excluded.is_video,
                is_photo=excluded.is_photo,
                width=COALESCE(excluded.width, source_messages.width),
                height=COALESCE(excluded.height, source_messages.height),
                duration=COALESCE(excluded.duration, source_messages.duration),
                file_size=COALESCE(excluded.file_size, source_messages.file_size),
                published_at=excluded.published_at
            """,
            values,
        )

    def save_messages(
        self, messages: Iterable[MessageSnapshot], checkpoint: int
    ) -> None:
        values = self._snapshot_values(messages)
        with self.transaction():
            if values:
                self._upsert_values(values)
            self.connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('checkpoint', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(checkpoint),),
            )
            self.connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('updated_at', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (datetime.now(UTC).isoformat(),),
            )

    def bootstrap_from_group_databases(
        self, databases: Iterable[tuple[Path, str]]
    ) -> tuple[int, int]:
        imported_before = self.message_count()
        highest_checkpoint = self.checkpoint()
        successful_databases = 0
        for path, source_channel in databases:
            if not path.is_file():
                continue
            connection: sqlite3.Connection | None = None
            try:
                uri = f"{path.resolve().as_uri()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=5)
                connection.row_factory = sqlite3.Row
                identity = connection.execute(
                    """
                    SELECT value FROM metadata
                    WHERE key='identity:source_channel'
                    """
                ).fetchone()
                if identity is not None and canonical_source_key(
                    str(identity["value"])
                ) != self.source_key:
                    LOGGER.warning("跳过源频道身份不匹配的旧数据库 %s", path)
                    continue
                rows = connection.execute(
                    """
                    SELECT message_id, grouped_id, caption, is_video, is_photo,
                           width, height, duration, file_size, published_at
                    FROM source_messages
                    WHERE source_channel=?
                    ORDER BY message_id
                    """,
                    (source_channel,),
                ).fetchall()
                checkpoint_row = connection.execute(
                    "SELECT value FROM metadata WHERE key=?",
                    (f"checkpoint:{source_channel}",),
                ).fetchone()
                checkpoint = int(checkpoint_row["value"]) if checkpoint_row else 0
                with self.transaction():
                    if rows:
                        self._upsert_values(tuple(tuple(row) for row in rows))
                    highest_checkpoint = max(highest_checkpoint, checkpoint)
                    self.connection.execute(
                        """
                        INSERT INTO metadata(key, value) VALUES ('checkpoint', ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (str(highest_checkpoint),),
                    )
                successful_databases += 1
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                LOGGER.warning("读取旧频道组数据库 %s 失败：%s", path, exc)
            finally:
                if connection is not None:
                    connection.close()
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES ('bootstrap_complete', '1')
            ON CONFLICT(key) DO UPDATE SET value='1'
            """
        )
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES ('updated_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (datetime.now(UTC).isoformat(),),
        )
        self.connection.commit()
        return self.message_count() - imported_before, successful_databases

    def message_batches_after(
        self, message_id: int, batch_size: int = 200
    ) -> Iterator[list[MessageSnapshot]]:
        cursor = self.connection.execute(
            """
            SELECT * FROM source_messages
            WHERE message_id > ?
            ORDER BY message_id
            """,
            (message_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            yield [
                MessageSnapshot(
                    message_id=int(row["message_id"]),
                    grouped_id=int(row["grouped_id"]),
                    caption=str(row["caption"]),
                    is_video=bool(row["is_video"]),
                    is_photo=bool(row["is_photo"]),
                    width=int(row["width"]) if row["width"] is not None else None,
                    height=int(row["height"]) if row["height"] is not None else None,
                    duration=(
                        float(row["duration"])
                        if row["duration"] is not None
                        else None
                    ),
                    file_size=(
                        int(row["file_size"])
                        if row["file_size"] is not None
                        else None
                    ),
                    published_at=datetime.fromisoformat(str(row["published_at"])),
                )
                for row in rows
            ]


class SourceIndexCoordinator:
    """Scan each unique source once and synchronize selected group databases."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._indexes: dict[str, SourceIndexDatabase] = {}
        self._scanned_sources: set[str] = set()
        self._source_errors: dict[str, Exception] = {}

    def close(self) -> None:
        for database in self._indexes.values():
            database.close()
        self._indexes.clear()

    def _index(self, group: ChannelGroupConfig) -> SourceIndexDatabase:
        source_key = canonical_source_key(group.source_channel)
        if source_key not in self._indexes:
            self._indexes[source_key] = SourceIndexDatabase(
                source_index_path(self.config.database_dir, group.source_channel),
                group.source_channel,
            )
        return self._indexes[source_key]

    def _bootstrap_databases(
        self, source_channel: str | int
    ) -> list[tuple[Path, str]]:
        source_key = canonical_source_key(source_channel)
        return [
            (group.database_path, str(group.source_channel))
            for group in self.config.channel_groups
            if canonical_source_key(group.source_channel) == source_key
        ]

    async def prepare_group(
        self,
        group: ChannelGroupConfig,
        database: StateDatabase,
        telegram: TelegramGateway,
    ) -> int:
        source_key = canonical_source_key(group.source_channel)
        if source_key in self._source_errors:
            raise self._source_errors[source_key]

        try:
            index = self._index(group)
            if not index.bootstrap_complete():
                imported, database_count = index.bootstrap_from_group_databases(
                    self._bootstrap_databases(group.source_channel)
                )
                LOGGER.info(
                    "共享源索引 %s 初始化：从 %s 个旧数据库导入 %s 条消息",
                    group.source_channel,
                    database_count,
                    imported,
                )
            if source_key not in self._scanned_sources:
                checkpoint = index.checkpoint()
                newest_id = checkpoint
                scanned = 0
                batch: list[MessageSnapshot] = []
                async for message in telegram.scan_messages(checkpoint):
                    batch.append(message)
                    newest_id = max(newest_id, message.message_id)
                    scanned += 1
                    if len(batch) >= 200:
                        index.save_messages(batch, newest_id)
                        batch.clear()
                if batch or newest_id != checkpoint:
                    index.save_messages(batch, newest_id)
                self._scanned_sources.add(source_key)
                LOGGER.info(
                    "源频道 %s 扫描完成：新消息 %s 条，检查点 %s",
                    group.source_channel,
                    scanned,
                    newest_id,
                )
            else:
                LOGGER.info("源频道 %s 本轮复用共享索引", group.source_channel)
        except Exception as exc:
            self._source_errors[source_key] = exc
            raise

        group_source = str(group.source_channel)
        group_checkpoint = database.checkpoint(group_source)
        shared_checkpoint = index.checkpoint()
        affected_groups: set[int] = set()
        synchronized = 0
        for batch in index.message_batches_after(group_checkpoint):
            batch_checkpoint = max(message.message_id for message in batch)
            database.save_messages(group_source, batch, batch_checkpoint)
            affected_groups.update(
                int(message.grouped_id)
                for message in batch
                if message.grouped_id is not None
            )
            synchronized += len(batch)
        if shared_checkpoint > database.checkpoint(group_source):
            database.save_messages(group_source, [], shared_checkpoint)
        group_count = database.refresh_groups(group_source, affected_groups)
        LOGGER.info(
            "频道组 %s 已复用共享索引：同步 %s 条消息，媒体组总数 %s",
            group.display_name,
            synchronized,
            group_count,
        )
        return group_count

    def details(self, source_channel: str | int) -> tuple[Path, int, int]:
        index = SourceIndexDatabase(
            source_index_path(self.config.database_dir, source_channel), source_channel
        )
        try:
            return index.path, index.checkpoint(), index.message_count()
        finally:
            index.close()
