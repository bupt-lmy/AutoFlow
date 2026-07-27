"""Opt-in live FFmpeg composition test using generated MiniMax assets."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.media import MediaComposerAgent
from axonflow.config.loader import load_agent_config
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolRegistry
from axonflow.tools.media_compose import MediaComposeTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MEDIA_LIVE") != "1",
    reason="Set RUN_MEDIA_LIVE=1 after running the MiniMax generator tests",
)


async def test_media_composer_agent_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    generated = project_dir / "workspace" / "media" / "generated"
    manifest = {
        "image": {
            "uri": str(
                max(generated.glob("minimax-image-*"), key=lambda path: path.stat().st_mtime)
            )
        },
        "narration": {
            "uri": str(
                max(generated.glob("minimax-speech-*"), key=lambda path: path.stat().st_mtime)
            )
        },
        "music": {
            "uri": str(
                max(generated.glob("minimax-music-*"), key=lambda path: path.stat().st_mtime)
            )
        },
        "subtitle": {
            "uri": str(
                max(
                    (project_dir / "workspace" / "media" / "subtitles").glob("*.srt"),
                    key=lambda path: path.stat().st_mtime,
                )
            )
        },
    }
    registry = ToolRegistry()
    registry.register(MediaComposeTool(project_dir / "workspace" / "media" / "composed"))
    agent = MediaComposerAgent(
        load_agent_config(project_dir / "config" / "agents" / "media-composer.yaml"),
        Mock(),
        Mock(),
        registry,
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="live-test",
        receiver=agent.id,
        workflow_id="media-composer-live",
        payload={"asset_manifest": manifest},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success", result
    output = Path(result["composed_video"]["output_path"])
    assert output.is_file() and output.stat().st_size > 100_000
    print(f"COMPOSED_VIDEO_ARTIFACT={output}")
