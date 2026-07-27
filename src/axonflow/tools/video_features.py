"""Event-driven multi-frame sampling and deterministic audiovisual analysis."""

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
    """Coarsely scan the full source, then sample each shot around detected events."""

    name = "video_scene_features"
    description = "低成本扫描全片，并按动作、音频起势和视觉新颖度为镜头自适应抽取多帧"
    parameters = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "scenes": {"type": "array", "items": {"type": "object"}},
            "samples_per_scene": {"type": "integer", "default": 5},
            "max_samples_per_scene": {"type": "integer", "default": 12},
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
        max_samples_per_scene: int = 12,
        analysis_fps: float = 4,
        timeout: int = 3600,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(source_path)
        if not source.is_file():
            return ToolResult(success=False, error=f"Video source not found: {source}")
        if not isinstance(scenes, list) or not scenes:
            return ToolResult(success=False, error="Scene feature analysis requires scenes")
        if not 4 <= samples_per_scene <= 12:
            return ToolResult(success=False, error="samples_per_scene must be between 4 and 12")
        if not samples_per_scene <= max_samples_per_scene <= 12:
            return ToolResult(
                success=False,
                error="max_samples_per_scene must be between samples_per_scene and 12",
            )
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
            analyses = [
                self._scene_analysis(scene, visual, audio) for scene in normalized
            ]
            sampling_plans = [
                self._sampling_plan(
                    analysis,
                    minimum=samples_per_scene,
                    maximum=max_samples_per_scene,
                )
                for analysis in analyses
            ]
            run_dir = self.output_dir / f"features-{uuid.uuid4().hex[:12]}"
            run_dir.mkdir(parents=True, exist_ok=False)
            frame_sets = await asyncio.wait_for(
                self._extract_scene_frames(source, normalized, sampling_plans, run_dir),
                timeout=timeout,
            )
        except TimeoutError:
            return ToolResult(success=False, error="Scene feature analysis timed out")
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))

        enhanced = [
            {
                **analysis,
                "sample_frames": frame_sets[index],
                "sampling": {
                    "strategy": "event_driven_adaptive",
                    "sample_count": len(frame_sets[index]),
                    "planned_sample_count": len(sampling_plans[index]),
                    "roles": [frame["role"] for frame in frame_sets[index]],
                },
            }
            for index, analysis in enumerate(analyses)
        ]
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "source_path": str(source),
                    "source_fps": source_metadata.get("fps"),
                    "analysis_fps": analysis_fps,
                    "sampling_strategy": "event_driven_adaptive_4_to_12",
                    "samples_per_scene": samples_per_scene,
                    "max_samples_per_scene": max_samples_per_scene,
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
            normalized.append(
                {
                    **scene,
                    "start_ms": start,
                    "end_ms": end,
                    "duration_ms": end - start,
                }
            )
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
        self,
        source: Path,
        analysis_fps: float,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, float]]:
        seek = ["-ss", f"{start_ms / 1000:.3f}"] if start_ms is not None else []
        duration = (
            ["-t", f"{(end_ms - start_ms) / 1000:.3f}"]
            if start_ms is not None and end_ms is not None
            else []
        )
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-hide_banner",
            "-v",
            "info",
            *seek,
            "-i",
            str(source),
            *duration,
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
            timestamp_offset_ms=start_ms or 0,
        )

    async def _audio_series(
        self,
        source: Path,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, float]]:
        seek = ["-ss", f"{start_ms / 1000:.3f}"] if start_ms is not None else []
        duration = (
            ["-t", f"{(end_ms - start_ms) / 1000:.3f}"]
            if start_ms is not None and end_ms is not None
            else []
        )
        process = await asyncio.create_subprocess_exec(
            _binary(FFMPEG_FULL, "ffmpeg"),
            "-hide_banner",
            "-v",
            "info",
            *seek,
            "-i",
            str(source),
            *duration,
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
            timestamp_offset_ms=start_ms or 0,
        )

    @staticmethod
    def _parse_metadata(
        text: str,
        keys: dict[str, str],
        *,
        timestamp_offset_ms: int = 0,
    ) -> list[dict[str, float]]:
        rows: list[dict[str, float]] = []
        current: dict[str, float] | None = None
        for line in text.splitlines():
            frame_match = re.search(r"frame:\d+.*pts_time:([-+0-9.eE]+)", line)
            if frame_match:
                if current is not None:
                    rows.append(current)
                timestamp = round(float(frame_match.group(1)) * 1000)
                current = {"timestamp_ms": timestamp + timestamp_offset_ms}
                continue
            if current is None:
                continue
            for output_key, metadata_key in keys.items():
                value_match = re.search(
                    rf"{re.escape(metadata_key)}=(-?inf|[-+0-9.eE]+)", line
                )
                if value_match:
                    raw = value_match.group(1)
                    current[output_key] = -120.0 if raw == "-inf" else float(raw)
        if current is not None:
            rows.append(current)
        return [row for row in rows if any(key in row for key in keys)]

    @classmethod
    def _event_samples(
        cls,
        scene: dict[str, Any],
        visual: list[dict[str, float]],
        audio: list[dict[str, float]],
    ) -> list[dict[str, float]]:
        start = int(scene["start_ms"])
        end = int(scene["end_ms"])
        visual_rows = [row for row in visual if start <= row["timestamp_ms"] < end]
        audio_rows = [row for row in audio if start <= row["timestamp_ms"] < end]
        previous_luma: float | None = None
        previous_energy = 0.0
        samples: list[dict[str, float]] = []
        for row in visual_rows:
            timestamp = row["timestamp_ms"]
            nearest = min(
                audio_rows,
                key=lambda value: abs(value["timestamp_ms"] - timestamp),
                default={},
            )
            rms_db = float(nearest.get("audio_rms_db", -120.0))
            energy = _clip((rms_db + 60.0) / 60.0)
            audio_onset = _clip((energy - previous_energy) / 0.35)
            luma = float(row.get("luma", 0.0))
            visual_novelty = (
                _clip(abs(luma - previous_luma) / 32.0)
                if previous_luma is not None
                else 0.0
            )
            motion = _clip(float(row.get("frame_difference", 0.0)) / 24.0)
            raw_activity = motion * 0.55 + audio_onset * 0.25 + visual_novelty * 0.20
            black_probability = _clip((24.0 - luma) / 24.0)
            quality_multiplier = 1.0 - black_probability * 0.75
            samples.append(
                {
                    **row,
                    "audio_rms_db": rms_db,
                    "audio_energy": round(energy, 4),
                    "audio_onset": round(audio_onset, 4),
                    "visual_novelty": round(visual_novelty, 4),
                    "motion_change": round(motion, 4),
                    "cut_strength": round(motion, 4),
                    "event_activity": round(raw_activity * quality_multiplier, 4),
                }
            )
            previous_luma = luma
            previous_energy = energy
        return samples

    @classmethod
    def _scene_analysis(
        cls,
        scene: dict[str, Any],
        visual: list[dict[str, float]],
        audio: list[dict[str, float]],
    ) -> dict[str, Any]:
        start = int(scene["start_ms"])
        end = int(scene["end_ms"])
        all_visual_rows = [row for row in visual if start <= row["timestamp_ms"] < end]
        interior_rows = [
            row for row in all_visual_rows if row["timestamp_ms"] >= start + 200
        ]
        visual_rows = interior_rows or all_visual_rows
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
        feature_samples = cls._event_samples(scene, visual, audio)
        event_values = [row["event_activity"] for row in feature_samples]
        return {
            **scene,
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
                "event_activity_mean": round(
                    statistics.fmean(event_values) if event_values else 0.0, 4
                ),
                "event_activity_p95": round(_percentile(event_values, 0.95), 4),
            },
        }

    @classmethod
    def _sampling_plan(
        cls,
        scene: dict[str, Any],
        *,
        minimum: int,
        maximum: int,
    ) -> list[dict[str, Any]]:
        start = int(scene["start_ms"])
        end = int(scene["end_ms"])
        duration = end - start
        samples = scene.get("feature_samples", [])
        if not isinstance(samples, list):
            samples = []
        peaks = cls._event_peaks(samples, minimum_spacing_ms=450)
        strong_peaks = [row for row in peaks if float(row.get("event_activity", 0)) >= 0.16]
        duration_bonus = int(duration >= 6_000) + int(duration >= 12_000)
        budget = min(maximum, minimum + duration_bonus + min(3, len(strong_peaks)))
        margin = min(100, max(1, duration // 10))
        candidates: dict[int, dict[str, Any]] = {}

        def add(timestamp: int, role: str, activity: float = 0.0) -> None:
            bounded = max(start, min(end - 1, timestamp))
            existing = candidates.get(bounded)
            if existing is None or activity > float(existing.get("event_activity", 0)):
                candidates[bounded] = {
                    "timestamp_ms": bounded,
                    "role": role,
                    "event_activity": round(activity, 4),
                }

        add(start + margin, "shot_start")
        add(end - margin, "shot_end")
        add((start + end) // 2, "shot_midpoint")
        for index, peak in enumerate(peaks[:3]):
            timestamp = int(peak["timestamp_ms"])
            activity = float(peak.get("event_activity", 0.0))
            role = "primary_event_peak" if index == 0 else f"secondary_event_peak_{index}"
            add(timestamp, role, activity)
            if index == 0:
                add(timestamp - 500, "event_lead_in", activity * 0.9)
                add(timestamp + 500, "event_follow_through", activity * 0.9)
        uniform_count = max(budget, minimum)
        for index in range(uniform_count):
            timestamp = start + round((index + 0.5) * duration / uniform_count)
            add(timestamp, "coverage", 0.0)

        priority = {
            "primary_event_peak": 0,
            "event_lead_in": 1,
            "event_follow_through": 2,
            "shot_start": 3,
            "shot_end": 4,
            "shot_midpoint": 5,
            "secondary_event_peak_1": 6,
            "secondary_event_peak_2": 7,
            "coverage": 8,
        }
        selected = sorted(
            candidates.values(),
            key=lambda item: (
                priority.get(str(item["role"]), 9),
                -float(item["event_activity"]),
                int(item["timestamp_ms"]),
            ),
        )[:budget]
        return sorted(selected, key=lambda item: int(item["timestamp_ms"]))

    @staticmethod
    def _event_peaks(
        rows: list[dict[str, Any]], *, minimum_spacing_ms: int
    ) -> list[dict[str, Any]]:
        local: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            activity = float(row.get("event_activity", 0.0))
            left = float(rows[index - 1].get("event_activity", 0.0)) if index else -1.0
            right = (
                float(rows[index + 1].get("event_activity", 0.0))
                if index + 1 < len(rows)
                else -1.0
            )
            if activity >= left and activity >= right:
                local.append(row)
        selected: list[dict[str, Any]] = []
        for row in sorted(
            local, key=lambda item: float(item.get("event_activity", 0.0)), reverse=True
        ):
            timestamp = int(row.get("timestamp_ms", 0))
            if all(
                abs(timestamp - int(existing.get("timestamp_ms", 0))) >= minimum_spacing_ms
                for existing in selected
            ):
                selected.append(row)
        return selected

    async def _extract_scene_frames(
        self,
        source: Path,
        scenes: list[dict[str, Any]],
        sampling_plans: list[list[dict[str, Any]]],
        run_dir: Path,
    ) -> list[list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(8)

        async def extract_frame(
            scene_dir: Path,
            offset: int,
            sample: dict[str, Any],
        ) -> dict[str, Any] | None:
            output = scene_dir / f"sample-{offset + 1:02d}.jpg"
            async with semaphore:
                process = await asyncio.create_subprocess_exec(
                    _binary(FFMPEG_FULL, "ffmpeg"),
                    "-hide_banner",
                    "-v",
                    "error",
                    "-ss",
                    f"{int(sample['timestamp_ms']) / 1000:.3f}",
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
                _stdout, _stderr = await process.communicate()
            if process.returncode != 0 or not output.is_file():
                return None
            return {**sample, "path": str(output)}

        async def extract_scene(
            index: int, scene: dict[str, Any], plan: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            scene_dir = run_dir / str(scene.get("id") or f"scene-{index + 1:03d}")
            scene_dir.mkdir(parents=True, exist_ok=False)
            frames = await asyncio.gather(
                *(extract_frame(scene_dir, offset, sample) for offset, sample in enumerate(plan))
            )
            usable = [frame for frame in frames if frame is not None]
            if not usable:
                raise RuntimeError(
                    f"Event-driven frame extraction failed for {scene.get('id')}"
                )
            return sorted(usable, key=lambda item: int(item["timestamp_ms"]))

        return await asyncio.gather(
            *(
                extract_scene(index, scene, sampling_plans[index])
                for index, scene in enumerate(scenes)
            )
        )

    @staticmethod
    def _summary(scenes: list[dict[str, Any]]) -> dict[str, Any]:
        ranked = sorted(
            scenes,
            key=lambda scene: scene["features"]["event_activity_p95"],
            reverse=True,
        )
        counts = [len(scene.get("sample_frames", [])) for scene in scenes]
        return {
            "scene_count": len(scenes),
            "event_ranked_scene_ids": [scene.get("id") for scene in ranked],
            "motion_ranked_scene_ids": [
                scene.get("id")
                for scene in sorted(
                    scenes,
                    key=lambda item: item["features"]["motion_intensity"],
                    reverse=True,
                )
            ],
            "sample_count_min": min(counts, default=0),
            "sample_count_max": max(counts, default=0),
            "silent_scene_count": sum(
                scene["features"]["audio_energy"] == 0 for scene in scenes
            ),
            "frozen_scene_count": sum(
                scene["features"]["freeze_ratio"] >= 0.8 for scene in scenes
            ),
        }


class VideoActivityScanTool(VideoSceneFeatureTool):
    """Rescan only selected intervals at a higher temporal frequency."""

    name = "video_activity_scan"
    description = "对候选镜头以高帧率精扫运动、音频起势和视觉新颖度，供帧级边界精修"
    parameters = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "intervals": {"type": "array", "items": {"type": "object"}},
            "analysis_fps": {"type": "number", "default": 12},
        },
        "required": ["source_path", "intervals"],
    }

    async def execute(
        self,
        source_path: str,
        intervals: list[dict[str, Any]],
        analysis_fps: float = 12,
        timeout: int = 3600,
        **_kwargs: Any,
    ) -> ToolResult:
        source = TimelineCompiler.local_path(source_path)
        if not source.is_file():
            return ToolResult(success=False, error=f"Video source not found: {source}")
        if not isinstance(intervals, list) or not intervals:
            return ToolResult(success=False, error="Fine activity scan requires intervals")
        if not 8 <= analysis_fps <= 30:
            return ToolResult(success=False, error="analysis_fps must be between 8 and 30")
        try:
            normalized = self._normalize_scenes(intervals)
        except ValueError as exc:
            return ToolResult(success=False, error=f"Invalid intervals: {exc}")
        probe = await MediaProbeTool().execute(path=str(source))
        if not probe.success:
            return probe
        metadata = json.loads(probe.output or "{}")
        has_audio = bool(metadata.get("audio_codec"))
        semaphore = asyncio.Semaphore(3)

        async def scan(scene: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                visual_task = asyncio.create_task(
                    self._visual_series(
                        source,
                        analysis_fps,
                        start_ms=int(scene["start_ms"]),
                        end_ms=int(scene["end_ms"]),
                    )
                )
                audio_task = (
                    asyncio.create_task(
                        self._audio_series(
                            source,
                            start_ms=int(scene["start_ms"]),
                            end_ms=int(scene["end_ms"]),
                        )
                    )
                    if has_audio
                    else None
                )
                visual = await visual_task
                audio = await audio_task if audio_task is not None else []
            return {
                "id": scene.get("id") or scene.get("scene_id"),
                "scene_id": scene.get("scene_id") or scene.get("id"),
                "start_ms": scene["start_ms"],
                "end_ms": scene["end_ms"],
                "feature_samples": self._event_samples(scene, visual, audio),
            }

        try:
            scanned = await asyncio.wait_for(
                asyncio.gather(*(scan(scene) for scene in normalized)),
                timeout=timeout,
            )
        except TimeoutError:
            return ToolResult(success=False, error="Fine activity scan timed out")
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "source_path": str(source),
                    "source_fps": metadata.get("fps"),
                    "analysis_fps": analysis_fps,
                    "intervals": scanned,
                },
                ensure_ascii=False,
            ),
        )
