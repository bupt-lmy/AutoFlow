"""Register verified local media as a durable platform asset."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from axonflow.media.models import AssetStatus, MediaAsset
from axonflow.media.render import TimelineCompiler
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import Tool, ToolResult
from axonflow.tools.media_probe import MediaProbeTool


class MediaRegisterTool(Tool):
    name = "media_register"
    description = "将质检通过的本地成片探测、计算 SHA-256 并登记到媒体资产库"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "name": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store

    async def execute(self, path: str, name: str | None = None, **_kwargs: Any) -> ToolResult:
        if self.store is None:
            return ToolResult(success=False, error="Platform asset storage is unavailable")
        try:
            media_path = TimelineCompiler.local_path(path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        if not media_path.is_file():
            return ToolResult(success=False, error=f"Media file not found: {media_path}")
        probe = await MediaProbeTool().execute(path=str(media_path))
        if not probe.success:
            return ToolResult(
                success=False, error=f"Cannot register unprobeable media: {probe.error}"
            )
        metadata = json.loads(probe.output or "{}")
        checksum = self._sha256(media_path)
        asset = MediaAsset(
            id=f"asset-{uuid.uuid4().hex[:12]}",
            name=(name or media_path.name).strip(),
            uri=media_path.as_uri(),
            kind="video",
            media_type="video/mp4",
            status=AssetStatus.READY,
            size_bytes=media_path.stat().st_size,
            checksum_sha256=checksum,
            duration_ms=metadata.get("duration_ms"),
            width=metadata.get("width"),
            height=metadata.get("height"),
            fps=metadata.get("fps"),
            video_codec=metadata.get("video_codec"),
            audio_codec=metadata.get("audio_codec"),
            sample_rate=metadata.get("sample_rate"),
            channels=metadata.get("channels"),
            metadata={"source": "axonflow-media-workflow", "quality_gate": "passed"},
        )
        self.store.save_media_asset(asset)
        return ToolResult(success=True, output=asset.model_dump_json())

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
