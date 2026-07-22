"""Media asset persistence and route tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from axonflow.api.routes import assets as asset_routes
from axonflow.media.models import AssetKind, AssetStatus, MediaAsset
from axonflow.media.storage import LocalMediaStorage
from axonflow.platform.store import PlatformStore


@pytest.fixture
def asset_store(tmp_path: Path, monkeypatch) -> PlatformStore:
    store = PlatformStore(tmp_path / "axonflow.db")
    storage = LocalMediaStorage(tmp_path / "media")
    monkeypatch.setattr(asset_routes, "get_platform_store", lambda: store)
    monkeypatch.setattr(asset_routes, "get_media_storage", lambda: storage)
    yield store
    store.close()


def test_store_persists_and_filters_media_assets(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    video = MediaAsset(
        id="asset-video",
        name="source.mp4",
        uri="file:///source.mp4",
        kind="video",
        status="ready",
        width=1920,
        height=1080,
    )
    audio = MediaAsset(
        id="asset-audio",
        name="voice.wav",
        uri="file:///voice.wav",
        kind="audio",
    )
    store.save_media_asset(video)
    store.save_media_asset(audio)

    assert store.get_media_asset("asset-video") == video
    assert [asset.id for asset in store.list_media_assets(kind=AssetKind.VIDEO)] == [
        "asset-video"
    ]
    assert [asset.id for asset in store.list_media_assets(status=AssetStatus.REGISTERED)] == [
        "asset-audio"
    ]
    assert store.delete_media_asset("asset-video") is True
    assert store.get_media_asset("asset-video") is None
    store.close()


async def test_asset_routes_create_update_list_and_delete(asset_store: PlatformStore) -> None:
    created = await asset_routes.create_asset(
        asset_routes.AssetCreateRequest(
            name="source.mp4",
            uri="file:///workspace/source.mp4",
            kind="video",
            media_type="video/mp4",
        )
    )
    assert created.id.startswith("asset-")
    assert created.status == AssetStatus.REGISTERED

    updated = await asset_routes.update_asset(
        created.id,
        asset_routes.AssetUpdateRequest(
            status="ready",
            duration_ms=2_500,
            width=1080,
            height=1920,
            fps=30,
        ),
    )
    assert updated.status == AssetStatus.READY
    assert updated.duration_ms == 2_500

    listed = await asset_routes.list_assets(kind=AssetKind.VIDEO, status=AssetStatus.READY)
    assert [asset.id for asset in listed] == [created.id]
    assert await asset_routes.get_asset(created.id) == updated

    await asset_routes.delete_asset(created.id)
    with pytest.raises(HTTPException) as captured:
        await asset_routes.get_asset(created.id)
    assert captured.value.status_code == 404


async def test_upload_streams_content_and_registers_ready_asset(
    asset_store: PlatformStore,
    monkeypatch,
) -> None:
    class Request:
        headers = {"content-type": "application/octet-stream", "content-length": "11"}

        async def stream(self):
            yield b"hello "
            yield b"video"

    async def ready(asset: MediaAsset) -> MediaAsset:
        return asset.model_copy(update={"status": AssetStatus.READY})

    monkeypatch.setattr(asset_routes, "_probe_uploaded_asset", ready)
    asset = await asset_routes.upload_asset(
        Request(),  # type: ignore[arg-type]
        name="../unsafe clip.mp4",
        kind=AssetKind.VIDEO,
    )

    assert asset.status == AssetStatus.READY
    assert asset.size_bytes == 11
    assert asset.checksum_sha256 is not None
    assert asset.metadata["storage"] == "local"
    stored_path = asset_routes.get_media_storage().resolve_owned_uri(asset.uri)
    assert stored_path.name.endswith("unsafe_clip.mp4")
    assert stored_path.read_bytes() == b"hello video"

    response = await asset_routes.download_asset(asset.id)
    assert Path(response.path) == stored_path
