"""Live local subtitle Agent test."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.media import SubtitleAgent
from axonflow.config.loader import load_agent_config
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolRegistry
from axonflow.tools.subtitle_create import SubtitleCreateTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MEDIA_LIVE") != "1",
    reason="Set RUN_MEDIA_LIVE=1 to create a real subtitle sidecar",
)


async def test_subtitle_agent_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    registry = ToolRegistry()
    registry.register(SubtitleCreateTool(project_dir / "workspace" / "media" / "subtitles"))
    agent = SubtitleAgent(
        load_agent_config(project_dir / "config" / "agents" / "subtitle-generator.yaml"),
        Mock(),
        Mock(),
        registry,
    )
    text = "当第一缕晨光吻上玻璃幕墙，整座城市缓缓睁开双眼。新的一天，从这里开始。"
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="live-test",
        receiver=agent.id,
        workflow_id="subtitle-live",
        payload={"asset_manifest": {"narration": {"metadata": {"text": text}}}},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success", result
    output = Path(result["subtitle"]["output_path"])
    assert output.is_file() and "城市" in output.read_text(encoding="utf-8")
    print(f"SUBTITLE_ARTIFACT={output}")
