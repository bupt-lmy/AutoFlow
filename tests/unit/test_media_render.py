"""Timeline compiler and controlled FFmpeg renderer tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from axonflow.media.models import Timeline, VideoClip, VideoTrack
from axonflow.media.render import TimelineCompiler, UnsupportedTimelineError
from axonflow.tools.media_render import MediaRenderTool


def _timeline(*clips: VideoClip, duration_ms: int) -> Timeline:
    return Timeline(
        width=1080,
        height=1920,
        fps=30,
        duration_ms=duration_ms,
        video_tracks=[VideoTrack(id="main", clips=list(clips))],
    )


def test_compiler_builds_argument_vector_for_contiguous_clips(tmp_path: Path) -> None:
    first = tmp_path / "first clip.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    timeline = _timeline(
        VideoClip(
            id="c1",
            asset_id="a1",
            source_end_ms=1_000,
            timeline_start_ms=0,
        ),
        VideoClip(
            id="c2",
            asset_id="a2",
            source_start_ms=500,
            source_end_ms=2_500,
            timeline_start_ms=1_000,
            speed=2,
        ),
        duration_ms=2_000,
    )

    plan = TimelineCompiler.compile(
        timeline,
        {"a1": first.as_uri(), "a2": str(second)},
        str(tmp_path / "output.mp4"),
    )

    assert plan.arguments[0] == "ffmpeg"
    assert str(first.resolve()) in plan.arguments
    filter_graph = plan.arguments[plan.arguments.index("-filter_complex") + 1]
    assert "trim=start=0.000:end=1.000" in filter_graph
    assert "setpts=(PTS-STARTPTS)/2" in filter_graph
    assert "concat=n=2:v=1:a=0[vout]" in filter_graph


def test_compiler_rejects_timeline_gaps(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    timeline = _timeline(
        VideoClip(
            id="c1",
            asset_id="a1",
            source_end_ms=1_000,
            timeline_start_ms=100,
        ),
        duration_ms=1_100,
    )

    with pytest.raises(UnsupportedTimelineError, match="contiguous"):
        TimelineCompiler.compile(timeline, {"a1": str(source)}, str(tmp_path / "out.mp4"))


async def test_render_executes_without_shell_and_returns_artifact(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source clip.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "renders" / "result.mp4"
    timeline = _timeline(
        VideoClip(id="c1", asset_id="a1", source_end_ms=1_000, timeline_start_ms=0),
        duration_ms=1_000,
    )
    captured: tuple[str, ...] = ()

    class Process:
        returncode = 0

        async def communicate(self):
            output.write_bytes(b"rendered")
            return b"", b""

    async def fake_subprocess(*arguments, **kwargs):
        nonlocal captured
        captured = arguments
        assert kwargs["stderr"] == asyncio.subprocess.PIPE
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    result = await MediaRenderTool().execute(
        timeline=timeline.model_dump(),
        assets={"a1": str(source)},
        output_path=str(output),
    )

    assert result.success is True
    assert captured[0] == "ffmpeg"
    assert str(source) in captured
    payload = json.loads(result.output or "{}")
    assert payload["output_path"] == str(output)
    assert payload["size_bytes"] == len(b"rendered")


async def test_render_rejects_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"video")
    output.write_bytes(b"keep")
    timeline = _timeline(
        VideoClip(id="c1", asset_id="a1", source_end_ms=1_000, timeline_start_ms=0),
        duration_ms=1_000,
    )

    result = await MediaRenderTool().execute(
        timeline=timeline.model_dump(),
        assets={"a1": str(source)},
        output_path=str(output),
    )

    assert result.success is False
    assert "already exists" in (result.error or "")
    assert output.read_bytes() == b"keep"
