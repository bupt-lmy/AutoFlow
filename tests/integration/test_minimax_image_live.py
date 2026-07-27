"""Opt-in live MiniMax image Agent integration test."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.minimax_media import MiniMaxImageAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolRegistry
from axonflow.tools.minimax_media import MiniMaxImageGenerateTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_LIVE") != "1",
    reason="Set RUN_MINIMAX_LIVE=1 to use the configured MiniMax Token Plan",
)


async def test_minimax_image_agent_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    store = PlatformStore(project_dir / "workspace" / "axonflow.db")
    registry = ToolRegistry()
    registry.register(
        MiniMaxImageGenerateTool(
            project_dir / "workspace" / "media" / "generated",
            credential_resolver=store.resolve_credential,
        )
    )
    config = AgentConfig(
        id="agent-minimax-image-generator",
        name="MiniMax 影视画面生成器",
        class_path="axonflow.agents.minimax_media.MiniMaxImageAgent",
        tools=["minimax_image_generate"],
        retry_limit=1,
        memory={"enabled": False},
        parameters={
            "minimax": {
                "model": "image-01",
                "credential_id": "cred-e29c18f8584a",
                "aspect_ratio": "16:9",
                "timeout": 180,
            }
        },
    )
    agent = MiniMaxImageAgent(config, Mock(), Mock(), registry)
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="live-test",
        receiver=agent.id,
        workflow_id="minimax-image-live",
        payload={
            "task": {
                "prompt": "电影感的未来城市清晨空镜，蓝金色调，无人物，无文字，高细节",
                "aspect_ratio": "16:9",
            }
        },
    )

    try:
        result = await agent.handle_message(message)
    finally:
        store.close()

    assert result["status"] == "success", result
    artifact = result["artifacts"][0]
    output_path = Path(artifact["uri"])
    assert output_path.is_file()
    assert output_path.stat().st_size > 1_000
    assert output_path.read_bytes()[:8].startswith((b"\xff\xd8\xff", b"\x89PNG"))
    print(f"MINIMAX_IMAGE_ARTIFACT={output_path}")
