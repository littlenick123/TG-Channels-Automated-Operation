from __future__ import annotations

import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    """Raised when application configuration is invalid."""


def _read_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _tag_key(tag: str) -> str:
    value = tag.strip()
    if value and not value.startswith("#"):
        value = f"#{value}"
    return unicodedata.normalize("NFKC", value).casefold()


def _channel(value: str | int) -> str | int:
    if isinstance(value, int):
        return value
    stripped = str(value).strip()
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if not stripped:
        raise ConfigError("频道标识不能为空")
    return stripped


def _resolve_path(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True, slots=True)
class AppConfig:
    api_id: int
    api_hash: str
    phone: str | None
    session_path: Path
    source_channel: str | int
    target_channel: str | int
    keep_tags: tuple[str, ...]
    drop_tags: tuple[str, ...]
    caption_limit: int
    timezone: ZoneInfo
    daily_time: str
    daily_success_count: int
    ffmpeg_path: str
    ffprobe_path: str
    ffmpeg_threads: int
    crf: int
    preset: str
    audio_bitrate: str
    minimum_source_short_edge: int
    album_settle_seconds: int
    disk_reserve_bytes: int
    database_path: Path
    work_dir: Path
    max_candidates_per_run: int
    max_runtime_hours: float
    flood_sleep_threshold_seconds: int
    retry_delays_seconds: tuple[int, ...]
    notify_saved_messages: bool


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是 TOML 表")
    return value


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}")
    _read_dotenv(config_path.parent / ".env")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    telegram = _section(data, "telegram")
    content = _section(data, "content")
    schedule = _section(data, "schedule")
    processing = _section(data, "processing")
    runtime = _section(data, "runtime")
    base = config_path.parent

    api_id_raw = os.getenv("TG_API_ID", "")
    api_hash = os.getenv("TG_API_HASH", "").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ConfigError("TG_API_ID 必须是整数") from exc
    if api_id <= 0 or not api_hash:
        raise ConfigError(".env 中必须设置有效的 TG_API_ID 和 TG_API_HASH")

    keep_tags = tuple(str(tag).strip() for tag in content.get("keep_tags", []))
    drop_tags = tuple(str(tag).strip() for tag in content.get("drop_tags", []))
    keep_keys = {_tag_key(tag) for tag in keep_tags if tag}
    drop_keys = {_tag_key(tag) for tag in drop_tags if tag}
    overlap = keep_keys & drop_keys
    if overlap:
        raise ConfigError(f"keep_tags 与 drop_tags 不能重叠：{', '.join(sorted(overlap))}")

    daily_time = str(schedule.get("daily_time", "00:01"))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
        raise ConfigError("daily_time 必须使用 HH:MM 24 小时格式")
    try:
        timezone = ZoneInfo(str(schedule.get("timezone", "Asia/Shanghai")))
    except Exception as exc:
        raise ConfigError("timezone 不是有效的 IANA 时区") from exc

    try:
        source_channel = _channel(telegram["source_channel"])
        target_channel = _channel(telegram["target_channel"])
    except KeyError as exc:
        raise ConfigError(f"缺少配置项 telegram.{exc.args[0]}") from exc

    config = AppConfig(
        api_id=api_id,
        api_hash=api_hash,
        phone=os.getenv("TG_PHONE") or None,
        session_path=_resolve_path(base, os.getenv("TG_SESSION_PATH", "./data/telegram-user")),
        source_channel=source_channel,
        target_channel=target_channel,
        keep_tags=keep_tags,
        drop_tags=drop_tags,
        caption_limit=int(content.get("caption_limit", 1024)),
        timezone=timezone,
        daily_time=daily_time,
        daily_success_count=int(schedule.get("daily_success_count", 4)),
        ffmpeg_path=str(processing.get("ffmpeg_path", "ffmpeg")),
        ffprobe_path=str(processing.get("ffprobe_path", "ffprobe")),
        ffmpeg_threads=int(processing.get("ffmpeg_threads", 3)),
        crf=int(processing.get("crf", 23)),
        preset=str(processing.get("preset", "medium")),
        audio_bitrate=str(processing.get("audio_bitrate", "128k")),
        minimum_source_short_edge=int(processing.get("minimum_source_short_edge", 1080)),
        album_settle_seconds=int(processing.get("album_settle_seconds", 300)),
        disk_reserve_bytes=int(processing.get("disk_reserve_bytes", 1_073_741_824)),
        database_path=_resolve_path(base, str(runtime.get("database_path", "./data/operator.db"))),
        work_dir=_resolve_path(base, str(runtime.get("work_dir", "./work"))),
        max_candidates_per_run=int(runtime.get("max_candidates_per_run", 12)),
        max_runtime_hours=float(runtime.get("max_runtime_hours", 6)),
        flood_sleep_threshold_seconds=int(
            runtime.get("flood_sleep_threshold_seconds", 60)
        ),
        retry_delays_seconds=tuple(
            int(value) for value in runtime.get("retry_delays_seconds", [30, 120, 600])
        ),
        notify_saved_messages=bool(runtime.get("notify_saved_messages", True)),
    )
    if config.daily_success_count < 1 or config.max_candidates_per_run < config.daily_success_count:
        raise ConfigError("每日成功数必须大于 0，最大候选数不得小于每日成功数")
    if not 1 <= config.ffmpeg_threads <= 4:
        raise ConfigError("4C VPS 的 ffmpeg_threads 必须在 1 到 4 之间")
    if not 0 <= config.crf <= 51:
        raise ConfigError("CRF 必须在 0 到 51 之间")
    if config.caption_limit < 1:
        raise ConfigError("caption_limit 必须大于 0")
    if not 1 <= config.flood_sleep_threshold_seconds <= 86_400:
        raise ConfigError("flood_sleep_threshold_seconds 必须在 1 到 86400 秒之间")
    return config
