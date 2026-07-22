"""Deterministic image, narration and music composition through FFmpeg."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from axonflow.media.render import TimelineCompiler
from axonflow.tools.base import Tool, ToolResult


class MediaComposeTool(Tool):
    """Create a delivery-ready MP4 from one image and two audio tracks."""

    name = "media_compose"
    description = "将图片、中文旁白和背景音乐合成为带缓慢推镜、混音和响度标准化的 MP4"
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "narration_path": {"type": "string"},
            "music_path": {"type": "string"},
            "duration_seconds": {"type": "number", "default": 12},
            "output_name": {"type": "string"},
        },
        "required": ["image_path", "narration_path", "music_path"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/composed") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        image_path: str,
        narration_path: str,
        music_path: str,
        subtitle_path: str | None = None,
        duration_seconds: float = 12,
        output_name: str | None = None,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        music_volume: float = 0.16,
        narration_volume: float = 1.0,
        narration_delay_ms: int = 500,
        timeout: int = 600,
        **_kwargs: Any,
    ) -> ToolResult:
        try:
            inputs = [
                TimelineCompiler.local_path(path)
                for path in (image_path, narration_path, music_path)
            ]
            subtitle = TimelineCompiler.local_path(subtitle_path) if subtitle_path else None
            for path in inputs:
                if not path.is_file():
                    raise ValueError(f"Input media file not found: {path}")
            if subtitle is not None and not subtitle.is_file():
                raise ValueError(f"Input subtitle file not found: {subtitle}")
            if not 3 <= duration_seconds <= 300:
                raise ValueError("duration_seconds must be between 3 and 300")
            if width <= 0 or height <= 0 or width % 2 or height % 2:
                raise ValueError("width and height must be positive even integers")
            if not 1 <= fps <= 60:
                raise ValueError("fps must be between 1 and 60")
            if not 0 <= music_volume <= 1 or not 0 <= narration_volume <= 4:
                raise ValueError("invalid audio volume")
            output_path = self._output_path(output_name)
            arguments = self.build_arguments(
                *inputs,
                output_path,
                duration_seconds=duration_seconds,
                width=width,
                height=height,
                fps=fps,
                music_volume=music_volume,
                narration_volume=narration_volume,
                narration_delay_ms=narration_delay_ms,
                subtitle=subtitle,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=f"Invalid composition request: {exc}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ToolResult(success=False, error="ffmpeg executable was not found")
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return ToolResult(success=False, error=f"ffmpeg composition timed out after {timeout}s")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"ffmpeg composition failed: {detail[:1200]}")
        if not output_path.is_file():
            return ToolResult(success=False, error="ffmpeg did not create the composed video")
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output_path),
                    "media_type": "video/mp4",
                    "duration_ms": round(duration_seconds * 1000),
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "size_bytes": output_path.stat().st_size,
                    "has_subtitles": subtitle is not None,
                },
                ensure_ascii=False,
            ),
        )

    def _output_path(self, output_name: str | None) -> Path:
        name = output_name or f"composed-video-{uuid.uuid4().hex[:12]}.mp4"
        candidate = Path(name)
        if candidate.name != name or name in {".", ".."}:
            raise ValueError("output_name must be a filename without directories")
        output = (self.output_dir / f"{candidate.stem}.mp4").resolve()
        if output.parent != self.output_dir:
            raise ValueError("output path escapes the composition directory")
        if output.exists():
            raise ValueError(f"output already exists: {output.name}")
        return output

    @staticmethod
    def build_arguments(
        image: Path,
        narration: Path,
        music: Path,
        output: Path,
        *,
        duration_seconds: float,
        width: int,
        height: int,
        fps: int,
        music_volume: float,
        narration_volume: float,
        narration_delay_ms: int,
        subtitle: Path | None = None,
    ) -> tuple[str, ...]:
        duration = f"{duration_seconds:g}"
        fade_out_start = max(duration_seconds - 2, 0)
        video_filter = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            "zoompan=z='min(zoom+0.0008,1.08)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={width}x{height}:fps={fps},format=yuv420p[v]"
        )
        voice_filter = (
            f"[1:a]aresample=48000,adelay={narration_delay_ms}:all=1,"
            f"volume={narration_volume:g},apad,atrim=duration={duration}[voice]"
        )
        music_filter = (
            f"[2:a]aresample=48000,volume={music_volume:g},atrim=duration={duration},"
            f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start:g}:d=2[music]"
        )
        mix_filter = (
            "[voice][music]amix=inputs=2:duration=longest:dropout_transition=2,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[a]"
        )
        arguments = [
            "ffmpeg",
            "-n",
            "-v",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(image),
            "-i",
            str(narration),
            "-stream_loop",
            "-1",
            "-i",
            str(music),
        ]
        if subtitle is not None:
            arguments.extend(["-i", str(subtitle)])
        arguments.extend(
            [
                "-filter_complex",
                ";".join((video_filter, voice_filter, music_filter, mix_filter)),
                "-map",
                "[v]",
                "-map",
                "[a]",
            ]
        )
        if subtitle is not None:
            arguments.extend(["-map", "3:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi"])
        arguments.extend(
            [
                "-t",
                duration,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "192k",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        return tuple(arguments)
