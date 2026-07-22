"""Technical media quality gate backed by FFprobe and FFmpeg decoding."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from axonflow.media.render import TimelineCompiler
from axonflow.tools.base import Tool, ToolResult
from axonflow.tools.media_probe import MediaProbeTool


class MediaQualityCheckTool(Tool):
    name = "media_quality_check"
    description = "校验成片的封装、视频/音频编码、尺寸、时长，并完整解码检查损坏"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "expected_width": {"type": "integer", "default": 1920},
            "expected_height": {"type": "integer", "default": 1080},
            "expected_duration_ms": {"type": "integer", "default": 12000},
        },
        "required": ["path"],
    }

    async def execute(
        self,
        path: str,
        expected_width: int = 1920,
        expected_height: int = 1080,
        expected_duration_ms: int = 12000,
        duration_tolerance_ms: int = 250,
        expect_subtitles: bool = False,
        timeout: int = 300,
        **_kwargs: Any,
    ) -> ToolResult:
        probe = await MediaProbeTool().execute(path=path, timeout=min(timeout, 300))
        if not probe.success:
            return ToolResult(success=False, error=f"Quality probe failed: {probe.error}")
        metadata = json.loads(probe.output or "{}")
        checks = {
            "video_codec_h264": metadata.get("video_codec") == "h264",
            "audio_codec_aac": metadata.get("audio_codec") == "aac",
            "dimensions": (
                metadata.get("width") == expected_width
                and metadata.get("height") == expected_height
            ),
            "duration": (
                isinstance(metadata.get("duration_ms"), int)
                and abs(metadata["duration_ms"] - expected_duration_ms) <= duration_tolerance_ms
            ),
            "has_audio": bool(metadata.get("sample_rate") and metadata.get("channels")),
            "sample_rate_48khz": metadata.get("sample_rate") == 48000,
            "stereo_audio": metadata.get("channels") == 2,
        }
        if expect_subtitles:
            checks["subtitle_track"] = any(
                stream.get("codec_type") == "subtitle" for stream in metadata.get("streams", [])
            )
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            return ToolResult(
                success=False,
                error=f"Media quality checks failed: {', '.join(failed)}",
            )

        media_path = TimelineCompiler.local_path(path)
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(media_path),
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except FileNotFoundError:
            return ToolResult(success=False, error="ffmpeg executable was not found")
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Media decode quality check timed out")
        if process.returncode != 0 or stderr.strip():
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"Media decode check failed: {detail[:1000]}")
        return ToolResult(
            success=True,
            output=json.dumps(
                {"verdict": "passed", "checks": checks, "metadata": metadata},
                ensure_ascii=False,
            ),
        )
