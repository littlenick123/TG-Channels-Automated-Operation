from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import VideoInfo

THUMBNAIL_MAX_BYTES = 20 * 1024


class MediaError(RuntimeError):
    """Raised when probing or processing a media file fails."""


class InvalidSourceMedia(MediaError):
    """Raised when source media permanently violates eligibility rules."""


class MediaProcessor:
    def __init__(self, config: AppConfig):
        self.config = config

    async def _run(self, *arguments: str) -> tuple[str, str]:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            raise
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            command = Path(arguments[0]).name
            raise MediaError(f"{command} 失败（退出码 {process.returncode}）：{err[-2000:]}")
        return out, err

    async def version(self, executable: str) -> str:
        out, _ = await self._run(executable, "-version")
        return out.splitlines()[0] if out else executable

    async def probe(self, path: Path) -> VideoInfo:
        out, _ = await self._run(
            self.config.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,width,height,duration:stream_tags=rotate:stream_side_data=rotation",
            "-of",
            "json",
            str(path),
        )
        try:
            payload: dict[str, Any] = json.loads(out)
            video = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
            duration = float(video.get("duration") or payload["format"]["duration"])
            width = int(video["width"])
            height = int(video["height"])
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise InvalidSourceMedia(f"无法读取视频元数据：{path}") from exc
        rotation = int(float(video.get("tags", {}).get("rotate", 0)))
        for side_data in video.get("side_data_list", []):
            if "rotation" in side_data:
                rotation = int(float(side_data["rotation"]))
                break
        has_audio = any(
            stream.get("codec_type") == "audio" for stream in payload.get("streams", [])
        )
        if duration <= 0 or width <= 0 or height <= 0:
            raise InvalidSourceMedia("视频时长或分辨率无效")
        return VideoInfo(path, duration, width, height, rotation, has_audio)

    def check_disk(self, source_size: int) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.config.work_dir).free
        required = max(0, source_size) * 3 + self.config.disk_reserve_bytes
        if free < required:
            raise MediaError(f"磁盘空间不足：需要 {required} 字节，可用 {free} 字节")

    def validate_source(self, info: VideoInfo) -> None:
        short_edge = min(info.display_width, info.display_height)
        if short_edge < self.config.minimum_source_short_edge:
            raise InvalidSourceMedia(
                f"源视频短边只有 {short_edge}px，低于 {self.config.minimum_source_short_edge}px"
            )

    async def transcode(self, source: Path, destination: Path, info: VideoInfo) -> VideoInfo:
        bounds = (1280, 720) if info.display_width >= info.display_height else (720, 1280)
        scale = (
            f"scale={bounds[0]}:{bounds[1]}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,setsar=1"
        )
        await self._run(
            self.config.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            scale,
            "-c:v",
            "libx264",
            "-preset",
            self.config.preset,
            "-crf",
            str(self.config.crf),
            "-pix_fmt",
            "yuv420p",
            "-threads",
            str(self.config.ffmpeg_threads),
            "-c:a",
            "aac",
            "-b:a",
            self.config.audio_bitrate,
            "-movflags",
            "+faststart",
            str(destination),
        )
        return await self.probe(destination)

    @staticmethod
    def screenshot_times(duration: float) -> tuple[float, float, float]:
        return (duration * 0.15, duration * 0.5, duration * 0.85)

    @staticmethod
    def thumbnail_time(duration: float) -> float:
        return duration * 0.1

    async def screenshots(self, video: Path, duration: float, directory: Path) -> list[Path]:
        paths: list[Path] = []
        for index, timestamp in enumerate(self.screenshot_times(duration), start=1):
            output = directory / f"frame_{index}.jpg"
            await self._run(
                self.config.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output),
            )
            paths.append(output)
        return paths

    async def thumbnail(self, video: Path, duration: float, destination: Path) -> Path:
        timestamp = self.thumbnail_time(duration)
        attempts = ((320, 8), (320, 12), (256, 12), (256, 16), (192, 16))
        for dimension, quality in attempts:
            scale = (
                f"scale={dimension}:{dimension}:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2"
            )
            await self._run(
                self.config.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-vf",
                scale,
                "-frames:v",
                "1",
                "-q:v",
                str(quality),
                str(destination),
            )
            if 0 < destination.stat().st_size <= THUMBNAIL_MAX_BYTES:
                return destination
        raise MediaError(
            f"无法生成不超过 {THUMBNAIL_MAX_BYTES} 字节的视频缩略图：{video}"
        )
