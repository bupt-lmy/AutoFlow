"""Compile validated timelines into deterministic FFmpeg argument vectors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from axonflow.media.models import Timeline, VideoClip


class UnsupportedTimelineError(ValueError):
    """Raised when a valid timeline requests a feature not implemented by the renderer."""


@dataclass(frozen=True)
class RenderPlan:
    arguments: tuple[str, ...]
    input_paths: tuple[Path, ...]
    output_path: Path


class TimelineCompiler:
    """Compile the deliberately small first rendering profile.

    Supported profile: one contiguous video track, H.264 MP4 output, optional
    speed adjustment, and aspect-fit scaling with letter/pillar boxing.
    """

    @classmethod
    def compile(
        cls,
        timeline: Timeline,
        assets: dict[str, str],
        output_path: str,
        *,
        overwrite: bool = False,
    ) -> RenderPlan:
        clips = cls._supported_clips(timeline)
        resolved_assets: dict[str, Path] = {}
        for clip in clips:
            value = assets.get(clip.asset_id)
            if value is None:
                raise ValueError(f"missing path for asset: {clip.asset_id}")
            path = cls.local_path(value)
            if not path.is_file():
                raise ValueError(f"media file not found for {clip.asset_id}: {path}")
            resolved_assets[clip.asset_id] = path

        output = cls.local_path(output_path)
        if output.suffix.lower() != ".mp4":
            raise ValueError("the initial renderer supports only .mp4 output")
        if output in resolved_assets.values():
            raise ValueError("output_path must not overwrite an input asset")

        unique_asset_ids = list(dict.fromkeys(clip.asset_id for clip in clips))
        input_paths = tuple(resolved_assets[asset_id] for asset_id in unique_asset_ids)
        input_index = {asset_id: index for index, asset_id in enumerate(unique_asset_ids)}
        arguments: list[str] = ["ffmpeg", "-y" if overwrite else "-n", "-v", "error"]
        for path in input_paths:
            arguments.extend(["-i", str(path)])

        filters: list[str] = []
        labels: list[str] = []
        for index, clip in enumerate(clips):
            label = f"v{index}"
            labels.append(f"[{label}]")
            start = cls.seconds(clip.source_start_ms)
            end = cls.seconds(clip.source_end_ms)
            filters.append(
                f"[{input_index[clip.asset_id]}:v:0]"
                f"trim=start={start}:end={end},"
                f"setpts=(PTS-STARTPTS)/{clip.speed:g},"
                f"scale={timeline.width}:{timeline.height}:force_original_aspect_ratio=decrease,"
                f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1[{label}]"
            )
        output_label = labels[0]
        if len(labels) > 1:
            output_label = "[vout]"
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0{output_label}")

        arguments.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                output_label,
                "-an",
                "-r",
                f"{timeline.fps:g}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        return RenderPlan(tuple(arguments), input_paths, output)

    @classmethod
    def _supported_clips(cls, timeline: Timeline) -> list[VideoClip]:
        if len(timeline.video_tracks) != 1 or not timeline.video_tracks[0].clips:
            raise UnsupportedTimelineError("the initial renderer requires exactly one video track")
        if timeline.audio_tracks or timeline.subtitle_tracks:
            raise UnsupportedTimelineError(
                "audio and subtitle tracks are not supported by the initial renderer"
            )
        clips = sorted(timeline.video_tracks[0].clips, key=lambda clip: clip.timeline_start_ms)
        cursor = 0
        for clip in clips:
            if clip.timeline_start_ms != cursor:
                raise UnsupportedTimelineError("video clips must be contiguous and start at 0")
            if clip.transition_in or clip.transition_out:
                raise UnsupportedTimelineError(
                    "transitions are not supported by the initial renderer"
                )
            transform = clip.transform
            if (
                transform.x != 0.5
                or transform.y != 0.5
                or transform.scale != 1
                or transform.rotation_degrees != 0
                or transform.opacity != 1
            ):
                raise UnsupportedTimelineError(
                    "custom transforms are not supported by the initial renderer"
                )
            cursor = clip.timeline_end_ms
        if cursor != timeline.duration_ms:
            raise UnsupportedTimelineError(
                "timeline duration must equal the end of the final video clip"
            )
        return clips

    @staticmethod
    def local_path(value: str) -> Path:
        parsed = urlparse(value)
        if parsed.scheme not in {"", "file"}:
            raise ValueError("renderer accepts only local paths or file:// URIs")
        if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
            raise ValueError("remote file URI hosts are not supported")
        raw_path = unquote(parsed.path) if parsed.scheme == "file" else value
        return Path(raw_path).expanduser().resolve()

    @staticmethod
    def seconds(milliseconds: int) -> str:
        return f"{milliseconds / 1000:.3f}"
