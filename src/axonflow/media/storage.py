"""Local media object storage used by the first AxonFlow deployment profile."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class StoredMedia:
    path: Path
    size_bytes: int
    checksum_sha256: str


class LocalMediaStorage:
    """Stream media into a workspace-owned directory with atomic finalization."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.assets_dir = self.root / "assets"
        self.renders_dir = self.root / "renders"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.renders_dir.mkdir(parents=True, exist_ok=True)

    async def save_upload(
        self,
        asset_id: str,
        filename: str,
        chunks: AsyncIterator[bytes],
        *,
        max_bytes: int,
    ) -> StoredMedia:
        safe_name = self.safe_filename(filename)
        final_path = self.assets_dir / f"{asset_id}-{safe_name}"
        temporary_path = self.assets_dir / f".{asset_id}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary_path.open("xb") as output:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"upload exceeds the {max_bytes}-byte limit")
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise ValueError("uploaded media cannot be empty")
            os.replace(temporary_path, final_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return StoredMedia(
            path=final_path,
            size_bytes=size,
            checksum_sha256=digest.hexdigest(),
        )

    def resolve_owned_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("asset is not stored in local workspace storage")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("asset path is outside local workspace storage")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def render_path(self, job_id: str, output_name: str) -> Path:
        safe_name = self.safe_filename(output_name)
        if Path(safe_name).suffix.lower() != ".mp4":
            raise ValueError("render output name must end with .mp4")
        return self.renders_dir / f"{job_id}-{safe_name}"

    @staticmethod
    def safe_filename(value: str) -> str:
        basename = Path(value.strip()).name
        normalized = _SAFE_FILENAME.sub("_", basename).strip("._")
        if not normalized:
            raise ValueError("filename must contain at least one safe character")
        return normalized[:180]
