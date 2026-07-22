"""Semantic video Agent unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from axonflow.agents.video_edit import (
    SourceTranscriptAgent,
    VideoHighlightScoringAgent,
    VideoHighlightSelectorAgent,
    VideoIntervalRefinerAgent,
    VideoTranscriptionAgent,
    _srt_cues,
)
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import Tool, ToolRegistry, ToolResult
from axonflow.tools.subtitle_create import SubtitleCreateTool


class NoSpeechTranscribeTool(Tool):
    name = "video_transcribe"
    description = "test no-speech response"

    async def execute(self, **_kwargs) -> ToolResult:
        return ToolResult(success=False, error="Whisper found no transcribable speech")


class MissingWhisperTranscribeTool(Tool):
    name = "video_transcribe"
    description = "test missing local Whisper response"

    async def execute(self, **_kwargs) -> ToolResult:
        return ToolResult(success=False, error="whisper-cli is not installed")


def test_srt_cues_parse_timestamps_and_multiline_text(tmp_path) -> None:
    subtitle = tmp_path / "source.srt"
    subtitle.write_text(
        "1\n00:00:01,250 --> 00:00:02,500\n第一行\n第二行\n",
        encoding="utf-8",
    )

    assert _srt_cues(str(subtitle)) == [{"start_ms": 1250, "end_ms": 2500, "text": "第一行 第二行"}]


async def test_selector_batches_long_video_and_performs_global_selection(tmp_path) -> None:
    scenes = []
    for index in range(4):
        frame = tmp_path / f"scene-{index}.jpg"
        frame.write_bytes(b"jpeg")
        scenes.append(
            {
                "id": f"scene-{index + 1:03d}",
                "start_ms": index * 1000,
                "end_ms": (index + 1) * 1000,
                "duration_ms": 1000,
                "keyframe_path": str(frame),
            }
        )
    gateway = Mock()
    gateway.chat = AsyncMock(
        side_effect=[
            SimpleNamespace(
                content=json.dumps(
                    {
                        "selected_scene_ids": ["scene-001"],
                        "reasons": {"scene-001": "first batch"},
                    }
                )
            ),
            SimpleNamespace(
                content=json.dumps(
                    {
                        "selected_scene_ids": ["scene-004"],
                        "reasons": {"scene-004": "second batch"},
                    }
                )
            ),
            SimpleNamespace(
                content=json.dumps(
                    {
                        "selected_scene_ids": ["scene-004"],
                        "reasons": {"scene-004": "global best"},
                        "summary": "selected across the full source",
                    }
                )
            ),
        ]
    )
    agent = VideoHighlightSelectorAgent(
        AgentConfig(
            id="selector",
            name="Selector",
            memory={"enabled": False},
            parameters={"max_visual_scenes": 2},
        ),
        Mock(),
        gateway,
        ToolRegistry(),
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="analysis",
            receiver=agent.id,
            workflow_id="long-video-test",
            payload={
                "source_path": "/source.mp4",
                "scenes": scenes,
                "description": "find the final scene",
                "target_duration_seconds": 1,
            },
        )
    )

    assert result["status"] == "success"
    assert result["selected_clips"][0]["scene_id"] == "scene-004"
    assert gateway.chat.await_count == 3


async def test_selector_retries_malformed_model_json(tmp_path) -> None:
    frame = tmp_path / "scene.jpg"
    frame.write_bytes(b"jpeg")
    gateway = Mock()
    gateway.chat = AsyncMock(
        side_effect=[
            SimpleNamespace(content="not-json"),
            SimpleNamespace(
                content=json.dumps(
                    {
                        "selected_scene_ids": ["scene-001"],
                        "reasons": {"scene-001": "valid retry"},
                    }
                )
            ),
        ]
    )
    agent = VideoHighlightSelectorAgent(
        AgentConfig(id="selector", name="Selector", memory={"enabled": False}),
        Mock(),
        gateway,
        ToolRegistry(),
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="analysis",
            receiver=agent.id,
            workflow_id="json-retry-test",
            payload={
                "source_path": "/source.mp4",
                "scenes": [
                    {
                        "id": "scene-001",
                        "start_ms": 0,
                        "end_ms": 1000,
                        "duration_ms": 1000,
                        "keyframe_path": str(frame),
                    }
                ],
                "description": "select action",
                "target_duration_seconds": 1,
            },
        )
    )

    assert result["status"] == "success"
    assert gateway.chat.await_count == 2


async def test_composite_scoring_keeps_silent_action_competitive(tmp_path) -> None:
    static_frame = tmp_path / "static.jpg"
    motion_frame = tmp_path / "motion.jpg"
    static_frame.write_bytes(b"jpeg-static")
    motion_frame.write_bytes(b"jpeg-motion")
    gateway = Mock()
    gateway.chat = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                {
                    "scene_scores": [
                        {
                            "scene_id": "scene-static",
                            "semantic_relevance": 0.4,
                            "action_confidence": 0.05,
                            "emotion_intensity": 0.1,
                            "aesthetic_quality": 0.5,
                            "dialogue_relevance": 0.0,
                            "reason": "静止准备画面",
                        },
                        {
                            "scene_id": "scene-motion",
                            "semantic_relevance": 0.9,
                            "action_confidence": 0.95,
                            "emotion_intensity": 0.7,
                            "aesthetic_quality": 0.7,
                            "dialogue_relevance": 0.0,
                            "reason": "无对白高速射门动作",
                        },
                    ]
                }
            )
        )
    )
    scorer = VideoHighlightScoringAgent(
        AgentConfig(id="scorer", name="Scorer", memory={"enabled": False}),
        Mock(),
        gateway,
        ToolRegistry(),
    )
    feature_payload = {
        "source_path": "/source.mp4",
        "description": "选择最精彩的高速射门动作",
        "target_duration_seconds": 2,
        "scenes": [
            {
                "id": "scene-static",
                "start_ms": 0,
                "end_ms": 2000,
                "sample_frames": [{"timestamp_ms": 1000, "path": str(static_frame)}],
                "features": {
                    "motion_intensity": 0.0,
                    "visual_change": 0.0,
                    "audio_impact": 0.0,
                    "freeze_ratio": 1.0,
                    "black_ratio": 0.0,
                },
            },
            {
                "id": "scene-motion",
                "start_ms": 2000,
                "end_ms": 4000,
                "sample_frames": [{"timestamp_ms": 3000, "path": str(motion_frame)}],
                "features": {
                    "motion_intensity": 0.9,
                    "visual_change": 0.8,
                    "audio_impact": 0.0,
                    "freeze_ratio": 0.0,
                    "black_ratio": 0.0,
                },
            },
        ],
    }
    result = await scorer.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="join",
            receiver=scorer.id,
            workflow_id="silent-action-score",
            payload={
                "agent-video-scene-features": feature_payload,
                "agent-source-transcript": {
                    "source_path": "/source.mp4",
                    "transcript_cues": [],
                },
            },
        )
    )

    assert result["status"] == "success"
    static, motion = result["scenes"]
    assert motion["dialogue_relevance"] == 0
    assert motion["highlight_score"] > static["highlight_score"]
    assert result["scoring_summary"]["ranked_scene_ids"][0] == "scene-motion"

    selector = VideoHighlightSelectorAgent(
        AgentConfig(id="selector", name="Selector", memory={"enabled": False}),
        Mock(),
        Mock(),
        ToolRegistry(),
    )
    selection = await selector.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="scorer",
            receiver=selector.id,
            payload=result,
        )
    )
    assert selection["selected_clips"][0]["scene_id"] == "scene-motion"


async def test_scoring_without_optional_key_uses_local_features_only(monkeypatch) -> None:
    monkeypatch.delenv("TEST_MINIMAX_API_KEY", raising=False)
    gateway = Mock()
    gateway.chat = AsyncMock(side_effect=AssertionError("LLM must not be called without a key"))
    scorer = VideoHighlightScoringAgent(
        AgentConfig(
            id="scorer",
            name="Scorer",
            model={
                "provider": "minimax",
                "name": "MiniMax-M3",
                "api_key_env": "TEST_MINIMAX_API_KEY",
            },
            memory={"enabled": False},
        ),
        Mock(),
        gateway,
        ToolRegistry(),
    )

    result = await scorer.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="join",
            receiver=scorer.id,
            workflow_id="local-score",
            payload={
                "description": "选择运动最强的片段",
                "source_path": "/source.mp4",
                "scenes": [
                    {
                        "id": "scene-motion",
                        "start_ms": 0,
                        "end_ms": 2000,
                        "features": {
                            "motion_intensity": 0.9,
                            "visual_change": 0.8,
                            "audio_impact": 0.2,
                        },
                    }
                ],
            },
        )
    )
    health = await scorer.check_health(timeout_seconds=1)

    assert result["status"] == "success"
    assert result["scenes"][0]["score_reason"] == "deterministic feature fallback"
    assert result["scoring_summary"]["batch_warnings"] == [
        "semantic scoring skipped: TEST_MINIMAX_API_KEY is not set"
    ]
    assert health["ready"] is True
    gateway.chat.assert_not_awaited()


def test_scoring_batches_never_exceed_minimax_image_limit() -> None:
    scenes = [
        {"id": f"scene-{index:03d}", "sample_frames": [{} for _ in range(5)]}
        for index in range(12)
    ]

    batches = VideoHighlightScoringAgent._scene_batches(
        scenes,
        max_scenes=5,
        max_images=20,
        max_frames_per_scene=4,
    )

    assert [len(batch) for batch in batches] == [5, 5, 2]
    assert all(
        sum(min(len(scene["sample_frames"]), 4) for scene in batch) <= 20
        for batch in batches
    )


def test_scoring_prefilter_limits_long_video_and_preserves_time_coverage() -> None:
    scenes = []
    for index in range(100):
        scenes.append(
            {
                "id": f"scene-{index:03d}",
                "start_ms": index * 1000,
                "end_ms": (index + 1) * 1000,
                "features": {
                    "motion_intensity": 1.0 if index == 50 else 0.01,
                    "visual_change": 0.5 if index == 50 else 0.01,
                    "audio_impact": 0,
                    "audio_energy": 0,
                    "freeze_ratio": 0,
                    "black_ratio": 0,
                },
            }
        )

    selected = VideoHighlightScoringAgent._prefilter_scenes(scenes, [], 30)
    selected_ids = {scene["id"] for scene in selected}

    assert len(selected) == 30
    assert "scene-000" in selected_ids
    assert "scene-050" in selected_ids
    assert "scene-099" in selected_ids


async def test_interval_refiner_cuts_around_internal_activity_peak() -> None:
    agent = VideoIntervalRefinerAgent(
        AgentConfig(
            id="refiner",
            name="Refiner",
            parameters={
                "refinement": {
                    "min_clip_ms": 800,
                    "max_clip_ms": 8000,
                    "pre_roll_ms": 350,
                    "post_roll_ms": 550,
                    "fps": 30,
                }
            },
            memory={"enabled": False},
        ),
        Mock(),
        Mock(),
        ToolRegistry(),
    )
    rows = []
    for timestamp in range(0, 10_000, 250):
        rows.append(
            {
                "timestamp_ms": timestamp,
                "frame_difference": 20 if 4000 <= timestamp <= 5000 else 0,
                "audio_rms_db": -120,
            }
        )
    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="selector",
            receiver=agent.id,
            payload={
                "source_path": "/source.mp4",
                "description": "pick the action peak",
                "target_duration_seconds": 2,
                "candidate_clips": [
                    {"scene_id": "scene-001", "start_ms": 0, "end_ms": 10_000}
                ],
                "scored_scenes": [
                    {
                        "id": "scene-001",
                        "start_ms": 0,
                        "end_ms": 10_000,
                        "highlight_score": 0.9,
                        "feature_samples": rows,
                    }
                ],
            },
        )
    )

    assert result["status"] == "success"
    clip = result["selected_clips"][0]
    assert 3500 <= clip["start_ms"] <= 4500
    assert 4500 <= clip["end_ms"] <= 5500
    assert clip["start_ms"] <= clip["peak_ms"] <= clip["end_ms"]
    assert result["refinement_report"]["actual_duration_ms"] == 2000
    assert result["refinement_report"]["duration_delta_ms"] == 0


async def test_interval_refiner_health_is_local_and_does_not_call_llm() -> None:
    gateway = Mock()
    gateway.chat = AsyncMock(side_effect=AssertionError("LLM must not be called"))
    agent = VideoIntervalRefinerAgent(
        AgentConfig(
            id="refiner",
            name="Refiner",
            parameters={
                "refinement": {"min_clip_ms": 800, "max_clip_ms": 8000, "fps": 30}
            },
            memory={"enabled": False},
        ),
        Mock(),
        gateway,
        ToolRegistry(),
    )

    health = await agent.check_health(timeout_seconds=1)

    assert health["state"] == "healthy"
    assert health["ready"] is True
    gateway.chat.assert_not_awaited()


async def test_source_transcript_degrades_when_whisper_is_not_installed() -> None:
    registry = ToolRegistry()
    registry.register(MissingWhisperTranscribeTool())
    agent = SourceTranscriptAgent(
        AgentConfig(id="source-transcript", name="Source transcript", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="scene-analysis",
            receiver=agent.id,
            payload={"source_path": "/silent.mp4", "scenes": []},
        )
    )

    assert result["status"] == "success"
    assert result["transcript_cues"] == []
    assert result["transcript_warning"] == "whisper-cli is not installed"


async def test_transcription_agent_falls_back_to_visual_captions_for_silent_clip(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.register(NoSpeechTranscribeTool())
    registry.register(SubtitleCreateTool(tmp_path))
    agent = VideoTranscriptionAgent(
        AgentConfig(id="transcriber", name="Transcriber", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="renderer",
            receiver=agent.id,
            payload={
                "composed_video": {"output_path": "/silent.mp4", "duration_ms": 2000},
                "selected_clips": [{"reason": "角色快速穿过走廊"}],
            },
        )
    )

    assert result["status"] == "success"
    assert result["subtitle"]["caption_source"] == "visual_selection_reason"
    assert "角色快速穿过走廊" in Path(result["subtitle"]["output_path"]).read_text()


async def test_transcription_agent_falls_back_when_whisper_is_not_installed(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(MissingWhisperTranscribeTool())
    registry.register(SubtitleCreateTool(tmp_path))
    agent = VideoTranscriptionAgent(
        AgentConfig(id="transcriber", name="Transcriber", memory={"enabled": False}),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender="renderer",
            receiver=agent.id,
            payload={
                "composed_video": {"output_path": "/silent.mp4", "duration_ms": 2000},
                "selected_clips": [{"reason": "运动测试片段"}],
            },
        )
    )

    assert result["status"] == "success"
    assert result["subtitle"]["caption_source"] == "visual_selection_reason"
    assert result["subtitle"]["transcript_warning"] == "whisper-cli is not installed"
