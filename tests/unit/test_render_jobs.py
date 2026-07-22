"""Render job contract and persistence tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from axonflow.media.models import RenderJob, RenderJobStatus, Timeline, VideoClip, VideoTrack
from axonflow.platform.store import PlatformStore


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


def test_render_job_rejects_undeclared_timeline_assets() -> None:
    with pytest.raises(ValidationError, match="undeclared"):
        RenderJob(
            id="render-1",
            timeline=_timeline(),
            input_asset_ids=["asset-other"],
            output_path="/renders/output.mp4",
        )


def test_render_job_store_round_trip_and_filter(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    queued = RenderJob(
        id="render-queued",
        timeline=_timeline(),
        input_asset_ids=["asset-source"],
        output_path="/renders/queued.mp4",
    )
    failed = RenderJob(
        id="render-failed",
        timeline=_timeline(),
        input_asset_ids=["asset-source"],
        output_path="/renders/failed.mp4",
        status="failed",
        error="encoder unavailable",
    )
    store.save_render_job(queued)
    store.save_render_job(failed)

    assert store.get_render_job(queued.id) == queued
    assert [job.id for job in store.list_render_jobs(RenderJobStatus.FAILED)] == [failed.id]
    assert {job.id for job in store.list_render_jobs()} == {queued.id, failed.id}
    store.close()
