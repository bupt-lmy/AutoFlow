"""Tests for MiniMax media-generation tools and Agents."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, Mock

from axonflow.agents.minimax_media import (
    MiniMaxImageAgent,
    MiniMaxMusicAgent,
    MiniMaxNarrationAgent,
    MiniMaxStoryboardAgent,
)
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import ToolRegistry
from axonflow.tools.minimax_media import (
    MiniMaxImageGenerateTool,
    MiniMaxMusicGenerateTool,
    MiniMaxSpeechGenerateTool,
    MiniMaxVideoGenerateTool,
)


def _jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0test-image\xff\xd9"


async def test_image_tool_resolves_credential_and_writes_image(tmp_path) -> None:
    resolver = Mock(return_value={"secret": "test-secret"})
    tool = MiniMaxImageGenerateTool(tmp_path, credential_resolver=resolver)
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "data": {"image_base64": [base64.b64encode(_jpeg_bytes()).decode()]},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
    )

    result = await tool.execute(
        prompt="cinematic sunrise",
        aspect_ratio="16:9",
        output_name="shot-001.png",
        credential_id="credential-1",
    )

    assert result.success is True
    output = json.loads(result.output or "{}")
    assert output["media_type"] == "image/jpeg"
    assert output["output_path"].endswith("shot-001.jpg")
    assert (tmp_path / "shot-001.jpg").read_bytes() == _jpeg_bytes()
    resolver.assert_called_once_with("credential-1")
    request = tool._post_json.await_args.args
    assert request[0] == "/image_generation"
    assert request[1] == "test-secret"
    assert request[2]["response_format"] == "base64"


async def test_image_tool_rejects_path_traversal_without_calling_api(tmp_path) -> None:
    tool = MiniMaxImageGenerateTool(tmp_path, credential_resolver=lambda _id: {"secret": "x"})
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": {"image_base64": [base64.b64encode(_jpeg_bytes()).decode()]}}
    )

    result = await tool.execute(
        prompt="shot",
        output_name="../escape.jpg",
        credential_id="credential-1",
    )

    assert result.success is False
    assert "without directories" in (result.error or "")


async def test_image_agent_returns_file_artifact(tmp_path) -> None:
    registry = ToolRegistry()
    tool = MiniMaxImageGenerateTool(tmp_path, credential_resolver=lambda _id: {"secret": "x"})
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": {"image_base64": [base64.b64encode(_jpeg_bytes()).decode()]}}
    )
    registry.register(tool)
    config = AgentConfig(
        id="image-agent",
        name="Image Agent",
        class_path="axonflow.agents.minimax_media.MiniMaxImageAgent",
        tools=["minimax_image_generate"],
        parameters={"minimax": {"credential_id": "credential-1"}},
        memory={"enabled": False},
    )
    agent = MiniMaxImageAgent(config, Mock(), Mock(), registry)
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="orchestrator",
        receiver="image-agent",
        workflow_id="test-image",
        payload={"task": {"prompt": "cinematic city", "output_name": "city.jpg"}},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success"
    assert result["artifacts"][0]["media_type"] == "image/jpeg"
    assert result["artifacts"][0]["uri"].endswith("city.jpg")


async def test_storyboard_agent_generates_every_planned_shot(tmp_path) -> None:
    registry = ToolRegistry()
    tool = MiniMaxImageGenerateTool(tmp_path, credential_resolver=lambda _id: {"secret": "x"})
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": {"image_base64": [base64.b64encode(_jpeg_bytes()).decode()]}}
    )
    registry.register(tool)
    config = AgentConfig(
        id="storyboard-agent",
        name="Storyboard Agent",
        tools=["minimax_image_generate"],
        parameters={"minimax": {"credential_id": "credential-1"}},
        memory={"enabled": False},
    )
    agent = MiniMaxStoryboardAgent(config, Mock(), Mock(), registry)

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="orchestrator",
            receiver=agent.id,
            payload={
                "shot_prompts": ["player runs toward ball", "player misses the goal"],
                "shot_duration_seconds": 1.5,
                "duration": 6,
            },
        )
    )

    assert result["status"] == "success"
    assert result["generation_backend"] == "storyboard"
    assert len(result["storyboard_images"]) == 2
    assert [image["shot_index"] for image in result["storyboard_images"]] == [1, 2]
    assert result["shot_duration_seconds"] == 3.2
    assert len(result["artifacts"]) == 2
    assert tool._post_json.await_count == 2


async def test_speech_tool_writes_mp3_and_preserves_metadata(tmp_path) -> None:
    resolver = Mock(return_value={"secret": "test-secret"})
    tool = MiniMaxSpeechGenerateTool(tmp_path, credential_resolver=resolver)
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "data": {"audio": b"ID3test-audio".hex()},
            "extra_info": {"audio_length": 1250, "audio_sample_rate": 32000},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
    )

    result = await tool.execute(
        text="城市正在醒来。",
        output_name="narration.wav",
        credential_id="credential-1",
    )

    assert result.success is True
    output = json.loads(result.output or "{}")
    assert output["output_path"].endswith("narration.mp3")
    assert output["duration_ms"] == 1250
    assert (tmp_path / "narration.mp3").read_bytes() == b"ID3test-audio"
    request = tool._post_json.await_args.args
    assert request[0] == "/t2a_v2"
    assert request[2]["voice_setting"]["voice_id"] == "male-qn-jingying"
    assert request[2]["audio_setting"]["format"] == "mp3"


async def test_narration_agent_returns_audio_artifact(tmp_path) -> None:
    registry = ToolRegistry()
    tool = MiniMaxSpeechGenerateTool(tmp_path, credential_resolver=lambda _id: {"secret": "x"})
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": {"audio": b"ID3test-audio".hex()}}
    )
    registry.register(tool)
    config = AgentConfig(
        id="narration-agent",
        name="Narration Agent",
        class_path="axonflow.agents.minimax_media.MiniMaxNarrationAgent",
        tools=["minimax_speech_generate"],
        parameters={"minimax": {"credential_id": "credential-1"}},
        memory={"enabled": False},
    )
    agent = MiniMaxNarrationAgent(config, Mock(), Mock(), registry)
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="orchestrator",
        receiver="narration-agent",
        workflow_id="test-narration",
        payload={"task": {"text": "清晨的城市正在醒来。", "output_name": "voice.mp3"}},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success"
    assert result["artifacts"][0]["media_type"] == "audio/mpeg"
    assert result["artifacts"][0]["uri"].endswith("voice.mp3")


async def test_music_tool_writes_instrumental_mp3(tmp_path) -> None:
    tool = MiniMaxMusicGenerateTool(
        tmp_path, credential_resolver=lambda _id: {"secret": "test-secret"}
    )
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "data": {"audio": b"ID3test-music".hex(), "status": 2},
            "extra_info": {
                "music_duration": 25364,
                "music_sample_rate": 44100,
                "music_channel": 2,
                "bitrate": 256000,
            },
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
    )

    result = await tool.execute(
        prompt="cinematic ambient documentary background music",
        output_name="score.wav",
        credential_id="credential-1",
    )

    assert result.success is True
    output = json.loads(result.output or "{}")
    assert output["output_path"].endswith("score.mp3")
    assert output["duration_ms"] == 25364
    assert output["channels"] == 2
    request = tool._post_json.await_args.args
    assert request[0] == "/music_generation"
    assert request[2]["is_instrumental"] is True
    assert request[2]["output_format"] == "hex"
    assert request[2]["audio_setting"]["sample_rate"] == 44100


async def test_music_agent_returns_audio_artifact(tmp_path) -> None:
    registry = ToolRegistry()
    tool = MiniMaxMusicGenerateTool(tmp_path, credential_resolver=lambda _id: {"secret": "x"})
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": {"audio": b"ID3test-music".hex()}}
    )
    registry.register(tool)
    config = AgentConfig(
        id="music-agent",
        name="Music Agent",
        class_path="axonflow.agents.minimax_media.MiniMaxMusicAgent",
        tools=["minimax_music_generate"],
        parameters={"minimax": {"credential_id": "credential-1"}},
        memory={"enabled": False},
    )
    agent = MiniMaxMusicAgent(config, Mock(), Mock(), registry)
    message = Message(
        type=MessageType.TASK_REQUEST,
        sender="orchestrator",
        receiver="music-agent",
        workflow_id="test-music",
        payload={"task": {"prompt": "gentle cinematic ambient background music"}},
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success"
    assert result["artifacts"][0]["media_type"] == "audio/mpeg"
    assert result["artifacts"][0]["uri"].endswith(".mp3")


async def test_video_tool_polls_downloads_and_writes_mp4(tmp_path) -> None:
    tool = MiniMaxVideoGenerateTool(
        tmp_path, credential_resolver=lambda _id: {"secret": "test-secret"}
    )
    tool._post_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "task_id": "task-123",
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
    )
    tool._get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"task_id": "task-123", "status": "Processing"},
            {"task_id": "task-123", "status": "Success", "file_id": "file-456"},
            {
                "file": {"file_id": "file-456", "download_url": "https://cdn.test/video.mp4"},
                "base_resp": {"status_code": 0},
            },
        ]
    )
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"generated-video"
    tool._download_bytes = AsyncMock(return_value=mp4)  # type: ignore[method-assign]

    result = await tool.execute(
        prompt="A fictional football player misses a goal.",
        duration=6,
        resolution="1080P",
        credential_id="credential-1",
        output_name="generated.mp4",
        poll_interval=0.001,
    )

    assert result.success is True, result.error
    output = json.loads(result.output or "{}")
    assert output["task_id"] == "task-123"
    assert output["file_id"] == "file-456"
    assert output["aigc_watermark"] is True
    assert (tmp_path / "generated.mp4").read_bytes() == mp4
    request = tool._post_json.await_args.args
    assert request[0] == "/video_generation"
    assert request[2]["aigc_watermark"] is True
    assert request[2]["model"] == "MiniMax-Hailuo-2.3"


async def test_video_tool_rejects_unsupported_duration_resolution_pair(tmp_path) -> None:
    tool = MiniMaxVideoGenerateTool(tmp_path)
    tool._post_json = AsyncMock()  # type: ignore[method-assign]

    result = await tool.execute(prompt="shot", duration=10, resolution="1080P")

    assert result.success is False
    assert "10-second video supports 768P only" in (result.error or "")
    tool._post_json.assert_not_awaited()
