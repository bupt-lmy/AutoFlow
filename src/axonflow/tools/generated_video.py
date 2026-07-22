"""Delivery normalization for AI-generated video clips."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from axonflow.media.render import TimelineCompiler
from axonflow.tools.base import Tool, ToolResult
from axonflow.tools.media_probe import MediaProbeTool
from axonflow.tools.video_edit import FFMPEG_FULL, _binary


class GeneratedVideoFinalizeTool(Tool):
    """Normalize a generated clip and add an always-visible synthetic-content label."""

    name = "generated_video_finalize"
    description = "将 AI 生成视频标准化为 H.264/AAC，并永久烧录 AI GENERATED 虚构标识"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "output_name": {"type": "string"},
        },
        "required": ["path"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/generated-final") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        path: str,
        output_name: str | None = None,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        timeout: int = 1800,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(path)
        if not source.is_file():
            return ToolResult(success=False, error=f"Generated video not found: {source}")
        probe_result = await MediaProbeTool().execute(path=str(source))
        if not probe_result.success:
            return ToolResult(
                success=False, error=f"Generated video is invalid: {probe_result.error}"
            )
        source_metadata = json.loads(probe_result.output or "{}")
        if not source_metadata.get("video_codec"):
            return ToolResult(success=False, error="Generated artifact has no video stream")
        name = output_name or f"generated-final-{uuid.uuid4().hex[:12]}.mp4"
        if Path(name).name != name:
            return ToolResult(success=False, error="output_name must not contain directories")
        output = (self.output_dir / f"{Path(name).stem}.mp4").resolve()
        if output.parent != self.output_dir or output.exists():
            return ToolResult(success=False, error="unsafe or existing generated-video output path")
        output.parent.mkdir(parents=True, exist_ok=True)

        font = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},"
            "drawbox=x=24:y=24:w=520:h=58:color=black@0.70:t=fill,"
            f"drawtext=fontfile='{font}':text='AI GENERATED - FICTIONAL':"
            "fontcolor=white:fontsize=30:x=42:y=37"
        )
        arguments = [
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-n",
            "-v",
            "error",
            "-i",
            str(source),
        ]
        has_audio = bool(source_metadata.get("audio_codec"))
        if not has_audio:
            arguments.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        arguments.extend(
            [
                "-vf",
                video_filter,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if has_audio else "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Generated video finalization timed out")
        if process.returncode != 0 or not output.is_file():
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                success=False, error=f"Generated video finalization failed: {detail[:1200]}"
            )
        final_probe = await MediaProbeTool().execute(path=str(output))
        metadata = json.loads(final_probe.output or "{}") if final_probe.success else {}
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output),
                    "media_type": "video/mp4",
                    "ai_generated": True,
                    "fictional_content": True,
                    "disclosure_burned": True,
                    "disclosure_text": "AI GENERATED - FICTIONAL",
                    **metadata,
                },
                ensure_ascii=False,
            ),
        )
