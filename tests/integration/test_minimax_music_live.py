"""Opt-in live MiniMax music Agent integration test."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.minimax_media import MiniMaxMusicAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolRegistry
from axonflow.tools.minimax_media import MiniMaxMusicGenerateTool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_LIVE") != "1",
    reason="Set RUN_MINIMAX_LIVE=1 to use the configured MiniMax Token Plan",
)


async def test_minimax_music_agent_live() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    store = PlatformStore(project_dir / "workspace" / "axonflow.db")
    registry = ToolRegistry()
    registry.register(
        MiniMaxMusicGenerateTool(
            project_dir / "workspace" / "media" / "generated",
            credential_resolver=store.resolve_credential,
        )
    )
    config = AgentConfig(
        id="agent-minimax-music-generator",
        name="MiniMax 影视配乐生成器",
        class_path="axonflow.agents.minimax_media.MiniMaxMusicAgent",
        tools=["minimax_music_generate"],
        retry_limit=1,
        memory={"enabled": False},
        parameters={
            "minimax": {
                "model": "music-2.6",
                "credential_id": "cred-e29c18f8584a",
                "timeout": 300,
            }
        },
    )
    agent = MiniMaxMusicAgent(config, Mock(), Mock(), registry)
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="live-test",
        receiver=agent.id,
        workflow_id="minimax-music-live",
        payload={
            "task": {
                "prompt": (
                    "电影感环境氛围，温暖克制，清晨未来城市纪录片背景配乐，轻柔钢琴与合成器，无人声"
                )
            }
        },
    )

    try:
        result = await agent.handle_message(message)
    finally:
        store.close()

    assert result["status"] == "success", result
    output_path = Path(result["artifacts"][0]["uri"])
    assert output_path.is_file()
    assert output_path.stat().st_size > 10_000
    probe = subprocess.run(
        [
            "/opt/homebrew/bin/ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    metadata = json.loads(probe.stdout)
    assert metadata["streams"][0]["codec_name"] == "mp3"
    assert float(metadata["format"]["duration"]) > 0
    print(f"MINIMAX_MUSIC_ARTIFACT={output_path}")
    print(f"MINIMAX_MUSIC_FFPROBE={json.dumps(metadata, ensure_ascii=False)}")
