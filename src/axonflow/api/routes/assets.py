"""Media asset registry API."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from axonflow.api.deps import get_media_storage, get_platform_store
from axonflow.media.models import AssetKind, AssetStatus, MediaAsset
from axonflow.tools.media_probe import MediaProbeTool

router = APIRouter(prefix="/api/assets", tags=["assets"])
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AssetCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    kind: AssetKind
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    checksum_sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    uri: str | None = Field(default=None, min_length=1)
    media_type: str | None = None
    status: AssetStatus | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    checksum_sha256: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0)
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)
    proxy_uri: str | None = None
    metadata: dict[str, Any] | None = None
    error: str | None = None


@router.post("", status_code=201)
async def create_asset(body: AssetCreateRequest) -> MediaAsset:
    timestamp = _now()
    asset = MediaAsset(
        id=f"asset-{uuid.uuid4().hex[:12]}",
        created_at=timestamp,
        updated_at=timestamp,
        **body.model_dump(),
    )
    return get_platform_store().save_media_asset(asset)


async def _probe_uploaded_asset(asset: MediaAsset) -> MediaAsset:
    if asset.kind not in {AssetKind.VIDEO, AssetKind.AUDIO}:
        return asset.model_copy(update={"status": AssetStatus.READY, "updated_at": _now()})
    result = await MediaProbeTool().execute(path=asset.uri)
    if not result.success:
        return asset.model_copy(
            update={
                "status": AssetStatus.FAILED,
                "error": result.error or "Media probe failed",
                "updated_at": _now(),
            }
        )
    probe = json.loads(result.output or "{}")
    return MediaAsset.model_validate(
        {
            **asset.model_dump(),
            **probe,
            "status": AssetStatus.READY,
            "error": None,
            "updated_at": _now(),
        }
    )


@router.post("/upload", status_code=201)
async def upload_asset(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    kind: Annotated[AssetKind, Query()],
) -> MediaAsset:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_size > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Media upload exceeds 2 GiB")

    asset_id = f"asset-{uuid.uuid4().hex[:12]}"
    try:
        stored = await get_media_storage().save_upload(
            asset_id,
            name,
            request.stream(),
            max_bytes=_MAX_UPLOAD_BYTES,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=413 if "exceeds" in str(exc) else 422,
            detail=str(exc),
        ) from exc
    timestamp = _now()
    asset = MediaAsset(
        id=asset_id,
        name=get_media_storage().safe_filename(name),
        uri=stored.path.as_uri(),
        kind=kind,
        media_type=request.headers.get("content-type"),
        size_bytes=stored.size_bytes,
        checksum_sha256=stored.checksum_sha256,
        created_at=timestamp,
        updated_at=timestamp,
        metadata={"storage": "local"},
    )
    asset = await _probe_uploaded_asset(asset)
    return get_platform_store().save_media_asset(asset)


@router.get("")
async def list_assets(
    kind: Annotated[AssetKind | None, Query()] = None,
    status: Annotated[AssetStatus | None, Query()] = None,
) -> list[MediaAsset]:
    return get_platform_store().list_media_assets(kind=kind, status=status)


@router.get("/{asset_id}")
async def get_asset(asset_id: str) -> MediaAsset:
    asset = get_platform_store().get_media_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Media asset not found: {asset_id}")
    return asset


@router.get("/{asset_id}/content", response_class=FileResponse)
async def download_asset(asset_id: str) -> FileResponse:
    asset = get_platform_store().get_media_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Media asset not found: {asset_id}")
    try:
        path = get_media_storage().resolve_owned_uri(asset.uri)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type=asset.media_type, filename=asset.name)


@router.patch("/{asset_id}")
async def update_asset(asset_id: str, body: AssetUpdateRequest) -> MediaAsset:
    current = get_platform_store().get_media_asset(asset_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Media asset not found: {asset_id}")
    changes = body.model_dump(exclude_unset=True)
    updated = MediaAsset.model_validate(
        {
            **current.model_dump(),
            **changes,
            "updated_at": _now(),
        }
    )
    return get_platform_store().save_media_asset(updated)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(asset_id: str) -> None:
    if not get_platform_store().delete_media_asset(asset_id):
        raise HTTPException(status_code=404, detail=f"Media asset not found: {asset_id}")
