"""Opt-in live Codex CLI contract test.

Run with ``AXONFLOW_CODEX_LIVE=1 pytest -q tests/integration/test_codex_live.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from axonflow.agents.codex import CodexAgent
from axonflow.config.models import AgentConfig, ModelConfig
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry

pytestmark = pytest.mark.skipif(
    os.environ.get("AXONFLOW_CODEX_LIVE") != "1",
    reason="set AXONFLOW_CODEX_LIVE=1 to use the local Codex login",
)


async def test_live_codex_agent_accepts_a_workflow_envelope(tmp_path: Path) -> None:
    agent = CodexAgent(
        config=AgentConfig(
            id="live-codex",
            name="Live Codex",
            role="Acknowledge the coding request without modifying files.",
            agent_type="codex",
            model=ModelConfig(provider="codex", name="configured-default"),
            retry_limit=1,
            memory={"enabled": False},
            parameters={
                "codex": {
                    "working_directory": str(tmp_path),
                    "allowed_working_directories": [str(tmp_path)],
                    "sandbox": "read-only",
                    "timeout_seconds": 120,
                    "ephemeral": True,
                    "skip_git_repo_check": True,
                }
            },
        ),
        message_bus=InMemoryMessageBus(),
        llm_gateway=LLMGateway(),
        tool_registry=ToolRegistry(),
    )
    message = Message(
        sender="requirements",
        receiver=agent.id,
        type=MessageType.TASK_REQUEST,
        workflow_id="live-smoke",
        step_id="coding",
        session_id="live-session",
        task_id="live-task",
        payload={
            "task": (
                "Confirm that you received this upstream coding request. Do not inspect or modify "
                "files, do not run commands, and report success with no changed files or tests."
            ),
            "_protocol": {"version": "aip-lite/0.1"},
        },
    )

    result = await agent.handle_message(message)

    assert result["status"] == "success", result
    assert result["outcome_status"] == "success"
    assert result["files_changed"] == []
    assert result["codex"]["thread_id"]
