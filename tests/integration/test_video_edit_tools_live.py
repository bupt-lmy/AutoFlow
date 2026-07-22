"""Live local validation for scene detection and real-motion highlight rendering."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from axonflow.tools.video_edit import (
    FFMPEG_FULL,
    HardSubtitleBurnTool,
    HighlightRenderTool,
    VideoIngestTool,
    VideoSceneDetectTool,
)

pytestmark = pytest.mark.skipif(not FFMPEG_FULL.is_file(), reason="ffmpeg-full is not installed")


def _source_video(path: Path) -> None:
    subprocess.run(
        [
            str(FFMPEG_FULL),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "smptebars=s=320x240:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "3:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


async def test_ingest_scene_detect_and_highlight_render_preserve_motion(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    _source_video(source)

    ingest = await VideoIngestTool(tmp_path / "imports").execute(str(source))
    assert ingest.success is True
    ingested = json.loads(ingest.output or "{}")
    assert ingested["probe"]["duration_ms"] == 6000

    detected_result = await VideoSceneDetectTool(tmp_path / "keyframes").execute(
        ingested["source_path"], threshold=8
    )
    assert detected_result.success is True, detected_result.error
    detected = json.loads(detected_result.output or "{}")
    assert len(detected["scenes"]) == 3
    assert [scene["start_ms"] for scene in detected["scenes"]] == [0, 2000, 4000]
    assert all(Path(scene["keyframe_path"]).is_file() for scene in detected["scenes"])

    selected = [detected["scenes"][0], detected["scenes"][2]]
    rendered_result = await HighlightRenderTool(tmp_path / "highlights").execute(
        source_path=str(source), clips=selected, width=640, height=360, fps=25
    )
    assert rendered_result.success is True, rendered_result.error
    rendered = json.loads(rendered_result.output or "{}")
    assert rendered["duration_ms"] == 4000
    assert rendered["video_codec"] == "h264"
    assert rendered["audio_codec"] == "aac"
    assert Path(rendered["output_path"]).stat().st_size > 10_000
    print(f"HIGHLIGHT_TEST_ARTIFACT={rendered['output_path']}")


async def test_highlight_render_honors_frame_aligned_subscene_boundaries(tmp_path) -> None:
    source = tmp_path / "source-precise.mp4"
    _source_video(source)
    result = await HighlightRenderTool(tmp_path / "precise").execute(
        source_path=str(source),
        clips=[{"start_ms": 1200, "end_ms": 2800}],
        width=640,
        height=360,
        fps=25,
    )

    assert result.success is True, result.error
    rendered = json.loads(result.output or "{}")
    assert rendered["duration_ms"] == 1600
    assert rendered["selected_clips"] == [{"start_ms": 1200, "end_ms": 2800}]


async def test_hard_subtitle_burn_produces_delivery_video(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    subtitle = tmp_path / "subtitle.srt"
    _source_video(source)
    subtitle.write_text(
        "1\n00:00:00,200 --> 00:00:02,000\n动作画面字幕已烧录\n",
        encoding="utf-8",
    )

    result = await HardSubtitleBurnTool(tmp_path / "final").execute(
        video_path=str(source), subtitle_path=str(subtitle)
    )

    assert result.success is True, result.error
    rendered = json.loads(result.output or "{}")
    assert rendered["subtitles_burned"] is True
    assert rendered["duration_ms"] == 6000
    assert rendered["video_codec"] == "h264"
    assert rendered["audio_codec"] == "aac"
    assert Path(rendered["output_path"]).stat().st_size > 10_000
