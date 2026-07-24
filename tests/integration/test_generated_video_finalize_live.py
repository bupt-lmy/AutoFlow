"""Local generated-video delivery and disclosure validation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from axonflow.tools.generated_video import GeneratedVideoFinalizeTool
from axonflow.tools.video_edit import FFMPEG_FULL

pytestmark = pytest.mark.skipif(not FFMPEG_FULL.is_file(), reason="ffmpeg-full is unavailable")


async def test_generated_video_finalizer_adds_audio_and_disclosure(tmp_path) -> None:
    source = tmp_path / "generated-silent.mp4"
    subprocess.run(
        [
            str(FFMPEG_FULL),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=24:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )

    result = await GeneratedVideoFinalizeTool(tmp_path / "final").execute(str(source))

    assert result.success is True, result.error
    final_video = json.loads(result.output or "{}")
    assert final_video["ai_generated"] is True
    assert final_video["fictional_content"] is True
    assert final_video["disclosure_burned"] is True
    assert final_video["video_codec"] == "h264"
    assert final_video["audio_codec"] == "aac"
    assert final_video["width"] == 1920
    assert final_video["height"] == 1080
    assert Path(final_video["output_path"]).stat().st_size > 10_000
