"""Opt-in end-to-end semantic source-video editing workflow acceptance test."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from axonflow.config.loader import load_global_config
from axonflow.engine import AxonFlowEngine
from axonflow.platform.store import PlatformStore
from axonflow.tools.video_edit import FFMPEG_FULL

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_LIVE") != "1",
    reason="Set RUN_MINIMAX_LIVE=1 for full MiniMax-M3 workflow acceptance",
)


def _source_with_delayed_speech(path: Path, speech: Path) -> None:
    subprocess.run(
        [
            str(FFMPEG_FULL),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=640x360:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "smptebars=s=640x360:r=25:d=2",
            "-i",
            str(speech),
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a]adelay=4000:all=1,apad=whole_dur=6[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


async def test_semantic_video_workflow_delivers_selected_motion_with_hard_subtitles(
    tmp_path,
) -> None:
    project_dir = Path(__file__).resolve().parents[2]
    speeches = sorted((project_dir / "workspace/media/generated").glob("minimax-speech-*.mp3"))
    if not speeches:
        pytest.skip("Generated MiniMax speech fixture is unavailable")
    source = tmp_path / "semantic-source.mp4"
    _source_with_delayed_speech(source, speeches[-1])

    config = load_global_config(project_dir / "config/axonflow.yaml")
    config.agent_health.enabled = False
    config.log_level = "ERROR"
    store = PlatformStore(project_dir / "workspace/axonflow.db")
    engine = AxonFlowEngine(
        config_dir=str(project_dir / "config"),
        config=config,
        platform_store=store,
    )
    try:
        await engine.start()
        result = await engine.run_workflow(
            "semantic-video-edit",
            json.dumps(
                {
                    "source": str(source),
                    "description": "只选择电视测试彩条画面，不要红色纯色画面或动态测试图。",
                    "target_duration_seconds": 2,
                    "hard_subtitles": True,
                },
                ensure_ascii=False,
            ),
        )
    finally:
        await engine.stop()
        store.close()

    print(f"SEMANTIC_WORKFLOW_RESULT={result.to_dict()}")
    assert result.status == "completed", result.to_dict()
    assert result.iterations == 12
    assert result.output["quality_report"]["verdict"] == "passed"
    assert result.output["composed_video"]["subtitles_burned"] is True
    assert result.output["composed_video"]["duration_ms"] == 2000
    assert result.output["refinement_report"]["actual_duration_ms"] == 2000
    assert result.output["selected_clips"]
    assert result.output["registered_asset"]["status"] == "ready"
    assert Path(result.output["composed_video"]["output_path"]).is_file()


@pytest.mark.skipif(
    not os.environ.get("SEMANTIC_VIDEO_URL"),
    reason="Set SEMANTIC_VIDEO_URL to validate a user-authorized remote source",
)
async def test_user_url_delivers_thirty_second_semantic_edit() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    config = load_global_config(project_dir / "config/axonflow.yaml")
    config.agent_health.enabled = False
    config.log_level = "ERROR"
    store = PlatformStore(project_dir / "workspace/axonflow.db")
    engine = AxonFlowEngine(
        config_dir=str(project_dir / "config"),
        config=config,
        platform_store=store,
    )
    try:
        await engine.start()
        result = await engine.run_workflow(
            "semantic-video-edit",
            json.dumps(
                {
                    "source": os.environ["SEMANTIC_VIDEO_URL"],
                    "description": (
                        "从整支视频中选择最有视觉冲击力、人物动作明显、镜头变化丰富且"
                        "节奏强的精彩片段；优先高潮段落，排除片头片尾、黑场和重复空镜。"
                        "按原时间顺序组织，让30秒剪辑具有开场、推进和收束。"
                    ),
                    "target_duration_seconds": 30,
                    "hard_subtitles": True,
                },
                ensure_ascii=False,
            ),
        )
    finally:
        await engine.stop()
        store.close()

    print(f"USER_URL_WORKFLOW_RESULT={result.to_dict()}")
    assert result.status == "completed", result.to_dict()
    final_video = result.output["composed_video"]
    assert 29_000 <= final_video["duration_ms"] <= 30_500
    assert final_video["subtitles_burned"] is True
    assert result.output["quality_report"]["verdict"] == "passed"
    assert result.output["registered_asset"]["status"] == "ready"
    assert Path(final_video["output_path"]).is_file()
