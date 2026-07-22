"""Media domain contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from axonflow.media.models import (
    AssetKind,
    AssetStatus,
    MediaAsset,
    SubtitleCue,
    SubtitleTrack,
    Timeline,
    VideoClip,
    VideoTrack,
)


def test_media_asset_normalizes_and_validates_checksum() -> None:
    asset = MediaAsset(
        id=" asset-1 ",
        name=" source.mp4 ",
        uri=" file:///workspace/source.mp4 ",
        kind=AssetKind.VIDEO,
        checksum_sha256="A" * 64,
        width=1920,
        height=1080,
    )

    assert asset.id == "asset-1"
    assert asset.checksum_sha256 == "a" * 64
    assert asset.status == AssetStatus.REGISTERED


def test_media_asset_rejects_partial_dimensions_and_failed_without_error() -> None:
    with pytest.raises(ValidationError, match="width and height"):
        MediaAsset(id="a", name="a.mp4", uri="file:///a.mp4", kind="video", width=1920)

    with pytest.raises(ValidationError, match="require an error"):
        MediaAsset(
            id="a",
            name="a.mp4",
            uri="file:///a.mp4",
            kind="video",
            status="failed",
        )


def test_timeline_computes_speed_adjusted_duration_and_validates_bounds() -> None:
    clip = VideoClip(
        id="clip-1",
        asset_id="asset-1",
        source_start_ms=1_000,
        source_end_ms=5_000,
        timeline_start_ms=2_000,
        speed=2,
    )
    timeline = Timeline(
        width=1080,
        height=1920,
        fps=30,
        duration_ms=4_000,
        video_tracks=[VideoTrack(id="video-main", clips=[clip])],
    )

    assert clip.timeline_duration_ms == 2_000
    assert clip.timeline_end_ms == 4_000
    assert timeline.version == "1.0"

    with pytest.raises(ValidationError, match="extends beyond"):
        Timeline(
            width=1080,
            height=1920,
            fps=30,
            duration_ms=3_999,
            video_tracks=[VideoTrack(id="video-main", clips=[clip])],
        )


def test_timeline_rejects_subtitle_outside_duration() -> None:
    cue = SubtitleCue(id="cue-1", start_ms=900, end_ms=1_100, text=" Hello ")
    assert cue.text == "Hello"

    with pytest.raises(ValidationError, match="subtitle cue"):
        Timeline(
            width=1920,
            height=1080,
            fps=25,
            duration_ms=1_000,
            subtitle_tracks=[SubtitleTrack(id="zh", language="zh-CN", cues=[cue])],
        )
