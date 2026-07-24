"""Resource discovery and generated-video Agent tests."""

from __future__ import annotations

import json
from unittest.mock import Mock

from axonflow.agents.generated_video import (
    GeneratedVideoDisclosureAgent,
    GenerationResourceSearchAgent,
    StoryboardMotionRendererAgent,
)
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import Tool, ToolRegistry, ToolResult


class SearchFixtureTool(Tool):
    name = "web_search"
    description = "test search"

    async def execute(self, **_kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            output=json.dumps(
                [{"title": "Commons", "url": "https://commons.wikimedia.org/example"}]
            ),
        )


class FinalizeFixtureTool(Tool):
    name = "generated_video_finalize"
    description = "test finalizer"

    async def execute(self, **_kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": "/generated-final.mp4",
                    "media_type": "video/mp4",
                    "ai_generated": True,
                    "fictional_content": True,
                    "disclosure_burned": True,
                }
            ),
        )


class StoryboardFixtureTool(Tool):
    name = "storyboard_motion_render"
    description = "test storyboard renderer"

    async def execute(self, **kwargs) -> ToolResult:
        assert kwargs["image_paths"] == ["/shot-1.jpg", "/shot-2.jpg"]
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": "/storyboard.mp4",
                    "media_type": "video/mp4",
                    "generation_backend": "storyboard",
                    "shot_count": 2,
                }
            ),
        )


async def test_resource_search_preserves_plain_language_generation_request() -> None:
    registry = ToolRegistry()
    registry.register(SearchFixtureTool())
    agent = GenerationResourceSearchAgent(
        AgentConfig(id="search", name="Search", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="orchestrator",
            receiver=agent.id,
            payload={"task": "虚构的足球失球喜剧场景"},
        )
    )

    assert result["status"] == "success"
    assert result["task"] == "虚构的足球失球喜剧场景"
    assert len(result["resource_candidates"]) == 1
    assert result["requires_disclosure"] is True
    assert result["requested_backend"] == "storyboard"


async def test_storyboard_renderer_returns_generated_video_for_disclosure() -> None:
    registry = ToolRegistry()
    registry.register(StoryboardFixtureTool())
    agent = StoryboardMotionRendererAgent(
        AgentConfig(id="renderer", name="Renderer", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="storyboard",
            receiver=agent.id,
            payload={
                "storyboard_images": [
                    {"output_path": "/shot-1.jpg"},
                    {"output_path": "/shot-2.jpg"},
                ],
                "shot_duration_seconds": 2,
            },
        )
    )

    assert result["status"] == "success"
    assert result["generated_video"]["output_path"] == "/storyboard.mp4"
    assert result["generation_backend"] == "storyboard"
    assert result["requires_disclosure"] is True


async def test_disclosure_agent_returns_final_composed_video() -> None:
    registry = ToolRegistry()
    registry.register(FinalizeFixtureTool())
    agent = GeneratedVideoDisclosureAgent(
        AgentConfig(id="disclosure", name="Disclosure", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="generator",
            receiver=agent.id,
            payload={"generated_video": {"output_path": "/generated.mp4"}},
        )
    )

    assert result["status"] == "success"
    assert result["composed_video"]["disclosure_burned"] is True
    assert result["generation_backend"] == "hailuo"
    assert result["artifacts"][-1]["uri"] == "/generated-final.mp4"
