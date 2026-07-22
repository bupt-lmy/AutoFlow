"""Tools for source-video ingest, scene analysis and highlight rendering."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from axonflow.media.render import TimelineCompiler
from axonflow.tools.base import Tool, ToolResult
from axonflow.tools.media_probe import MediaProbeTool

FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
FFPROBE_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")
WHISPER_CLI = Path("/opt/homebrew/bin/whisper-cli")


def _binary(preferred: Path, fallback: str) -> str:
    return str(preferred) if preferred.is_file() else fallback


class VideoIngestTool(Tool):
    """Resolve a local video or download one page/direct URL with yt-dlp."""

    name = "video_ingest"
    description = "导入本地视频或使用 yt-dlp 下载用户有权处理的目标链接"
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string"}},
        "required": ["source"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/imports") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(self, source: str, timeout: int = 1800, **_kwargs: Any) -> ToolResult:
        source = source.strip()
        if not source:
            return ToolResult(success=False, error="Video source cannot be empty")
        if not re.match(r"^https?://", source, re.IGNORECASE):
            try:
                path = TimelineCompiler.local_path(source)
            except ValueError as exc:
                return ToolResult(success=False, error=str(exc))
            if not path.is_file():
                return ToolResult(success=False, error=f"Video source not found: {path}")
            return await self._result(path, "local")

        yt_dlp = shutil.which("yt-dlp")
        if yt_dlp is None:
            return ToolResult(success=False, error="yt-dlp is required for URL video sources")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        template = self.output_dir / f"import-{uuid.uuid4().hex[:12]}.%(ext)s"
        arguments = [
            yt_dlp,
            "--no-playlist",
            "--no-progress",
            "--restrict-filenames",
            "-f",
            "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format",
            "mp4",
            "--ffmpeg-location",
            str(FFMPEG_FULL.parent),
            "--print",
            "after_move:filepath",
            "-o",
            str(template),
            source,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Video URL download timed out")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"yt-dlp failed: {detail[:1000]}")
        lines = [line.strip() for line in stdout.decode().splitlines() if line.strip()]
        if not lines:
            return ToolResult(success=False, error="yt-dlp did not return a downloaded file")
        return await self._result(Path(lines[-1]).resolve(), "url")

    @staticmethod
    async def _result(path: Path, source_type: str) -> ToolResult:
        probe = await MediaProbeTool().execute(path=str(path))
        if not probe.success:
            return ToolResult(success=False, error=f"Imported video is invalid: {probe.error}")
        metadata = json.loads(probe.output or "{}")
        if not metadata.get("video_codec"):
            return ToolResult(success=False, error="Imported source has no video stream")
        return ToolResult(
            success=True,
            output=json.dumps(
                {"source_path": str(path), "source_type": source_type, "probe": metadata},
                ensure_ascii=False,
            ),
        )


class VideoSceneDetectTool(Tool):
    """Detect shot boundaries and extract one representative frame per scene."""

    name = "video_scene_detect"
    description = "使用 FFmpeg 场景变化检测切分镜头，并为每个镜头提取中间关键帧"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "threshold": {"type": "number", "default": 8},
        },
        "required": ["path"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/keyframes") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        path: str,
        threshold: float = 8,
        min_scene_ms: int = 500,
        max_scenes: int = 60,
        timeout: int = 1800,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(path)
        if not source.is_file():
            return ToolResult(success=False, error=f"Video source not found: {source}")
        if max_scenes < 2:
            return ToolResult(success=False, error="max_scenes must be at least 2")
        probe = await MediaProbeTool().execute(path=str(source))
        if not probe.success:
            return probe
        metadata = json.loads(probe.output or "{}")
        duration_ms = metadata.get("duration_ms")
        if not isinstance(duration_ms, int) or duration_ms <= 0:
            return ToolResult(success=False, error="Video duration is unavailable")
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-hide_banner",
            "-i",
            str(source),
            "-vf",
            f"scdet=threshold={threshold:g},metadata=print",
            "-an",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Scene detection timed out")
        if process.returncode != 0:
            return ToolResult(success=False, error="FFmpeg scene detection failed")
        text = stderr.decode("utf-8", errors="replace")
        cuts = sorted(
            {
                round(float(match) * 1000)
                for match in re.findall(r"lavfi\.scd\.time[:=]\s*([0-9.]+)", text)
            }
        )
        boundaries = [0, *[cut for cut in cuts if min_scene_ms <= cut < duration_ms], duration_ms]
        all_ranges = [
            (index, start, end)
            for index, (start, end) in enumerate(zip(boundaries, boundaries[1:], strict=False), 1)
            if end - start >= min_scene_ms
        ]
        if len(all_ranges) > max_scenes:
            sample_indexes = {
                round(index * (len(all_ranges) - 1) / (max_scenes - 1))
                for index in range(max_scenes)
            }
            scene_ranges = [all_ranges[index] for index in sorted(sample_indexes)]
        else:
            scene_ranges = all_ranges
        scenes: list[dict[str, Any]] = []
        keyframe_dir = self.output_dir / f"scenes-{uuid.uuid4().hex[:12]}"
        keyframe_dir.mkdir(parents=True, exist_ok=True)
        for original_index, start, end in scene_ranges:
            scene_id = f"scene-{original_index:03d}"
            keyframe = keyframe_dir / f"{scene_id}.jpg"
            extracted = await self._extract_frame(source, (start + end) / 2000, keyframe)
            if not extracted:
                continue
            scenes.append(
                {
                    "id": scene_id,
                    "start_ms": start,
                    "end_ms": end,
                    "duration_ms": end - start,
                    "keyframe_path": str(keyframe),
                }
            )
        if not scenes:
            return ToolResult(success=False, error="No usable video scenes were detected")
        return ToolResult(
            success=True,
            output=json.dumps(
                {"source_path": str(source), "duration_ms": duration_ms, "scenes": scenes},
                ensure_ascii=False,
            ),
        )

    @staticmethod
    async def _extract_frame(source: Path, seconds: float, output: Path) -> bool:
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-v",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(768,iw)':-2",
            "-q:v",
            "2",
            "-y",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        return process.returncode == 0 and output.is_file()


class HighlightRenderTool(Tool):
    """Render selected source ranges while retaining real motion and source audio."""

    name = "highlight_render"
    description = "按选中的源视频时间范围剪切并拼接真实动态画面和原声，可烧录 SRT 字幕"
    parameters = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "clips": {"type": "array", "items": {"type": "object"}},
            "subtitle_path": {"type": "string"},
        },
        "required": ["source_path", "clips"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/highlights") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        source_path: str,
        clips: list[dict[str, Any]],
        subtitle_path: str | None = None,
        output_name: str | None = None,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        timeout: int = 1800,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(source_path)
        if not source.is_file() or not clips:
            return ToolResult(success=False, error="Highlight render requires source and clips")
        probe = await MediaProbeTool().execute(path=str(source))
        if not probe.success:
            return probe
        source_metadata = json.loads(probe.output or "{}")
        has_audio = bool(source_metadata.get("audio_codec"))
        try:
            ranges = self._ranges(clips)
            output = self._output_path(output_name)
            subtitle = TimelineCompiler.local_path(subtitle_path) if subtitle_path else None
            if subtitle is not None and not subtitle.is_file():
                raise ValueError("subtitle file does not exist")
            arguments = self.build_arguments(
                source,
                ranges,
                output,
                has_audio=has_audio,
                subtitle=subtitle,
                width=width,
                height=height,
                fps=fps,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=f"Invalid highlight render request: {exc}")
        output.parent.mkdir(parents=True, exist_ok=True)
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
            return ToolResult(success=False, error="Highlight rendering timed out")
        if process.returncode != 0 or not output.is_file():
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"Highlight rendering failed: {detail[:1200]}")
        result_probe = await MediaProbeTool().execute(path=str(output))
        rendered = json.loads(result_probe.output or "{}") if result_probe.success else {}
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output),
                    "media_type": "video/mp4",
                    "selected_clips": clips,
                    **rendered,
                },
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _ranges(clips: list[dict[str, Any]]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for clip in clips:
            start = int(clip.get("start_ms", -1))
            end = int(clip.get("end_ms", -1))
            if start < 0 or end <= start:
                raise ValueError("each clip requires valid start_ms and end_ms")
            ranges.append((start, end))
        return ranges

    def _output_path(self, output_name: str | None) -> Path:
        name = output_name or f"highlight-{uuid.uuid4().hex[:12]}.mp4"
        candidate = Path(name)
        if candidate.name != name:
            raise ValueError("output_name must not contain directories")
        output = (self.output_dir / f"{candidate.stem}.mp4").resolve()
        if output.parent != self.output_dir or output.exists():
            raise ValueError("unsafe or existing highlight output path")
        return output

    @staticmethod
    def build_arguments(
        source: Path,
        ranges: list[tuple[int, int]],
        output: Path,
        *,
        has_audio: bool,
        subtitle: Path | None,
        width: int,
        height: int,
        fps: int,
    ) -> tuple[str, ...]:
        arguments: list[str] = [
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-n",
            "-v",
            "error",
            "-i",
            str(source),
        ]
        filters: list[str] = []
        concat_inputs: list[str] = []
        if len(ranges) > 1:
            video_outputs = "".join(f"[vsrc{index}]" for index in range(len(ranges)))
            filters.append(f"[0:v:0]split={len(ranges)}{video_outputs}")
            if has_audio:
                audio_outputs = "".join(f"[asrc{index}]" for index in range(len(ranges)))
                filters.append(f"[0:a:0]asplit={len(ranges)}{audio_outputs}")
        for index, (start, end) in enumerate(ranges):
            duration = (end - start) / 1000
            video_source = f"vsrc{index}" if len(ranges) > 1 else "0:v:0"
            filters.append(
                f"[{video_source}]trim=start={start / 1000:.3f}:end={end / 1000:.3f},"
                f"setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}[v{index}]"
            )
            if has_audio:
                audio_source = f"asrc{index}" if len(ranges) > 1 else "0:a:0"
                filters.append(
                    f"[{audio_source}]atrim=start={start / 1000:.3f}:end={end / 1000:.3f},"
                    "asetpts=PTS-STARTPTS,aresample=48000,"
                    f"aformat=channel_layouts=stereo[a{index}]"
                )
            else:
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:g}[a{index}]")
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
        video_output = "[vbase]" if subtitle is not None else "[vout]"
        filters.append(
            f"{''.join(concat_inputs)}concat=n={len(ranges)}:v=1:a=1{video_output}[aout]"
        )
        if subtitle is not None:
            escaped = str(subtitle).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            filters.append(
                f"[vbase]subtitles=filename='{escaped}':"
                "force_style='FontName=PingFang SC,FontSize=20,Outline=2,Shadow=1,"
                "MarginV=48,Alignment=2'[vout]"
            )
        arguments.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
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


class VideoTranscribeTool(Tool):
    """Transcribe an edited video to timestamped SRT with local whisper.cpp."""

    name = "video_transcribe"
    description = "使用本地 whisper.cpp 对剪辑成片转写并生成带时间码的 SRT 字幕"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "language": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(
        self,
        model_path: str | Path = "workspace/models/ggml-small.bin",
        output_dir: str | Path = "workspace/media/transcripts",
    ) -> None:
        self.model_path = Path(model_path).resolve()
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        path: str,
        language: str = "auto",
        timeout: int = 3600,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(path)
        if not source.is_file():
            return ToolResult(success=False, error=f"Video source not found: {source}")
        if not WHISPER_CLI.is_file():
            return ToolResult(success=False, error="whisper-cli is not installed")
        if not self.model_path.is_file():
            return ToolResult(success=False, error=f"Whisper model not found: {self.model_path}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex[:12]
        wav_path = self.output_dir / f"audio-{job_id}.wav"
        output_prefix = self.output_dir / f"transcript-{job_id}"
        srt_path = output_prefix.with_suffix(".srt")
        active_process: asyncio.subprocess.Process | None = None
        stdout = b""
        stderr = b""
        try:
            extract = await asyncio.create_subprocess_exec(
                _binary(FFMPEG_FULL, "ffmpeg"),
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            active_process = extract
            _stdout, stderr = await asyncio.wait_for(extract.communicate(), timeout=timeout)
            if extract.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                return ToolResult(success=False, error=f"Audio extraction failed: {detail[:800]}")
            process = await asyncio.create_subprocess_exec(
                str(WHISPER_CLI),
                "-m",
                str(self.model_path),
                "-f",
                str(wav_path),
                "-l",
                language,
                "-osrt",
                "-of",
                str(output_prefix),
                "-ml",
                "18",
                "-sow",
                "-ng",
                "-np",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            active_process = process
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            if active_process is not None:
                active_process.kill()
                await active_process.wait()
            return ToolResult(success=False, error="Video transcription timed out")
        finally:
            wav_path.unlink(missing_ok=True)
        if active_process is None or active_process.returncode != 0 or not srt_path.is_file():
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"Whisper transcription failed: {detail[:1000]}")
        content = srt_path.read_text(encoding="utf-8").strip()
        if not content:
            return ToolResult(success=False, error="Whisper found no transcribable speech")
        cue_count = len(re.findall(r"(?m)^\d+\s*$", content))
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(srt_path),
                    "media_type": "application/x-subrip",
                    "cue_count": cue_count,
                    "language": language,
                },
                ensure_ascii=False,
            ),
        )


class HardSubtitleBurnTool(Tool):
    """Burn an SRT into real video frames with ffmpeg-full/libass."""

    name = "hard_subtitle_burn"
    description = "使用 ffmpeg-full/libass 将 SRT 中文字幕永久烧录到剪辑视频画面"
    parameters = {
        "type": "object",
        "properties": {"video_path": {"type": "string"}, "subtitle_path": {"type": "string"}},
        "required": ["video_path", "subtitle_path"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/final") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        video_path: str,
        subtitle_path: str,
        output_name: str | None = None,
        timeout: int = 1800,
        **_kwargs: Any,
    ) -> ToolResult:
        video = TimelineCompiler.local_path(video_path)
        subtitle = TimelineCompiler.local_path(subtitle_path)
        if not video.is_file() or not subtitle.is_file():
            return ToolResult(success=False, error="Video and subtitle files must exist")
        name = output_name or f"final-highlight-{uuid.uuid4().hex[:12]}.mp4"
        if Path(name).name != name:
            return ToolResult(success=False, error="output_name must not contain directories")
        output = (self.output_dir / f"{Path(name).stem}.mp4").resolve()
        if output.parent != self.output_dir or output.exists():
            return ToolResult(success=False, error="unsafe or existing subtitle output path")
        output.parent.mkdir(parents=True, exist_ok=True)
        escaped = str(subtitle).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        subtitle_filter = (
            f"subtitles=filename='{escaped}':"
            "force_style='FontName=PingFang SC,FontSize=20,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
            "MarginV=52,Alignment=2'"
        )
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-n",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            subtitle_filter,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Hard subtitle rendering timed out")
        if process.returncode != 0 or not output.is_file():
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                success=False, error=f"Hard subtitle rendering failed: {detail[:1000]}"
            )
        probe = await MediaProbeTool().execute(path=str(output))
        metadata = json.loads(probe.output or "{}") if probe.success else {}
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output),
                    "media_type": "video/mp4",
                    "subtitles_burned": True,
                    "subtitle_path": str(subtitle),
                    **metadata,
                },
                ensure_ascii=False,
            ),
        )
