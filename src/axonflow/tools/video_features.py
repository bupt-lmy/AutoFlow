"""Deterministic multi-frame, motion-proxy and audio feature extraction."""

from __future__ import annotations

import asyncio
import json
import math
import re
import statistics
import uuid
from pathlib import Path
from typing import Any

from axonflow.media.render import TimelineCompiler
from axonflow.tools.base import Tool, ToolResult
from axonflow.tools.media_probe import MediaProbeTool
from axonflow.tools.video_edit import FFMPEG_FULL, _binary


def _clip(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


class VideoSceneFeatureTool(Tool):
    """Analyze every scene using sampled frames and low-rate FFmpeg metadata."""

    name = "video_scene_features"
    description = "为镜头抽取多帧，并计算运动、画面变化、冻结、黑场和音频冲击特征"
    parameters = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "scenes": {"type": "array", "items": {"type": "object"}},
            "samples_per_scene": {"type": "integer", "default": 5},
            "analysis_fps": {"type": "number", "default": 4},
        },
        "required": ["source_path", "scenes"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/scene-features") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        source_path: str,
        scenes: list[dict[str, Any]],
        samples_per_scene: int = 5,
        analysis_fps: float = 4,
        timeout: int = 3600,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(source_path)
        if not source.is_file():
            return ToolResult(success=False, error=f"Video source not found: {source}")
        if not isinstance(scenes, list) or not scenes:
            return ToolResult(success=False, error="Scene feature analysis requires scenes")
        if not 3 <= samples_per_scene <= 12:
            return ToolResult(success=False, error="samples_per_scene must be between 3 and 12")
        if not 1 <= analysis_fps <= 10:
            return ToolResult(success=False, error="analysis_fps must be between 1 and 10")
        try:
            normalized = self._normalize_scenes(scenes)
        except ValueError as exc:
            return ToolResult(success=False, error=f"Invalid scenes: {exc}")

        probe = await MediaProbeTool().execute(path=str(source))
        if not probe.success:
            return probe
        source_metadata = json.loads(probe.output or "{}")
        try:
            visual, audio = await asyncio.wait_for(
                self._analyze_streams(
                    source,
                    analysis_fps=analysis_fps,
                    has_audio=bool(source_metadata.get("audio_codec")),
                ),
                timeout=timeout,
            )
            run_dir = self.output_dir / f"features-{uuid.uuid4().hex[:12]}"
            run_dir.mkdir(parents=True, exist_ok=False)
            frame_sets = await asyncio.wait_for(
                self._extract_scene_frames(
                    source,
                    normalized,
                    run_dir,
                    samples_per_scene=samples_per_scene,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            return ToolResult(success=False, error="Scene feature analysis timed out")
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))

        enhanced = [
            self._scene_result(scene, frame_sets[index], visual, audio)
            for index, scene in enumerate(normalized)
        ]
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "source_path": str(source),
                    "analysis_fps": analysis_fps,
                    "samples_per_scene": samples_per_scene,
                    "scenes": enhanced,
                    "feature_summary": self._summary(enhanced),
                },
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _normalize_scenes(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for scene in scenes:
            if not isinstance(scene, dict):
                raise ValueError("every scene must be an object")
            start = int(scene.get("start_ms", -1))
            end = int(scene.get("end_ms", -1))
            if start < 0 or end <= start:
                raise ValueError("every scene requires a valid start_ms/end_ms range")
            normalized.append({**scene, "start_ms": start, "end_ms": end})
        return normalized

    async def _analyze_streams(
        self, source: Path, *, analysis_fps: float, has_audio: bool
    ) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        visual_task = asyncio.create_task(self._visual_series(source, analysis_fps))
        audio_task = asyncio.create_task(self._audio_series(source)) if has_audio else None
        visual = await visual_task
        audio = await audio_task if audio_task is not None else []
        return visual, audio

    async def _visual_series(
        self, source: Path, analysis_fps: float
    ) -> list[dict[str, float]]:
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(source),
            "-vf",
            f"fps={analysis_fps:g},scale=160:90,signalstats,metadata=print",
            "-an",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Visual feature extraction failed: {detail[-1000:]}")
        return self._parse_metadata(
            stderr.decode("utf-8", errors="replace"),
            {
                "frame_difference": "lavfi.signalstats.YDIF",
                "luma": "lavfi.signalstats.YAVG",
            },
        )

    async def _audio_series(self, source: Path) -> list[dict[str, float]]:
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(source),
            "-af",
            (
                "aresample=8000,asetnsamples=n=4000:p=1,"
                "astats=metadata=1:reset=1,ametadata=print"
            ),
            "-vn",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Audio feature extraction failed: {detail[-1000:]}")
        return self._parse_metadata(
            stderr.decode("utf-8", errors="replace"),
            {
                "audio_peak_db": "lavfi.astats.Overall.Peak_level",
                "audio_rms_db": "lavfi.astats.Overall.RMS_level",
            },
        )

    @staticmethod
    def _parse_metadata(text: str, keys: dict[str, str]) -> list[dict[str, float]]:
        rows: list[dict[str, float]] = []
        current: dict[str, float] | None = None
        for line in text.splitlines():
            frame_match = re.search(r"frame:\d+.*pts_time:([0-9.]+)", line)
            if frame_match:
                if current is not None:
                    rows.append(current)
                current = {"timestamp_ms": round(float(frame_match.group(1)) * 1000)}
                continue
            if current is None:
                continue
            for output_key, metadata_key in keys.items():
                value_match = re.search(rf"{re.escape(metadata_key)}=(-?inf|[-+0-9.eE]+)", line)
                if value_match:
                    raw = value_match.group(1)
                    current[output_key] = -120.0 if raw == "-inf" else float(raw)
        if current is not None:
            rows.append(current)
        return [row for row in rows if any(key in row for key in keys)]

    async def _extract_scene_frames(
        self,
        source: Path,
        scenes: list[dict[str, Any]],
        run_dir: Path,
        *,
        samples_per_scene: int,
    ) -> list[list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(4)

        async def extract(index: int, scene: dict[str, Any]) -> list[dict[str, Any]]:
            async with semaphore:
                scene_dir = run_dir / str(scene.get("id") or f"scene-{index + 1:03d}")
                scene_dir.mkdir(parents=True, exist_ok=False)
                duration = (scene["end_ms"] - scene["start_ms"]) / 1000
                fps = samples_per_scene / duration
                pattern = scene_dir / "sample-%02d.jpg"
                process = await asyncio.create_subprocess_exec(
                    _binary(FFMPEG_FULL, "ffmpeg"),
                    "-hide_banner",
                    "-v",
                    "error",
                    "-ss",
                    f"{scene['start_ms'] / 1000:.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    str(source),
                    "-vf",
                    f"fps={fps:.8f},scale='min(768,iw)':-2",
                    "-frames:v",
                    str(samples_per_scene),
                    "-q:v",
                    "2",
                    str(pattern),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await process.communicate()
                paths = sorted(scene_dir.glob("sample-*.jpg"))
                if process.returncode != 0 or len(paths) < 2:
                    detail = stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(
                        f"Multi-frame extraction failed for {scene.get('id')}: {detail[-500:]}"
                    )
                interval = (scene["end_ms"] - scene["start_ms"]) / len(paths)
                return [
                    {
                        "timestamp_ms": round(scene["start_ms"] + (offset + 0.5) * interval),
                        "path": str(path),
                    }
                    for offset, path in enumerate(paths)
                ]

        return await asyncio.gather(
            *(extract(index, scene) for index, scene in enumerate(scenes))
        )

    @classmethod
    def _scene_result(
        cls,
        scene: dict[str, Any],
        sample_frames: list[dict[str, Any]],
        visual: list[dict[str, float]],
        audio: list[dict[str, float]],
    ) -> dict[str, Any]:
        start = scene["start_ms"]
        end = scene["end_ms"]
        visual_rows = [row for row in visual if start <= row["timestamp_ms"] < end]
        # Ignore a hard-cut spike at the exact scene entrance where possible.
        interior_rows = [row for row in visual_rows if row["timestamp_ms"] >= start + 200]
        if interior_rows:
            visual_rows = interior_rows
        audio_rows = [row for row in audio if start <= row["timestamp_ms"] < end]
        differences = [row.get("frame_difference", 0.0) for row in visual_rows]
        lumas = [row.get("luma", 0.0) for row in visual_rows]
        rms_values = [row.get("audio_rms_db", -120.0) for row in audio_rows]
        peak_values = [row.get("audio_peak_db", -120.0) for row in audio_rows]
        motion_p95 = _percentile(differences, 0.95)
        motion_mean = statistics.fmean(differences) if differences else 0.0
        motion_intensity = _clip(motion_p95 / 24.0)
        visual_change = _clip((motion_mean * 0.4 + motion_p95 * 0.6) / 24.0)
        freeze_ratio = (
            sum(value < 0.75 for value in differences) / len(differences)
            if differences
            else 1.0
        )
        black_ratio = sum(value < 24 for value in lumas) / len(lumas) if lumas else 0.0
        audio_rms_db = statistics.fmean(rms_values) if rms_values else -120.0
        audio_peak_db = max(peak_values, default=-120.0)
        audio_energy = _clip((audio_rms_db + 60.0) / 60.0)
        audio_impact = _clip((audio_peak_db - audio_rms_db) / 30.0) * audio_energy
        feature_samples = [
            {
                **row,
                "audio_rms_db": cls._nearest_audio(row["timestamp_ms"], audio),
            }
            for row in visual_rows
        ]
        return {
            **scene,
            "sample_frames": sample_frames,
            "feature_samples": feature_samples,
            "features": {
                "motion_mean": round(motion_mean, 4),
                "motion_p95": round(motion_p95, 4),
                "motion_intensity": round(motion_intensity, 4),
                "visual_change": round(visual_change, 4),
                "freeze_ratio": round(freeze_ratio, 4),
                "black_ratio": round(black_ratio, 4),
                "audio_rms_db": round(audio_rms_db, 4),
                "audio_peak_db": round(audio_peak_db, 4),
                "audio_energy": round(audio_energy, 4),
                "audio_impact": round(audio_impact, 4),
            },
        }

    @staticmethod
    def _nearest_audio(timestamp_ms: float, audio: list[dict[str, float]]) -> float:
        if not audio:
            return -120.0
        nearest = min(audio, key=lambda row: abs(row["timestamp_ms"] - timestamp_ms))
        return nearest.get("audio_rms_db", -120.0)

    @staticmethod
    def _summary(scenes: list[dict[str, Any]]) -> dict[str, Any]:
        ranked = sorted(
            scenes,
            key=lambda scene: scene["features"]["motion_intensity"],
            reverse=True,
        )
        return {
            "scene_count": len(scenes),
            "motion_ranked_scene_ids": [scene.get("id") for scene in ranked],
            "silent_scene_count": sum(
                scene["features"]["audio_energy"] == 0 for scene in scenes
            ),
            "frozen_scene_count": sum(
                scene["features"]["freeze_ratio"] >= 0.8 for scene in scenes
            ),
        }
