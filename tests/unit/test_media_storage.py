"""Local media storage safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from axonflow.media.storage import LocalMediaStorage


async def _chunks(*values: bytes):
    for value in values:
        yield value


async def test_storage_streams_atomically_and_checks_size(tmp_path: Path) -> None:
    storage = LocalMediaStorage(tmp_path / "media")
    stored = await storage.save_upload(
        "asset-1",
        "../../my clip.mp4",
        _chunks(b"abc", b"def"),
        max_bytes=6,
    )

    assert stored.path.name == "asset-1-my_clip.mp4"
    assert stored.path.read_bytes() == b"abcdef"
    assert len(stored.checksum_sha256) == 64
    assert storage.resolve_owned_uri(stored.path.as_uri()) == stored.path


async def test_storage_removes_partial_upload_over_limit(tmp_path: Path) -> None:
    storage = LocalMediaStorage(tmp_path / "media")
    with pytest.raises(ValueError, match="exceeds"):
        await storage.save_upload(
            "asset-2",
            "large.mp4",
            _chunks(b"1234", b"5678"),
            max_bytes=7,
        )

    assert not list(storage.assets_dir.iterdir())


def test_storage_rejects_download_outside_owned_root(tmp_path: Path) -> None:
    storage = LocalMediaStorage(tmp_path / "media")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    with pytest.raises(ValueError, match="outside"):
        storage.resolve_owned_uri(outside.as_uri())
