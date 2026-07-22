"""Render job API behavior tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from axonflow.api.routes import render_jobs as routes
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


@pytest.fixture
def render_runtime(tmp_path: Path, monkeypatch):
    store = PlatformStore(tmp_path / "axonflow.db")
    storage = LocalMediaStorage(tmp_path / "media")
    source = storage.assets_dir / "source.mp4"
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
    blocker = asyncio.Event()

    class RenderTool:
        async def execute(self, **_arguments):
            await blocker.wait()
            return ToolResult(success=False, error="not expected to complete")

    runner = RenderJobRunner(store, storage, RenderTool())  # type: ignore[arg-type]
    monkeypatch.setattr(routes, "get_platform_store", lambda: store)
    monkeypatch.setattr(routes, "get_render_job_runner", lambda: runner)
    yield store, runner
    for task in list(runner._tasks.values()):
        task.cancel()
    store.close()


async def test_render_job_routes_submit_list_get_and_cancel(render_runtime) -> None:
    _store, runner = render_runtime
    submitted = await routes.create_render_job(
        routes.RenderJobCreateRequest(timeline=_timeline(), output_name="api-result.mp4")
    )
    await asyncio.sleep(0)

    assert submitted.status == RenderJobStatus.QUEUED
    assert await routes.get_render_job(submitted.id)
    assert any(job.id == submitted.id for job in await routes.list_render_jobs())

    canceled = await routes.cancel_render_job(submitted.id)
    assert canceled.status == RenderJobStatus.CANCELED
    await runner.shutdown()


async def test_render_job_route_rejects_missing_asset(render_runtime) -> None:
    timeline = _timeline().model_copy(deep=True)
    timeline.video_tracks[0].clips[0].asset_id = "asset-missing"

    with pytest.raises(HTTPException) as captured:
        await routes.create_render_job(routes.RenderJobCreateRequest(timeline=timeline))
    assert captured.value.status_code == 422
    assert "not found" in captured.value.detail
