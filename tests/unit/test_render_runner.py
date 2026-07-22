"""Async render job execution tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from axonflow.media.jobs import RenderJobRunner
from axonflow.media.models import (
    AssetStatus,
    MediaAsset,
    RenderJob,
    RenderJobStatus,
    Timeline,
    VideoClip,
    VideoTrack,
)
from axonflow.media.storage import LocalMediaStorage
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolResult


def _timeline() -> Timeline:
    return Timeline(
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


def _runtime(tmp_path: Path):
    store = PlatformStore(tmp_path / "axonflow.db")
    storage = LocalMediaStorage(tmp_path / "media")
    source = storage.assets_dir / "asset-source.mp4"
    source.write_bytes(b"source")
    store.save_media_asset(
        MediaAsset(
            id="asset-source",
            name="source.mp4",
            uri=source.as_uri(),
            kind="video",
            status=AssetStatus.READY,
        )
    )
    return store, storage


async def test_runner_completes_and_registers_output_asset(tmp_path: Path) -> None:
    store, storage = _runtime(tmp_path)

    class RenderTool:
        async def execute(self, **arguments):
            output = Path(arguments["output_path"])
            output.write_bytes(b"rendered-video")
            return ToolResult(
                success=True,
                output=json.dumps(
                    {
                        "duration_ms": 1_000,
                        "width": 320,
                        "height": 240,
                        "fps": 25,
                    }
                ),
            )

    runner = RenderJobRunner(store, storage, RenderTool())  # type: ignore[arg-type]
    submitted = await runner.submit(_timeline(), "result.mp4")
    completed = await runner.wait(submitted.id)

    assert completed is not None
    assert completed.status == RenderJobStatus.COMPLETED
    assert completed.progress == 1
    output_asset = store.get_media_asset(completed.output_asset_id or "")
    assert output_asset is not None
    assert output_asset.metadata["render_job_id"] == completed.id
    assert output_asset.checksum_sha256 is not None
    store.close()


async def test_runner_cancels_active_job(tmp_path: Path) -> None:
    store, storage = _runtime(tmp_path)
    started = asyncio.Event()

    class RenderTool:
        async def execute(self, **_arguments):
            started.set()
            await asyncio.Event().wait()
            return ToolResult(success=True)

    runner = RenderJobRunner(store, storage, RenderTool())  # type: ignore[arg-type]
    submitted = await runner.submit(_timeline(), "cancel.mp4")
    await started.wait()
    canceled = await runner.cancel(submitted.id)

    assert canceled is not None
    assert canceled.status == RenderJobStatus.CANCELED
    assert canceled.completed_at is not None
    store.close()


def test_runner_marks_interrupted_jobs_failed(tmp_path: Path) -> None:
    store, storage = _runtime(tmp_path)
    runner = RenderJobRunner(store, storage)
    interrupted = runner.store.save_render_job(
        RenderJob(
            id="render-interrupted",
            timeline=_timeline(),
            input_asset_ids=["asset-source"],
            output_path=str(storage.renders_dir / "interrupted.mp4"),
            status="running",
        )
    )

    assert runner.recover_interrupted() == 1
    recovered = store.get_render_job(interrupted.id)
    assert recovered is not None
    assert recovered.status == RenderJobStatus.FAILED
    assert "restarted" in (recovered.error or "")
    store.close()
