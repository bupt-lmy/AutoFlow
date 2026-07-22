"""Composition argument and Agent boundary tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from axonflow.agents.media import MediaComposerAgent, MediaQualityAgent
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolResult
from axonflow.tools.media_compose import MediaComposeTool


def _message(payload: dict) -> Message:
    return Message(
        type=MessageType.TASK_REQUEST,
        sender="orchestrator",
        receiver="media-agent",
        workflow_id="compose-test",
        payload=payload,
    )


def test_compose_arguments_include_motion_mix_and_delivery_codecs(tmp_path) -> None:
    arguments = MediaComposeTool.build_arguments(
        Path("image.jpg"),
        Path("voice.mp3"),
        Path("music.mp3"),
        tmp_path / "result.mp4",
        duration_seconds=12,
        width=1920,
        height=1080,
        fps=30,
        music_volume=0.16,
        narration_volume=1,
        narration_delay_ms=500,
    )

    joined = " ".join(arguments)
    assert arguments[0] == "ffmpeg"
    assert "zoompan=" in joined
    assert "amix=inputs=2" in joined
    assert "loudnorm=I=-16" in joined
    assert "libx264" in arguments
    assert "aac" in arguments
    assert arguments[arguments.index("-ar") + 1] == "48000"
    assert arguments[arguments.index("-ac") + 1] == "2"
    assert arguments[-1].endswith("result.mp4")


async def test_composer_agent_maps_manifest_to_tool() -> None:
    registry = Mock()
    registry.execute = AsyncMock(
        return_value=ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": "/composed/result.mp4",
                    "media_type": "video/mp4",
                    "duration_ms": 12000,
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                }
            ),
        )
    )
    agent = MediaComposerAgent(
        AgentConfig(id="composer", name="Composer", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )
    manifest = {
        "image": {"uri": "/generated/image.jpg"},
        "narration": {"uri": "/generated/voice.mp3"},
        "music": {"uri": "/generated/music.mp3"},
    }

    result = await agent.handle_message(_message({"asset_manifest": manifest}))

    assert result["status"] == "success"
    assert result["artifacts"][0]["media_type"] == "video/mp4"
    arguments = registry.execute.await_args.args[1]
    assert arguments["image_path"] == "/generated/image.jpg"
    assert arguments["music_volume"] == 0.16


async def test_quality_agent_promotes_passed_report() -> None:
    registry = Mock()
    registry.execute = AsyncMock(
        return_value=ToolResult(
            success=True,
            output='{"verdict":"passed","checks":{"duration":true}}',
        )
    )
    agent = MediaQualityAgent(
        AgentConfig(id="quality", name="Quality", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )
    composed = {
        "output_path": "/composed/result.mp4",
        "duration_ms": 12000,
        "width": 1920,
        "height": 1080,
    }

    result = await agent.handle_message(_message({"composed_video": composed}))

    assert result["status"] == "success"
    assert result["quality_report"]["verdict"] == "passed"
    assert registry.execute.await_args.args[0] == "media_quality_check"
