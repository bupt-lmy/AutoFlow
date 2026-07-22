"""Deterministic media boundary Agent tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

from axonflow.agents.media import MediaAssetManifestAgent, MediaInspectorAgent, MediaRendererAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolResult


def _agent(agent_class, tool_registry):
    return agent_class(
        config=AgentConfig(id="media-agent", name="Media", memory={"enabled": False}),
        message_bus=Mock(),
        llm_gateway=Mock(),
        tool_registry=tool_registry,
    )


def _message(payload: dict) -> Message:
    return Message(
        type=MessageType.TASK_REQUEST,
        sender="orchestrator",
        receiver="media-agent",
        workflow_id="video-edit-mvp",
        payload=payload,
    )


async def test_inspector_probes_all_assets_and_preserves_request_fields() -> None:
    registry = Mock()
    registry.execute = AsyncMock(
        side_effect=[
            ToolResult(success=True, output='{"duration_ms":1000,"width":1920,"height":1080}'),
            ToolResult(success=True, output='{"duration_ms":2000,"width":1280,"height":720}'),
        ]
    )
    agent = _agent(MediaInspectorAgent, registry)
    request = {
        "assets": {"a1": "/media/a.mp4", "a2": "/media/b.mp4"},
        "output_path": "/renders/result.mp4",
        "target": {"width": 1080, "height": 1920},
    }

    result = await agent.handle_message(_message({"task": json.dumps(request)}))

    assert result["status"] == "success"
    assert result["output_path"] == request["output_path"]
    assert set(result["probe_results"]) == {"a1", "a2"}
    assert registry.execute.await_count == 2


async def test_renderer_returns_structured_artifact() -> None:
    registry = Mock()
    registry.execute = AsyncMock(
        return_value=ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": "/renders/result.mp4",
                    "duration_ms": 1000,
                    "size_bytes": 123,
                }
            ),
        )
    )
    agent = _agent(MediaRendererAgent, registry)

    result = await agent.handle_message(
        _message(
            {
                "timeline": {"version": "1.0"},
                "assets": {"a1": "/media/a.mp4"},
                "output_path": "/renders/result.mp4",
            }
        )
    )

    assert result["status"] == "success"
    assert result["artifacts"][0]["uri"] == "/renders/result.mp4"
    assert result["artifacts"][0]["media_type"] == "video/mp4"


async def test_asset_manifest_collects_all_generation_branches() -> None:
    agent = _agent(MediaAssetManifestAgent, Mock())
    payload = {
        agent_id: {
            "status": "success",
            "artifacts": [
                {
                    "type": "file",
                    "uri": f"/generated/{category}",
                    "media_type": "application/octet-stream",
                }
            ],
        }
        for agent_id, category in MediaAssetManifestAgent.required_agents.items()
    }

    result = await agent.handle_message(_message(payload))

    assert result["status"] == "success"
    assert set(result["asset_manifest"]) == {"image", "narration", "music"}
    assert len(result["artifacts"]) == 3
