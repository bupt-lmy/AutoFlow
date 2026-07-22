"""Controlled FFprobe tool tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from axonflow.tools.media_probe import MediaProbeTool


class _Process:
    returncode = 0

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def test_normalize_probe_extracts_video_and_audio_metadata() -> None:
    result = MediaProbeTool.normalize_probe(
        {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "12.345",
                "size": "2048",
            },
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }
    )

    assert result.duration_ms == 12_345
    assert result.size_bytes == 2048
    assert result.width == 1920
    assert result.fps == pytest.approx(29.97002997)
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert result.sample_rate == 48_000


async def test_execute_uses_argument_vector_and_returns_json(tmp_path, monkeypatch) -> None:
    media = tmp_path / "clip with spaces.mp4"
    media.write_bytes(b"not-used-by-mocked-ffprobe")
    payload = {
        "format": {"duration": "1.5", "size": str(media.stat().st_size)},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 640,
                "height": 360,
                "avg_frame_rate": "25/1",
            }
        ],
    }
    arguments: tuple = ()

    async def fake_subprocess(*args, **kwargs):
        nonlocal arguments
        arguments = args
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        return _Process(json.dumps(payload).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    result = await MediaProbeTool().execute(path=media.as_uri())

    assert result.success is True
    assert arguments[0] == "ffprobe"
    assert arguments[-1] == str(media.resolve())
    assert json.loads(result.output or "{}")["duration_ms"] == 1500


async def test_execute_rejects_remote_urls_and_missing_files(tmp_path) -> None:
    remote = await MediaProbeTool().execute(path="https://example.com/video.mp4")
    missing = await MediaProbeTool().execute(path=str(tmp_path / "missing.mp4"))

    assert remote.success is False
    assert "only local" in (remote.error or "")
    assert missing.success is False
    assert "not found" in (missing.error or "")


async def test_execute_reports_missing_ffprobe(tmp_path, monkeypatch) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"clip")

    async def missing_binary(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_binary)
    result = await MediaProbeTool().execute(path=str(media))

    assert result.success is False
    assert "install FFmpeg" in (result.error or "")
