"""Versioned media asset and edit-timeline contracts.

The contracts in this module are deliberately independent from FFmpeg and any
specific model provider. Agents produce these validated objects; deterministic
workers consume them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class AssetKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    SUBTITLE = "subtitle"
    DATA = "data"


class AssetStatus(StrEnum):
    REGISTERED = "registered"
    PROBING = "probing"
    READY = "ready"
    FAILED = "failed"


class RenderJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class MediaAsset(BaseModel):
    """A source or generated media artifact known to AxonFlow."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    kind: AssetKind
    media_type: str | None = None
    status: AssetStatus = AssetStatus.REGISTERED
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
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @field_validator("id", "name", "uri")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise ValueError("checksum_sha256 must contain exactly 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def validate_dimensions(self) -> MediaAsset:
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be provided together")
        if self.status == AssetStatus.FAILED and not self.error:
            raise ValueError("failed assets require an error message")
        return self


class MediaProbeResult(BaseModel):
    """Normalized subset of FFprobe output used to update a MediaAsset."""

    format_name: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0)
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)
    streams: list[dict[str, Any]] = Field(default_factory=list)


class ClipTransform(BaseModel):
    """Transform values use normalized coordinates where applicable."""

    x: float = 0.5
    y: float = 0.5
    scale: float = Field(default=1.0, gt=0)
    rotation_degrees: float = 0.0
    opacity: float = Field(default=1.0, ge=0, le=1)


class TimelineClip(BaseModel):
    id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    source_start_ms: int = Field(default=0, ge=0)
    source_end_ms: int = Field(gt=0)
    timeline_start_ms: int = Field(ge=0)
    speed: float = Field(default=1.0, gt=0, le=16)

    @model_validator(mode="after")
    def validate_range(self) -> TimelineClip:
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("source_end_ms must be greater than source_start_ms")
        return self

    @property
    def timeline_duration_ms(self) -> int:
        return round((self.source_end_ms - self.source_start_ms) / self.speed)

    @property
    def timeline_end_ms(self) -> int:
        return self.timeline_start_ms + self.timeline_duration_ms


class VideoClip(TimelineClip):
    transform: ClipTransform = Field(default_factory=ClipTransform)
    transition_in: str | None = None
    transition_out: str | None = None


class AudioClip(TimelineClip):
    volume: float = Field(default=1.0, ge=0, le=4)
    fade_in_ms: int = Field(default=0, ge=0)
    fade_out_ms: int = Field(default=0, ge=0)


class SubtitleCue(BaseModel):
    id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1)
    speaker: str | None = None
    style: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("subtitle text cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> SubtitleCue:
        if self.end_ms <= self.start_ms:
            raise ValueError("subtitle end_ms must be greater than start_ms")
        return self


class VideoTrack(BaseModel):
    id: str = Field(min_length=1)
    clips: list[VideoClip] = Field(default_factory=list)


class AudioTrack(BaseModel):
    id: str = Field(min_length=1)
    clips: list[AudioClip] = Field(default_factory=list)


class SubtitleTrack(BaseModel):
    id: str = Field(min_length=1)
    language: str = "und"
    cues: list[SubtitleCue] = Field(default_factory=list)


class Timeline(BaseModel):
    """Provider-neutral edit decision timeline consumed by render workers."""

    version: str = "1.0"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0, le=240)
    duration_ms: int = Field(gt=0)
    video_tracks: list[VideoTrack] = Field(default_factory=list)
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_content_bounds(self) -> Timeline:
        clip_ends = [
            clip.timeline_end_ms
            for track in [*self.video_tracks, *self.audio_tracks]
            for clip in track.clips
        ]
        cue_ends = [cue.end_ms for track in self.subtitle_tracks for cue in track.cues]
        if clip_ends and max(clip_ends) > self.duration_ms:
            raise ValueError("a clip extends beyond timeline duration_ms")
        if cue_ends and max(cue_ends) > self.duration_ms:
            raise ValueError("a subtitle cue extends beyond timeline duration_ms")
        return self


class RenderJob(BaseModel):
    """Durable state for one deterministic Timeline render."""

    id: str = Field(min_length=1)
    timeline: Timeline
    input_asset_ids: list[str] = Field(min_length=1)
    output_path: str = Field(min_length=1)
    status: RenderJobStatus = RenderJobStatus.QUEUED
    progress: float = Field(default=0, ge=0, le=1)
    output_asset_id: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    completed_at: str | None = None

    @field_validator("input_asset_ids")
    @classmethod
    def normalize_asset_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not normalized:
            raise ValueError("input_asset_ids cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_job_state(self) -> RenderJob:
        referenced = {
            clip.asset_id
            for track in [*self.timeline.video_tracks, *self.timeline.audio_tracks]
            for clip in track.clips
        }
        if not referenced.issubset(set(self.input_asset_ids)):
            missing = sorted(referenced - set(self.input_asset_ids))
            raise ValueError(f"timeline references undeclared assets: {', '.join(missing)}")
        if self.status == RenderJobStatus.COMPLETED and not self.output_asset_id:
            raise ValueError("completed render jobs require output_asset_id")
        if self.status == RenderJobStatus.FAILED and not self.error:
            raise ValueError("failed render jobs require an error message")
        return self
