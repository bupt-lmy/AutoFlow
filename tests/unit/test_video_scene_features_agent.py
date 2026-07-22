"""Multi-frame feature Agent tests."""

from __future__ import annotations

import json
from unittest.mock import Mock

from axonflow.agents.video_edit import VideoSceneFeatureAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import Tool, ToolRegistry, ToolResult


class FeatureFixtureTool(Tool):
    name = "video_scene_features"
    description = "feature fixture"

    async def execute(self, **kwargs) -> ToolResult:
        assert kwargs["samples_per_scene"] == 5
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "source_path": kwargs["source_path"],
                    "scenes": [{**kwargs["scenes"][0], "features": {"motion_intensity": 0.8}}],
                    "feature_summary": {"scene_count": 1},
                }
            ),
        )


async def test_feature_agent_preserves_edit_request() -> None:
    registry = ToolRegistry()
    registry.register(FeatureFixtureTool())
    agent = VideoSceneFeatureAgent(
        AgentConfig(
            id="features",
            name="Features",
            parameters={"features": {"samples_per_scene": 5}},
            memory={"enabled": False},
        ),
        Mock(),
        Mock(),
        registry,
    )
    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="scene-analysis",
            receiver=agent.id,
            payload={
                "source_path": "/source.mp4",
                "scenes": [{"id": "scene-001", "start_ms": 0, "end_ms": 1000}],
                "description": "silent action",
                "target_duration_seconds": 12,
            },
        )
    )

    assert result["status"] == "success"
    assert result["scenes"][0]["features"]["motion_intensity"] == 0.8
    assert result["description"] == "silent action"
    assert result["target_duration_seconds"] == 12
