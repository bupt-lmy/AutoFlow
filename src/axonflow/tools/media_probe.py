"""Deterministic local-media inspection through FFprobe."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from axonflow.media.models import MediaProbeResult
from axonflow.tools.base import Tool, ToolResult


class MediaProbeTool(Tool):
    """Inspect a local media file without invoking a shell."""

    name = "media_probe"
    description = (
        "使用 FFprobe 检查本地音视频文件，返回时长、尺寸、帧率、编码和音轨信息"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "本地文件路径或 file:// URI",
            },
            "timeout": {
                "type": "integer",
                "description": "超时时间（秒），默认 30，最大 300",
                "default": 30,
                "minimum": 1,
                "maximum": 300,
            },
        },
        "required": ["path"],
    }

    async def execute(self, path: str, timeout: int = 30, **_kwargs: Any) -> ToolResult:
        try:
            media_path = self._local_path(path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        if not media_path.is_file():
            return ToolResult(success=False, error=f"Media file not found: {media_path}")
        if not 1 <= timeout <= 300:
            return ToolResult(success=False, error="timeout must be between 1 and 300 seconds")

        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(media_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error="ffprobe executable was not found; install FFmpeg on the media worker",
            )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error=f"ffprobe timed out after {timeout}s")

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                success=False,
                error=f"ffprobe failed with exit code {process.returncode}: {detail[:500]}",
            )
        try:
            raw = json.loads(stdout.decode("utf-8"))
            normalized = self.normalize_probe(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return ToolResult(success=False, error=f"Invalid ffprobe output: {exc}")
        return ToolResult(success=True, output=normalized.model_dump_json())

    @staticmethod
    def _local_path(value: str) -> Path:
        parsed = urlparse(value)
        if parsed.scheme not in {"", "file"}:
            raise ValueError("media_probe accepts only local paths or file:// URIs")
        if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
            raise ValueError("remote file URI hosts are not supported")
        raw_path = unquote(parsed.path) if parsed.scheme == "file" else value
        return Path(raw_path).expanduser().resolve()

    @classmethod
    def normalize_probe(cls, payload: dict[str, Any]) -> MediaProbeResult:
        streams = payload.get("streams")
        if not isinstance(streams, list):
            streams = []
        format_data = payload.get("format")
        if not isinstance(format_data, dict):
            format_data = {}
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            {},
        )
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            {},
        )
        return MediaProbeResult(
            format_name=cls._text(format_data.get("format_name")),
            duration_ms=cls._milliseconds(format_data.get("duration")),
            size_bytes=cls._integer(format_data.get("size")),
            width=cls._positive_integer(video.get("width")),
            height=cls._positive_integer(video.get("height")),
            fps=cls._frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            video_codec=cls._text(video.get("codec_name")),
            audio_codec=cls._text(audio.get("codec_name")),
            sample_rate=cls._positive_integer(audio.get("sample_rate")),
            channels=cls._positive_integer(audio.get("channels")),
            streams=[stream for stream in streams if isinstance(stream, dict)],
        )

    @staticmethod
    def _text(value: Any) -> str | None:
        return str(value) if value not in {None, "", "N/A"} else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @classmethod
    def _positive_integer(cls, value: Any) -> int | None:
        parsed = cls._integer(value)
        return parsed if parsed is not None and parsed > 0 else None

    @staticmethod
    def _milliseconds(value: Any) -> int | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return round(parsed * 1000) if parsed >= 0 else None

    @staticmethod
    def _frame_rate(value: Any) -> float | None:
        if value in {None, "", "0/0", "N/A"}:
            return None
        try:
            text = str(value)
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                rate = float(numerator) / float(denominator)
            else:
                rate = float(text)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return rate if rate > 0 else None
