"""Live FFmpeg smoke test for the local async render pipeline."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from axonflow.media.jobs import RenderJobRunner
from axonflow.media.models import (
    AssetStatus,
    MediaAsset,
    RenderJobStatus,
    Timeline,
    VideoClip,
    VideoTrack,
)
from axonflow.media.storage import LocalMediaStorage
from axonflow.platform.store import PlatformStore
from axonflow.tools.media_probe import MediaProbeTool


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
async def test_live_render_job_creates_registered_probeable_video(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    storage = LocalMediaStorage(tmp_path / "media")
    source = storage.assets_dir / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    store.save_media_asset(
        MediaAsset(
            id="asset-source",
            name=source.name,
            uri=source.as_uri(),
            kind="video",
            status=AssetStatus.READY,
        )
    )
    timeline = Timeline(
        width=320,
        height=240,
        fps=25,
        duration_ms=1_000,
        video_tracks=[
            VideoTrack(
                id="main",
                clips=[
                    VideoClip(
                        id="clip-1",
                        asset_id="asset-source",
                        source_end_ms=1_000,
                        timeline_start_ms=0,
                    )
                ],
            )
        ],
    )

    runner = RenderJobRunner(store, storage)
    submitted = await runner.submit(timeline, "live-result.mp4")
    completed = await runner.wait(submitted.id)

    assert completed is not None
    assert completed.status == RenderJobStatus.COMPLETED
    output_asset = store.get_media_asset(completed.output_asset_id or "")
    assert output_asset is not None
    probe = await MediaProbeTool().execute(path=output_asset.uri)
    assert probe.success is True
    metadata = json.loads(probe.output or "{}")
    assert metadata["duration_ms"] == 1_000
    assert metadata["width"] == 320
    assert metadata["height"] == 240
    assert metadata["video_codec"] == "h264"
    store.close()
