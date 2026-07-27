"""FFmpeg-backed acceptance tests for silent-action scene features."""

from __future__ import annotations

import asyncio
import json

from axonflow.tools.video_features import VideoActivityScanTool, VideoSceneFeatureTool


async def _silent_static_and_motion_source(path) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360:r=30:d=2",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=640x360:r=30:d=2",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()


async def test_silent_motion_scores_above_static_scene(tmp_path) -> None:
    source = tmp_path / "silent-motion.mp4"
    await _silent_static_and_motion_source(source)
    result = await VideoSceneFeatureTool(tmp_path / "features").execute(
        source_path=str(source),
        scenes=[
            {"id": "scene-static", "start_ms": 0, "end_ms": 2000, "duration_ms": 2000},
            {"id": "scene-motion", "start_ms": 2000, "end_ms": 4000, "duration_ms": 2000},
        ],
        samples_per_scene=5,
        analysis_fps=4,
    )

    assert result.success is True, result.error
    output = json.loads(result.output or "{}")
    static, motion = output["scenes"]
    assert len(static["sample_frames"]) == 5
    assert 5 <= len(motion["sample_frames"]) <= 12
    assert motion["sampling"]["strategy"] == "event_driven_adaptive"
    assert any(
        frame["role"] == "primary_event_peak" for frame in motion["sample_frames"]
    )
    assert all("event_activity" in row for row in motion["feature_samples"])
    assert static["features"]["audio_energy"] == 0
    assert motion["features"]["audio_energy"] == 0
    assert static["features"]["freeze_ratio"] >= 0.8
    assert static["features"]["black_ratio"] >= 0.8
    assert motion["features"]["motion_intensity"] > static["features"]["motion_intensity"]
    assert output["feature_summary"]["motion_ranked_scene_ids"][0] == "scene-motion"


async def test_fine_scan_produces_high_frequency_event_curve(tmp_path) -> None:
    source = tmp_path / "fine-scan.mp4"
    await _silent_static_and_motion_source(source)

    result = await VideoActivityScanTool().execute(
        source_path=str(source),
        intervals=[
            {
                "scene_id": "scene-motion",
                "start_ms": 2000,
                "end_ms": 4000,
            }
        ],
        analysis_fps=12,
    )

    assert result.success is True, result.error
    output = json.loads(result.output or "{}")
    rows = output["intervals"][0]["feature_samples"]
    assert len(rows) >= 20
    assert output["analysis_fps"] == 12
    assert max(row["event_activity"] for row in rows) > 0
    spacings = [
        right["timestamp_ms"] - left["timestamp_ms"]
        for left, right in zip(rows, rows[1:], strict=False)
    ]
    assert max(spacings) <= 100
