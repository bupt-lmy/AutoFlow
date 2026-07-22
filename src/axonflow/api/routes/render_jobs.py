"""Asynchronous media render job API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from axonflow.api.deps import get_platform_store, get_render_job_runner
from axonflow.media.models import RenderJob, RenderJobStatus, Timeline

router = APIRouter(prefix="/api/render-jobs", tags=["render-jobs"])


class RenderJobCreateRequest(BaseModel):
    timeline: Timeline
    output_name: str = Field(default="rendered.mp4", min_length=1, max_length=180)


@router.post("", status_code=202)
async def create_render_job(body: RenderJobCreateRequest) -> RenderJob:
    try:
        return await get_render_job_runner().submit(body.timeline, body.output_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
async def list_render_jobs(
    status: Annotated[RenderJobStatus | None, Query()] = None,
) -> list[RenderJob]:
    return get_platform_store().list_render_jobs(status)


@router.get("/{job_id}")
async def get_render_job(job_id: str) -> RenderJob:
    job = get_platform_store().get_render_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Render job not found: {job_id}")
    return job


@router.post("/{job_id}/cancel")
async def cancel_render_job(job_id: str) -> RenderJob:
    job = await get_render_job_runner().cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Render job not found: {job_id}")
    return job
