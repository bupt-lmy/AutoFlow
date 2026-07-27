"""Local FFmpeg acceptance test for the no-video-credit generation backend."""

from __future__ import annotations

import asyncio
import json

from axonflow.tools.storyboard_video import StoryboardMotionRenderTool


async def _make_frame(path, color: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-n",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=1280x720",
        "-frames:v",
        "1",
        str(path),
    )
    await process.communicate()
    assert process.returncode == 0


async def test_storyboard_images_render_with_motion_and_transitions(tmp_path) -> None:
    images = [tmp_path / f"shot-{index}.jpg" for index in range(3)]
    for path, color in zip(images, ["red", "green", "blue"], strict=True):
        await _make_frame(path, color)

    result = await StoryboardMotionRenderTool(tmp_path / "output").execute(
        image_paths=[str(path) for path in images],
        shot_duration_seconds=1,
        transition_seconds=0.2,
        width=640,
        height=360,
        fps=24,
    )

    assert result.success is True, result.error
    output = json.loads(result.output or "{}")
    assert output["generation_backend"] == "storyboard"
    assert output["shot_count"] == 3
    assert output["video_codec"] == "h264"
    assert output["audio_codec"] is None
    assert output["width"] == 640
    assert output["height"] == 360
    assert 2500 <= output["duration_ms"] <= 2700
