"""Controlled Timeline-to-MP4 rendering tool."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import ValidationError

from axonflow.media.models import Timeline
from axonflow.media.render import TimelineCompiler, UnsupportedTimelineError
from axonflow.tools.base import Tool, ToolResult


class MediaRenderTool(Tool):
    name = "media_render"
    description = "将经过校验的 Timeline 确定性渲染为 MP4；第一版支持单视频轨顺序剪辑"
    parameters = {
        "type": "object",
        "properties": {
            "timeline": {"type": "object", "description": "Timeline JSON 对象"},
            "assets": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "asset_id 到本地文件路径或 file:// URI 的映射",
            },
            "output_path": {"type": "string", "description": "本地 .mp4 输出路径"},
            "overwrite": {"type": "boolean", "default": False},
            "timeout": {
                "type": "integer",
                "default": 3600,
                "minimum": 1,
                "maximum": 86400,
            },
        },
        "required": ["timeline", "assets", "output_path"],
    }

    async def execute(
        self,
        timeline: dict[str, Any],
        assets: dict[str, str],
        output_path: str,
        overwrite: bool = False,
        timeout: int = 3600,
        **_kwargs: Any,
    ) -> ToolResult:
        if not 1 <= timeout <= 86400:
            return ToolResult(success=False, error="timeout must be between 1 and 86400 seconds")
        try:
            parsed_timeline = Timeline.model_validate(timeline)
            plan = TimelineCompiler.compile(
                parsed_timeline,
                assets,
                output_path,
                overwrite=overwrite,
            )
        except (ValidationError, ValueError, UnsupportedTimelineError) as exc:
            return ToolResult(success=False, error=f"Invalid render request: {exc}")
        if plan.output_path.exists() and not overwrite:
            return ToolResult(
                success=False,
                error=f"Output file already exists: {plan.output_path}",
            )
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            process = await asyncio.create_subprocess_exec(
                *plan.arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error="ffmpeg executable was not found; install FFmpeg on the media worker",
            )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error=f"ffmpeg timed out after {timeout}s")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                success=False,
                error=f"ffmpeg failed with exit code {process.returncode}: {detail[:1000]}",
            )
        if not plan.output_path.is_file():
            return ToolResult(
                success=False,
                error="ffmpeg completed without creating the output file",
            )
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "status": "success",
                    "output_path": str(plan.output_path),
                    "media_type": "video/mp4",
                    "duration_ms": parsed_timeline.duration_ms,
                    "width": parsed_timeline.width,
                    "height": parsed_timeline.height,
                    "fps": parsed_timeline.fps,
                    "size_bytes": plan.output_path.stat().st_size,
                },
                ensure_ascii=False,
            ),
        )
