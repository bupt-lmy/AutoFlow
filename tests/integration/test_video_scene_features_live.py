"""FFmpeg-backed acceptance tests for silent-action scene features."""

from __future__ import annotations

import asyncio
import json

from axonflow.tools.video_features import VideoSceneFeatureTool


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
    assert len(motion["sample_frames"]) == 5
    assert static["features"]["audio_energy"] == 0
    assert motion["features"]["audio_energy"] == 0
    assert static["features"]["freeze_ratio"] >= 0.8
    assert static["features"]["black_ratio"] >= 0.8
    assert motion["features"]["motion_intensity"] > static["features"]["motion_intensity"]
    assert output["feature_summary"]["motion_ranked_scene_ids"][0] == "scene-motion"
