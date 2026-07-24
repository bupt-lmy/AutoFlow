"""Opt-in local aggregation test over real generated MiniMax artifacts."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.media import MediaAssetManifestAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolRegistry

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_LIVE") != "1",
    reason="Set RUN_MINIMAX_LIVE=1 after running the three MiniMax generators",
)


async def test_manifest_agent_collects_real_generated_artifacts() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    generated_dir = project_dir / "workspace" / "media" / "generated"
    paths = {
        "agent-minimax-image-generator": max(generated_dir.glob("minimax-image-*")),
        "agent-minimax-narration-generator": max(generated_dir.glob("minimax-speech-*")),
        "agent-minimax-music-generator": max(generated_dir.glob("minimax-music-*")),
    }
    payload = {
        agent_id: {
            "status": "success",
            "artifacts": [
                {
                    "type": "file",
                    "name": path.name,
                    "uri": str(path),
                    "media_type": "image/jpeg" if "image" in agent_id else "audio/mpeg",
                }
            ],
        }
        for agent_id, path in paths.items()
    }
    agent = MediaAssetManifestAgent(
        AgentConfig(id="agent-media-asset-manifest", name="Manifest", retry_limit=1),
        Mock(),
        Mock(),
        ToolRegistry(),
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="join",
        receiver=agent.id,
        workflow_id="manifest-live",
        payload=payload,
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success", result
    assert set(result["asset_manifest"]) == {"image", "narration", "music"}
    assert all(Path(item["uri"]).is_file() for item in result["artifacts"])
    print(f"MINIMAX_ASSET_MANIFEST={result['asset_manifest']}")
