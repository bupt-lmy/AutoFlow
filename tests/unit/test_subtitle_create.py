"""Subtitle creation Tool and Agent tests."""

from __future__ import annotations

import json
from unittest.mock import Mock

from axonflow.agents.media import SubtitleAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolRegistry
from axonflow.tools.subtitle_create import SubtitleCreateTool


async def test_subtitle_tool_creates_timed_utf8_srt(tmp_path) -> None:
    tool = SubtitleCreateTool(tmp_path)

    result = await tool.execute(text="城市正在醒来。新的故事开始了！", duration_ms=12000)

    assert result.success is True
    output = json.loads(result.output or "{}")
    content = (tmp_path / output["output_path"].rsplit("/", 1)[-1]).read_text()
    assert output["cue_count"] == 2
    assert "00:00:00,500 -->" in content
    assert "城市正在醒来。" in content
    assert content.endswith("新的故事开始了！\n")


async def test_subtitle_agent_adds_artifact_to_manifest(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(SubtitleCreateTool(tmp_path))
    agent = SubtitleAgent(
        AgentConfig(id="subtitle", name="Subtitle", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="manifest",
        receiver=agent.id,
        workflow_id="subtitle-test",
        payload={"asset_manifest": {"narration": {"metadata": {"text": "清晨的城市正在醒来。"}}}},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success"
    assert result["asset_manifest"]["subtitle"]["uri"].endswith(".srt")
