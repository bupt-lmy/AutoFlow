"""Opt-in local Whisper transcription and visible hard-subtitle validation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from axonflow.tools.video_edit import FFMPEG_FULL, HardSubtitleBurnTool, VideoTranscribeTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MEDIA_LIVE") != "1",
    reason="Set RUN_MEDIA_LIVE=1 after downloading the local Whisper model",
)


async def test_transcribe_and_burn_real_minimax_speech(tmp_path) -> None:
    project_dir = Path(__file__).resolve().parents[2]
    model = project_dir / "workspace" / "models" / "ggml-small.bin"
    speeches = sorted(
        (project_dir / "workspace" / "media" / "generated").glob("minimax-speech-*.mp3")
    )
    if not model.is_file() or not speeches:
        pytest.skip("Whisper model or generated speech fixture is unavailable")
    source = tmp_path / "dynamic-with-speech.mp4"
    subprocess.run(
        [
            str(FFMPEG_FULL),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=25:d=12",
            "-i",
            str(speeches[-1]),
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )

    transcribed_result = await VideoTranscribeTool(model, tmp_path / "transcripts").execute(
        str(source)
    )
    assert transcribed_result.success is True, transcribed_result.error
    transcript = json.loads(transcribed_result.output or "{}")
    subtitle_content = Path(transcript["output_path"]).read_text(encoding="utf-8")
    assert transcript["cue_count"] > 0
    assert "-->" in subtitle_content

    burned_result = await HardSubtitleBurnTool(tmp_path / "final").execute(
        video_path=str(source), subtitle_path=transcript["output_path"]
    )
    assert burned_result.success is True, burned_result.error
    final_video = json.loads(burned_result.output or "{}")
    assert final_video["subtitles_burned"] is True
    assert final_video["video_codec"] == "h264"
    assert Path(final_video["output_path"]).stat().st_size > source.stat().st_size / 2
    print(f"WHISPER_TRANSCRIPT={subtitle_content}")
    print(f"HARD_SUBTITLE_ARTIFACT={final_video['output_path']}")
