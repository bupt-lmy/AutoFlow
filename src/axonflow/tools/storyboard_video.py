"""Turn generated storyboard stills into a visibly moving video clip."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from pathlib import Path
from typing import Any

from axonflow.media.render import TimelineCompiler
from axonflow.tools.base import Tool, ToolResult
from axonflow.tools.media_probe import MediaProbeTool
from axonflow.tools.video_edit import FFMPEG_FULL, _binary


class StoryboardMotionRenderTool(Tool):
    """Animate two or more generated keyframes with camera motion and transitions."""

    name = "storyboard_motion_render"
    description = "将多张 AI 分镜图通过推拉摇移和交叉淡化合成为动态 H.264 视频"
    parameters = {
        "type": "object",
        "properties": {
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 8,
            },
            "shot_duration_seconds": {"type": "number", "default": 2.0},
            "transition_seconds": {"type": "number", "default": 0.4},
            "width": {"type": "integer", "default": 1920},
            "height": {"type": "integer", "default": 1080},
            "fps": {"type": "integer", "default": 30},
            "output_name": {"type": "string"},
        },
        "required": ["image_paths"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/storyboards") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        image_paths: list[str],
        shot_duration_seconds: float = 2.0,
        transition_seconds: float = 0.4,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        output_name: str | None = None,
        timeout: int = 1800,
        **_kwargs: Any,
    ) -> ToolResult:
        if not isinstance(image_paths, list) or not 2 <= len(image_paths) <= 8:
            return ToolResult(success=False, error="image_paths must contain 2 to 8 images")
        if not 0.75 <= shot_duration_seconds <= 10:
            return ToolResult(
                success=False, error="shot_duration_seconds must be between 0.75 and 10"
            )
        if not 0 <= transition_seconds < shot_duration_seconds:
            return ToolResult(
                success=False,
                error="transition_seconds must be non-negative and shorter than a shot",
            )
        if not 320 <= width <= 3840 or not 240 <= height <= 2160:
            return ToolResult(success=False, error="output dimensions are outside supported bounds")
        if not 12 <= fps <= 60:
            return ToolResult(success=False, error="fps must be between 12 and 60")

        try:
            sources = [TimelineCompiler.local_path(value) for value in image_paths]
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        missing = [str(path) for path in sources if not path.is_file()]
        if missing:
            return ToolResult(success=False, error=f"Storyboard image not found: {missing[0]}")

        name = output_name or f"storyboard-motion-{uuid.uuid4().hex[:12]}.mp4"
        if Path(name).name != name:
            return ToolResult(success=False, error="output_name must not contain directories")
        output = (self.output_dir / f"{Path(name).stem}.mp4").resolve()
        if output.parent != self.output_dir or output.exists():
            return ToolResult(success=False, error="unsafe or existing storyboard output path")
        output.parent.mkdir(parents=True, exist_ok=True)

        arguments: list[str] = [_binary(FFMPEG_FULL, "ffmpeg"), "-n", "-v", "error"]
        for source in sources:
            arguments.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    str(fps),
                    "-t",
                    f"{shot_duration_seconds:.3f}",
                    "-i",
                    str(source),
                ]
            )

        frames = max(1, math.ceil(shot_duration_seconds * fps))
        filters: list[str] = []
        for index in range(len(sources)):
            # Alternate the focal point so consecutive stills feel like distinct camera shots.
            x = "iw/2-(iw/zoom/2)" if index % 2 == 0 else "iw-iw/zoom"
            y = "ih/2-(ih/zoom/2)" if index % 3 else "ih-ih/zoom"
            filters.append(
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,"
                f"zoompan=z='min(zoom+0.0015,1.10)':x='{x}':y='{y}':"
                f"d={frames}:s={width}x{height}:fps={fps},"
                f"trim=duration={shot_duration_seconds:.3f},setpts=PTS-STARTPTS[v{index}]"
            )

        current = "v0"
        for index in range(1, len(sources)):
            output_label = f"x{index}"
            offset = index * (shot_duration_seconds - transition_seconds)
            filters.append(
                f"[{current}][v{index}]xfade=transition=fade:"
                f"duration={transition_seconds:.3f}:offset={offset:.3f}[{output_label}]"
            )
            current = output_label

        arguments.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{current}]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except FileNotFoundError:
            return ToolResult(success=False, error="ffmpeg executable was not found")
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Storyboard motion rendering timed out")
        if process.returncode != 0 or not output.is_file():
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"Storyboard rendering failed: {detail[:1200]}")

        probe = await MediaProbeTool().execute(path=str(output))
        if not probe.success:
            return ToolResult(success=False, error=f"Rendered storyboard is invalid: {probe.error}")
        metadata = json.loads(probe.output or "{}")
        expected_duration = len(sources) * shot_duration_seconds - (
            len(sources) - 1
        ) * transition_seconds
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output),
                    "media_type": "video/mp4",
                    "generation_backend": "storyboard",
                    "storyboard_source_images": [str(path) for path in sources],
                    "shot_count": len(sources),
                    "shot_duration_seconds": shot_duration_seconds,
                    "transition_seconds": transition_seconds,
                    "expected_duration_seconds": expected_duration,
                    "motion_style": "alternating_ken_burns_with_crossfades",
                    **metadata,
                },
                ensure_ascii=False,
            ),
        )
