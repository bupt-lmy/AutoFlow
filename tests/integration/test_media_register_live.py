"""Live local registration test for the latest quality-approved composition."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.media import MediaAssetRegisterAgent
from axonflow.config.loader import load_agent_config
from axonflow.core.message import Message, MessageType
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolRegistry
from axonflow.tools.media_register import MediaRegisterTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MEDIA_LIVE") != "1",
    reason="Set RUN_MEDIA_LIVE=1 after the quality test",
)


async def test_media_asset_register_agent_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    composed = max(
        (project_dir / "workspace" / "media" / "composed").glob("*.mp4"),
        key=lambda path: path.stat().st_mtime,
    )
    store = PlatformStore(project_dir / "workspace" / "axonflow.db")
    registry = ToolRegistry()
    registry.register(MediaRegisterTool(store))
    agent = MediaAssetRegisterAgent(
        load_agent_config(project_dir / "config" / "agents" / "media-asset-register.yaml"),
        Mock(),
        Mock(),
        registry,
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="quality",
        receiver=agent.id,
        workflow_id="register-live",
        payload={
            "quality_report": {"verdict": "passed"},
            "composed_video": {"output_path": str(composed)},
        },
    )

    try:
        result = await agent.handle_message(message)
        stored = store.get_media_asset(result.get("registered_asset", {}).get("id", ""))
    finally:
        store.close()

    assert result["status"] == "success", result
    assert stored is not None and stored.status.value == "ready"
    print(f"REGISTERED_MEDIA_ASSET={result['registered_asset']['id']}")
