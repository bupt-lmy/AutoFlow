"""Opt-in acceptance test for title-driven official-source video editing."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from axonflow.config.loader import load_global_config
from axonflow.engine import AxonFlowEngine
from axonflow.platform.store import PlatformStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MOVIE_DISCOVERY_LIVE") != "1",
    reason="Set RUN_MOVIE_DISCOVERY_LIVE=1 for network and MiniMax acceptance",
)


async def test_movie_title_discovers_official_trailer_and_delivers_edit() -> None:
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
            "movie-title-video-edit",
            json.dumps(
                {
                    "title": "复仇者联盟3",
                    "description": (
                        "从官方预告片中选择灭霸、英雄集结和战斗动作最有视觉冲击力的片段，"
                        "排除片头片尾和纯文字画面，按原时间顺序组织。"
                    ),
                    "target_duration_seconds": 30,
                    "usage": "internal_demo",
                    "hard_subtitles": True,
                },
                ensure_ascii=False,
            ),
        )
    finally:
        await engine.stop()
        store.close()

    print(f"MOVIE_TITLE_WORKFLOW_RESULT={result.to_dict()}")
    assert result.status == "completed", result.to_dict()
    assert result.iterations == 15
    final_video = result.output["composed_video"]
    assert 29_000 <= final_video["duration_ms"] <= 30_500
    assert final_video["subtitles_burned"] is True
    assert result.output["quality_report"]["verdict"] == "passed"
    provenance = result.output["source_provenance"]
    assert provenance["authorization_basis"].startswith("official_public_promotional")
    assert provenance["selected_source"]["official_channel"] is True
    assert "marvel" in provenance["selected_source"]["channel"].lower()
    assert provenance["publication_allowed"] is False
    assert Path(final_video["output_path"]).is_file()
