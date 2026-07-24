"""Opt-in real MiniMax storyboard-to-video workflow acceptance test."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from axonflow.config.loader import load_global_config
from axonflow.engine import AxonFlowEngine
from axonflow.platform.store import PlatformStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_STORYBOARD_LIVE") != "1",
    reason="Set RUN_MINIMAX_STORYBOARD_LIVE=1 to use MiniMax image generation quota",
)


async def test_fictional_public_figure_football_scene_is_generated_and_disclosed() -> None:
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
            "text-to-video-generation",
            json.dumps(
                {
                    "description": (
                        "明显虚构的讽刺喜剧 AI 短片：唐纳德·特朗普身穿夸张的红白足球服，"
                        "在明亮体育场罚点球。他助跑大力射门，足球飞过横梁；他愣住，双手"
                        "抱头跪地，随后夸张地嚎啕大哭。镜头从全景跟拍推近到表情特写，"
                        "动作连续、电影光影。这不是现实事件。"
                    ),
                    "duration": 6,
                    "resolution": "768P",
                    "generation_backend": "storyboard",
                    "collect_resources": False,
                },
                ensure_ascii=False,
            ),
        )
    finally:
        await engine.stop()
        store.close()

    print(f"TEXT_TO_VIDEO_WORKFLOW_RESULT={result.to_dict()}")
    assert result.status == "completed", result.to_dict()
    final_video = result.output["composed_video"]
    assert final_video["ai_generated"] is True
    assert final_video["fictional_content"] is True
    assert final_video["disclosure_burned"] is True
    assert final_video["video_codec"] == "h264"
    assert final_video["audio_codec"] == "aac"
    assert 5_000 <= final_video["duration_ms"] <= 12_000
    assert result.output["generated_video"]["generation_backend"] == "storyboard"
    assert result.output["generated_video"]["shot_count"] == 4
    assert result.output["quality_report"]["verdict"] == "passed"
    assert result.output["registered_asset"]["status"] == "ready"
    assert Path(final_video["output_path"]).is_file()
