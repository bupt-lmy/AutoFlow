"""Opt-in live MiniMax creative brief planner Agent test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from axonflow.agents.structured import StructuredResultAgent
from axonflow.config.loader import load_agent_config
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolRegistry

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_LIVE") != "1",
    reason="Set RUN_MINIMAX_LIVE=1 to use the configured MiniMax Token Plan",
)


async def test_minimax_creative_brief_planner_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    store = PlatformStore(project_dir / "workspace" / "axonflow.db")
    config = load_agent_config(
        project_dir / "config" / "agents" / "minimax-creative-brief-planner.yaml"
    )
    agent = StructuredResultAgent(
        config=config,
        message_bus=InMemoryMessageBus(),
        llm_gateway=LLMGateway(credential_resolver=store.resolve_credential),
        tool_registry=ToolRegistry(),
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="live-test",
        receiver=agent.id,
        workflow_id="minimax-brief-planner-live",
        payload={"task": "制作一条关于未来城市清晨苏醒的温暖电影感短片。"},
    )

    try:
        result = await agent.handle_message(message)
    finally:
        store.close()

    assert result["status"] == "success", result
    assert isinstance(result["image_prompt"], str) and result["image_prompt"]
    assert isinstance(result["narration"], str) and result["narration"]
    assert isinstance(result["music_prompt"], str) and result["music_prompt"]
    print(f"MINIMAX_BRIEF_RESULT={result['structured']}")
