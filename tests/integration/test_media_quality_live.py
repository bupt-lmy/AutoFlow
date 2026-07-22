"""Opt-in live technical quality Agent test over the latest composed video."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.media import MediaQualityAgent
from axonflow.config.loader import load_agent_config
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolRegistry
from axonflow.tools.media_quality import MediaQualityCheckTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MEDIA_LIVE") != "1",
    reason="Set RUN_MEDIA_LIVE=1 after running the composition test",
)


async def test_media_quality_agent_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    composed_path = max(
        (project_dir / "workspace" / "media" / "composed").glob("composed-video-*.mp4"),
        key=lambda path: path.stat().st_mtime,
    )
    registry = ToolRegistry()
    registry.register(MediaQualityCheckTool())
    agent = MediaQualityAgent(
        load_agent_config(project_dir / "config" / "agents" / "media-quality.yaml"),
        Mock(),
        Mock(),
        registry,
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="live-test",
        receiver=agent.id,
        workflow_id="media-quality-live",
        payload={
            "composed_video": {
                "output_path": str(composed_path),
                "duration_ms": 12000,
                "width": 1920,
                "height": 1080,
                "has_subtitles": True,
            }
        },
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success", result
    assert result["quality_report"]["verdict"] == "passed"
    print(f"MEDIA_QUALITY_REPORT={result['quality_report']}")
