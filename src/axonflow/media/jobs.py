"""In-process async render runner backed by durable job records."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axonflow.media.models import (
    AssetKind,
    AssetStatus,
    MediaAsset,
    RenderJob,
    RenderJobStatus,
    Timeline,
)
from axonflow.media.storage import LocalMediaStorage
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolResult
from axonflow.tools.media_render import MediaRenderTool


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RenderJobRunner:
    """Run FFmpeg tasks asynchronously while keeping SQLite as source of truth."""

    def __init__(
        self,
        store: PlatformStore,
        storage: LocalMediaStorage,
        render_tool: MediaRenderTool | None = None,
    ) -> None:
        self.store = store
        self.storage = storage
        self.render_tool = render_tool or MediaRenderTool()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def submit(self, timeline: Timeline, output_name: str) -> RenderJob:
        asset_ids = self._timeline_asset_ids(timeline)
        self._resolve_assets(asset_ids)
        job_id = f"render-{uuid.uuid4().hex[:12]}"
        output_path = self.storage.render_path(job_id, output_name)
        job = RenderJob(
            id=job_id,
            timeline=timeline,
            input_asset_ids=asset_ids,
            output_path=str(output_path),
        )
        self.store.save_render_job(job)
        task = asyncio.create_task(self._run(job_id), name=f"render-job:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(job_id, None))
        return job

    async def cancel(self, job_id: str) -> RenderJob | None:
        job = self.store.get_render_job(job_id)
        if job is None:
            return None
        if job.status in {
            RenderJobStatus.COMPLETED,
            RenderJobStatus.FAILED,
            RenderJobStatus.CANCELED,
        }:
            return job
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        else:
            job = self._updated(
                job,
                status=RenderJobStatus.CANCELED,
                progress=job.progress,
                completed_at=_now(),
            )
            self.store.save_render_job(job)
        return self.store.get_render_job(job_id)

    async def wait(self, job_id: str) -> RenderJob | None:
        task = self._tasks.get(job_id)
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        return self.store.get_render_job(job_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    def recover_interrupted(self) -> int:
        recovered = 0
        for status in (RenderJobStatus.QUEUED, RenderJobStatus.RUNNING):
            for job in self.store.list_render_jobs(status):
                failed = self._updated(
                    job,
                    status=RenderJobStatus.FAILED,
                    error="Render service restarted before the job completed",
                    completed_at=_now(),
                )
                self.store.save_render_job(failed)
                recovered += 1
        return recovered

    async def _run(self, job_id: str) -> None:
        job = self.store.get_render_job(job_id)
        if job is None:
            return
        job = self._updated(
            job,
            status=RenderJobStatus.RUNNING,
            progress=0.05,
            started_at=_now(),
            error=None,
        )
        self.store.save_render_job(job)
        try:
            assets = self._resolve_assets(job.input_asset_ids)
            result = await self.render_tool.execute(
                timeline=job.timeline.model_dump(),
                assets=assets,
                output_path=job.output_path,
            )
            if not result.success:
                self.store.save_render_job(
                    self._updated(
                        job,
                        status=RenderJobStatus.FAILED,
                        error=result.error or "Media render failed",
                        completed_at=_now(),
                    )
                )
                return
            output_asset = await asyncio.to_thread(self._register_output, job, result)
            self.store.save_render_job(
                self._updated(
                    job,
                    status=RenderJobStatus.COMPLETED,
                    progress=1,
                    output_asset_id=output_asset.id,
                    completed_at=_now(),
                )
            )
        except asyncio.CancelledError:
            self.store.save_render_job(
                self._updated(
                    job,
                    status=RenderJobStatus.CANCELED,
                    completed_at=_now(),
                )
            )
            raise
        except Exception as exc:
            self.store.save_render_job(
                self._updated(
                    job,
                    status=RenderJobStatus.FAILED,
                    error=str(exc),
                    completed_at=_now(),
                )
            )

    def _resolve_assets(self, asset_ids: list[str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for asset_id in asset_ids:
            asset = self.store.get_media_asset(asset_id)
            if asset is None:
                raise ValueError(f"Media asset not found: {asset_id}")
            if asset.status != AssetStatus.READY:
                raise ValueError(f"Media asset is not ready: {asset_id} ({asset.status.value})")
            path = self.storage.resolve_owned_uri(asset.uri)
            resolved[asset_id] = str(path)
        return resolved

    def _register_output(self, job: RenderJob, result: ToolResult) -> MediaAsset:
        rendered: dict[str, Any] = json.loads(result.output or "{}")
        output_path = Path(job.output_path)
        digest = hashlib.sha256()
        with output_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        timestamp = _now()
        asset = MediaAsset(
            id=f"asset-{uuid.uuid4().hex[:12]}",
            name=output_path.name,
            uri=output_path.as_uri(),
            kind=AssetKind.VIDEO,
            media_type="video/mp4",
            status=AssetStatus.READY,
            size_bytes=output_path.stat().st_size,
            checksum_sha256=digest.hexdigest(),
            duration_ms=rendered.get("duration_ms"),
            width=rendered.get("width"),
            height=rendered.get("height"),
            fps=rendered.get("fps"),
            video_codec="h264",
            metadata={"storage": "local", "render_job_id": job.id},
            created_at=timestamp,
            updated_at=timestamp,
        )
        return self.store.save_media_asset(asset)

    @staticmethod
    def _timeline_asset_ids(timeline: Timeline) -> list[str]:
        return list(
            dict.fromkeys(
                clip.asset_id
                for track in [*timeline.video_tracks, *timeline.audio_tracks]
                for clip in track.clips
            )
        )

    @staticmethod
    def _updated(job: RenderJob, **changes: Any) -> RenderJob:
        return RenderJob.model_validate(
            {
                **job.model_dump(),
                **changes,
                "updated_at": _now(),
            }
        )
