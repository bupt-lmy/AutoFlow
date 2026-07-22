"""Opt-in MiniMax-M3 visual scene selection and source clip render test."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from axonflow.agents.video_edit import VideoHighlightRendererAgent, VideoHighlightSelectorAgent
from axonflow.config.loader import load_agent_config
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMGateway
from axonflow.platform.store import PlatformStore
from axonflow.tools.base import ToolRegistry
from axonflow.tools.video_edit import HighlightRenderTool, VideoSceneDetectTool
from tests.integration.test_video_edit_tools_live import _source_video

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIMAX_LIVE") != "1",
    reason="Set RUN_MINIMAX_LIVE=1 to test MiniMax-M3 visual scene selection",
)


async def test_minimax_selects_described_scene_and_renders_source_clip(tmp_path) -> None:
    project_dir = Path(__file__).resolve().parents[2]
    source = tmp_path / "source.mp4"
    _source_video(source)
    detected_result = await VideoSceneDetectTool(tmp_path / "keyframes").execute(
        str(source), threshold=8
    )
    assert detected_result.success is True
    detected = json.loads(detected_result.output or "{}")

    store = PlatformStore(project_dir / "workspace" / "axonflow.db")
    selector = VideoHighlightSelectorAgent(
        load_agent_config(project_dir / "config" / "agents" / "video-highlight-selector.yaml"),
        Mock(),
        LLMGateway(credential_resolver=store.resolve_credential),
        ToolRegistry(),
    )
    selection_message = Message(
        type=MessageType.TASK_REQUEST,
        sender="scene-analysis",
        receiver=selector.id,
        workflow_id="visual-highlight-live",
        payload={
            **detected,
            "description": "只选择电视测试彩条画面，不要选择红色纯色画面或动态测试图。",
            "target_duration_seconds": 2,
        },
    )
    try:
        selection = await selector.handle_message(selection_message)
    finally:
        store.close()

    assert selection["status"] == "success", selection
    assert selection["selected_clips"][0]["scene_id"] == "scene-003"

    registry = ToolRegistry()
    registry.register(HighlightRenderTool(tmp_path / "highlights"))
    renderer = VideoHighlightRendererAgent(
        load_agent_config(project_dir / "config" / "agents" / "video-highlight-renderer.yaml"),
        Mock(),
        Mock(),
        registry,
    )
    rendered = await renderer.handle_message(
        Message(
            type=MessageType.TASK_REQUEST,
            sender=selector.id,
            receiver=renderer.id,
            workflow_id="visual-highlight-live",
            payload=selection,
        )
    )

    assert rendered["status"] == "success", rendered
    assert rendered["composed_video"]["duration_ms"] == 2000
    print(f"SEMANTIC_HIGHLIGHT_SELECTION={selection['selection']}")
    print(f"SEMANTIC_HIGHLIGHT_ARTIFACT={rendered['composed_video']['output_path']}")
