from __future__ import annotations

import asyncio
import json
import math
import shutil
from pathlib import Path
from typing import Any

from PIL import ImageFont

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

    async def _run(
        self, *arguments: str, cwd: Path | None = None
    ) -> tuple[str, str]:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
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

    async def check_watermark_support(self) -> None:
        try:
            ImageFont.truetype(str(self.config.watermark_font_file), 24)
        except OSError as exc:
            raise MediaError(
                f"无法加载视频水印字体：{self.config.watermark_font_file}"
            ) from exc
        out, _ = await self._run(
            self.config.ffmpeg_path,
            "-hide_banner",
            "-filters",
        )
        if "drawtext" not in out:
            raise MediaError("当前 FFmpeg 不包含 drawtext 滤镜，无法生成文字水印")

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

    @staticmethod
    def transcode_bounds(output_height: int, *, landscape: bool) -> tuple[int, int]:
        long_edge = 2 * round(output_height * 8 / 9)
        return (
            (long_edge, output_height)
            if landscape
            else (output_height, long_edge)
        )

    @staticmethod
    def first_third_duration(duration: float) -> float:
        return duration / 3

    async def cut_first_third(
        self, source: Path, destination: Path, info: VideoInfo
    ) -> VideoInfo:
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
            "-t",
            f"{self.first_third_duration(info.duration):.6f}",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(destination),
        )
        return await self.probe(destination)

    @staticmethod
    def scaled_dimensions(
        width: int, height: int, bounds: tuple[int, int]
    ) -> tuple[int, int]:
        ratio = min(bounds[0] / width, bounds[1] / height)
        scaled_width = max(2, 2 * math.floor(width * ratio / 2))
        scaled_height = max(2, 2 * math.floor(height * ratio / 2))
        return scaled_width, scaled_height

    def watermark_style(
        self, text: str, frame_width: int, frame_height: int
    ) -> tuple[int, int]:
        font_size = max(1, round(frame_height * 0.05))
        maximum_width = frame_width * 0.9
        while True:
            border_width = max(1, round(font_size * 0.08))
            font = ImageFont.truetype(
                str(self.config.watermark_font_file), font_size
            )
            left, _, right, _ = font.getbbox(text, stroke_width=border_width)
            text_width = right - left
            if text_width <= maximum_width or font_size == 1:
                return font_size, border_width
            font_size = max(
                1,
                min(font_size - 1, math.floor(font_size * maximum_width / text_width)),
            )

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    async def transcode(
        self,
        source: Path,
        destination: Path,
        info: VideoInfo,
        *,
        watermark_text: str = "",
    ) -> VideoInfo:
        bounds = self.transcode_bounds(
            self.config.output_height,
            landscape=info.display_width >= info.display_height,
        )
        filters = (
            f"scale={bounds[0]}:{bounds[1]}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,setsar=1"
        )
        text_file: Path | None = None
        if watermark_text:
            frame_width, frame_height = self.scaled_dimensions(
                info.display_width, info.display_height, bounds
            )
            font_size, border_width = self.watermark_style(
                watermark_text, frame_width, frame_height
            )
            text_file = destination.parent / "watermark_text.txt"
            text_file.write_text(watermark_text, encoding="utf-8")
            font_file = self._escape_filter_value(
                self.config.watermark_font_file.as_posix()
            )
            filters += (
                ",drawtext="
                f"fontfile='{font_file}':"
                f"textfile='{text_file.name}':"
                "expansion=none:fontcolor=white:bordercolor=black:"
                f"borderw={border_width}:fontsize={font_size}:"
                "x=(w-text_w)/2:y=h*0.03:"
                "enable='gte(t\\,180)*lt(mod(t-180\\,180)\\,10)'"
            )
        try:
            await self._run(
                self.config.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source.resolve()),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                filters,
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
                str(destination.resolve()),
                cwd=destination.parent,
            )
        finally:
            if text_file is not None:
                text_file.unlink(missing_ok=True)
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
