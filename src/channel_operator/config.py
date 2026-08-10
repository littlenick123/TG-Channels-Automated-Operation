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
class ChannelGroupConfig:
    name: str
    source_channel: str | int
    target_channel: str | int
    database_path: Path
    daily_success_count: int


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    bot_token: str
    chat_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AppConfig:
    api_id: int
    api_hash: str
    phone: str | None
    session_path: Path
    channel_groups: tuple[ChannelGroupConfig, ...]
    reporting: ReportingConfig
    keep_tags: tuple[str, ...]
    drop_tags: tuple[str, ...]
    caption_limit: int
    intro_footer: str
    timezone: ZoneInfo
    daily_time: str
    ffmpeg_path: str
    ffprobe_path: str
    ffmpeg_threads: int
    crf: int
    preset: str
    audio_bitrate: str
    minimum_source_short_edge: int
    album_settle_seconds: int
    disk_reserve_bytes: int
    database_dir: Path
    work_dir: Path
    max_candidates_per_run: int
    max_runtime_hours: float
    download_concurrency: int
    flood_sleep_threshold_seconds: int
    retry_delays_seconds: tuple[int, ...]


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

    content = _section(data, "content")
    schedule = _section(data, "schedule")
    processing = _section(data, "processing")
    runtime = _section(data, "runtime")
    reporting = _section(data, "reporting")
    base = config_path.parent
    database_dir = _resolve_path(base, str(runtime.get("database_dir", "./data")))

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
    intro_footer_raw = content.get("intro_footer", "")
    if not isinstance(intro_footer_raw, str):
        raise ConfigError("content.intro_footer 必须是字符串")
    intro_footer = intro_footer_raw.strip()
    if "\n" in intro_footer or "\r" in intro_footer:
        raise ConfigError("content.intro_footer 必须是单行文本")

    daily_time = str(schedule.get("daily_time", "00:01"))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
        raise ConfigError("daily_time 必须使用 HH:MM 24 小时格式")
    try:
        timezone = ZoneInfo(str(schedule.get("timezone", "Asia/Shanghai")))
    except Exception as exc:
        raise ConfigError("timezone 不是有效的 IANA 时区") from exc

    raw_groups = data.get("channel_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ConfigError("必须配置至少一个 [[channel_groups]]；旧版单频道配置已不再支持")
    channel_groups: list[ChannelGroupConfig] = []
    names: set[str] = set()
    database_paths: set[str] = set()
    for index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            raise ConfigError(f"channel_groups 第 {index} 项必须是 TOML 表")
        try:
            name = str(raw_group["name"]).strip()
            source_channel = _channel(raw_group["source_channel"])
            target_channel = _channel(raw_group["target_channel"])
            daily_success_count = int(raw_group["daily_success_count"])
        except KeyError as exc:
            raise ConfigError(
                f"channel_groups 第 {index} 项缺少 {exc.args[0]}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"channel_groups 第 {index} 项包含无效值") from exc
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ConfigError(
                f"频道组名称 {name!r} 无效，只能使用字母、数字、下划线或连字符"
            )
        if name in names:
            raise ConfigError(f"频道组名称不能重复：{name}")
        if "database_path" in raw_group:
            raise ConfigError(
                f"频道组 {name} 不再使用 database_path；"
                "请在 [runtime] 中统一配置 database_dir"
            )
        database_path = database_dir / f"{name}.db"
        path_key = os.path.normcase(str(database_path))
        if path_key in database_paths:
            raise ConfigError(f"频道组数据库路径不能重复：{database_path}")
        if daily_success_count < 1:
            raise ConfigError(f"频道组 {name} 的 daily_success_count 必须大于 0")
        names.add(name)
        database_paths.add(path_key)
        channel_groups.append(
            ChannelGroupConfig(
                name=name,
                source_channel=source_channel,
                target_channel=target_channel,
                database_path=database_path,
                daily_success_count=daily_success_count,
            )
        )

    report_bot_token = os.getenv("TG_REPORT_BOT_TOKEN", "").strip()
    if not report_bot_token:
        raise ConfigError(".env 中必须设置 TG_REPORT_BOT_TOKEN")
    has_chat_id = "chat_id" in reporting
    has_chat_ids = "chat_ids" in reporting
    if has_chat_id and has_chat_ids:
        raise ConfigError("reporting.chat_id 与 reporting.chat_ids 不能同时配置")
    if has_chat_ids:
        raw_chat_ids = reporting["chat_ids"]
        if not isinstance(raw_chat_ids, list) or not raw_chat_ids:
            raise ConfigError("reporting.chat_ids 必须是非空整数数组")
    elif has_chat_id:
        raw_chat_ids = [reporting["chat_id"]]
    else:
        raise ConfigError("缺少配置项 reporting.chat_ids")
    try:
        report_chat_ids = tuple(int(value) for value in raw_chat_ids)
    except (TypeError, ValueError) as exc:
        raise ConfigError("reporting.chat_ids 必须全部是整数") from exc
    if any(chat_id <= 0 for chat_id in report_chat_ids):
        raise ConfigError("私人会话 reporting.chat_ids 必须全部是正整数")
    if len(set(report_chat_ids)) != len(report_chat_ids):
        raise ConfigError("reporting.chat_ids 不能包含重复值")

    config = AppConfig(
        api_id=api_id,
        api_hash=api_hash,
        phone=os.getenv("TG_PHONE") or None,
        session_path=_resolve_path(base, os.getenv("TG_SESSION_PATH", "./data/telegram-user")),
        channel_groups=tuple(channel_groups),
        reporting=ReportingConfig(report_bot_token, report_chat_ids),
        keep_tags=keep_tags,
        drop_tags=drop_tags,
        caption_limit=int(content.get("caption_limit", 1024)),
        intro_footer=intro_footer,
        timezone=timezone,
        daily_time=daily_time,
        ffmpeg_path=str(processing.get("ffmpeg_path", "ffmpeg")),
        ffprobe_path=str(processing.get("ffprobe_path", "ffprobe")),
        ffmpeg_threads=int(processing.get("ffmpeg_threads", 3)),
        crf=int(processing.get("crf", 23)),
        preset=str(processing.get("preset", "medium")),
        audio_bitrate=str(processing.get("audio_bitrate", "128k")),
        minimum_source_short_edge=int(processing.get("minimum_source_short_edge", 1080)),
        album_settle_seconds=int(processing.get("album_settle_seconds", 300)),
        disk_reserve_bytes=int(processing.get("disk_reserve_bytes", 1_073_741_824)),
        database_dir=database_dir,
        work_dir=_resolve_path(base, str(runtime.get("work_dir", "./work"))),
        max_candidates_per_run=int(runtime.get("max_candidates_per_run", 12)),
        max_runtime_hours=float(runtime.get("max_runtime_hours", 6)),
        download_concurrency=int(runtime.get("download_concurrency", 4)),
        flood_sleep_threshold_seconds=int(
            runtime.get("flood_sleep_threshold_seconds", 60)
        ),
        retry_delays_seconds=tuple(
            int(value) for value in runtime.get("retry_delays_seconds", [30, 120, 360])
        ),
    )
    oversized_groups = [
        group.name
        for group in config.channel_groups
        if group.daily_success_count > config.max_candidates_per_run
    ]
    if oversized_groups:
        raise ConfigError(
            "以下频道组的 daily_success_count 超过 max_candidates_per_run："
            + ", ".join(oversized_groups)
        )
    if not 1 <= config.ffmpeg_threads <= 4:
        raise ConfigError("4C VPS 的 ffmpeg_threads 必须在 1 到 4 之间")
    if not 0 <= config.crf <= 51:
        raise ConfigError("CRF 必须在 0 到 51 之间")
    if not 1 <= config.caption_limit <= 1024:
        raise ConfigError("caption_limit 必须在 1 到 1024 之间")
    if not 1 <= config.download_concurrency <= 8:
        raise ConfigError("download_concurrency 必须在 1 到 8 之间")
    if not 1 <= config.flood_sleep_threshold_seconds <= 86_400:
        raise ConfigError("flood_sleep_threshold_seconds 必须在 1 到 86400 秒之间")
    return config
