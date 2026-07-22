"""Media asset registration Tool and Agent tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

from axonflow.agents.media import MediaAssetRegisterAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolRegistry
from axonflow.tools.media_probe import MediaProbeTool
from axonflow.tools.media_register import MediaRegisterTool


async def test_media_register_tool_persists_checksum_and_probe_metadata(
    tmp_path, monkeypatch
) -> None:
    video = tmp_path / "result.mp4"
    video.write_bytes(b"fake-video-data")
    monkeypatch.setattr(
        MediaProbeTool,
        "execute",
        AsyncMock(
            return_value=Mock(
                success=True,
                output=json.dumps(
                    {
                        "duration_ms": 12000,
                        "width": 1920,
                        "height": 1080,
                        "fps": 30,
                        "video_codec": "h264",
                        "audio_codec": "aac",
                        "sample_rate": 48000,
                        "channels": 2,
                    }
                ),
            )
        ),
    )
    store = PlatformStore(tmp_path / "axonflow.db")
    tool = MediaRegisterTool(store)

    try:
        result = await tool.execute(path=str(video))
        asset = json.loads(result.output or "{}")
        stored = store.get_media_asset(asset["id"])
    finally:
        store.close()

    assert result.success is True
    assert stored is not None
    assert stored.checksum_sha256 and len(stored.checksum_sha256) == 64
    assert stored.duration_ms == 12000


async def test_register_agent_requires_passed_quality() -> None:
    registry = ToolRegistry()
    agent = MediaAssetRegisterAgent(
        AgentConfig(id="register", name="Register", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="quality",
        receiver=agent.id,
        workflow_id="register-test",
        payload={"quality_report": {"verdict": "failed"}},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "error"
    assert "quality-approved" in result["error"]
