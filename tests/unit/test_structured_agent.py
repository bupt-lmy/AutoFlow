"""Structured model-result Agent tests."""

from unittest.mock import AsyncMock

import pytest

from axonflow.agents.structured import StructuredResultAgent
from axonflow.config.models import AgentConfig
from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message, MessageType
from axonflow.json_utils import parse_json_object
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry


def _agent() -> StructuredResultAgent:
    return StructuredResultAgent(
        config=AgentConfig(
            id="gate",
            name="Gate",
            class_path="axonflow.agents.structured.StructuredResultAgent",
            parameters={
                "structured_result": {
                    "success_values": ["passed"],
                    "error_values": ["failed", "blocked"],
                    "promote_fields": ["requirements", "report_path"],
                }
            },
        ),
        message_bus=InMemoryMessageBus(),
        llm_gateway=LLMGateway(),
        tool_registry=ToolRegistry(),
    )


def _message() -> Message:
    return Message(
        sender="upstream",
        receiver="gate",
        type=MessageType.TASK_REQUEST,
        payload={"task": "verify"},
    )


def test_parse_json_object_accepts_fence_and_surrounding_text() -> None:
    assert parse_json_object("```json\n{\"done\": true}\n```") == {"done": True}
    assert parse_json_object("Decision: {\"done\": false} after review") == {"done": False}


@pytest.mark.asyncio
async def test_structured_agent_promotes_passed_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        BaseAgent,
        "handle_message",
        AsyncMock(
            return_value={
                "status": "success",
                "content": (
                    '{"verdict":"passed","requirements":[{"status":"passed"}],'
                    '"report_path":"docs/report.md"}'
                ),
            }
        ),
    )

    result = await _agent().handle_message(_message())

    assert result["status"] == "success"
    assert result["report_path"] == "docs/report.md"
    assert result["requirements"] == [{"status": "passed"}]


@pytest.mark.asyncio
async def test_structured_agent_turns_failed_verdict_into_workflow_error(monkeypatch) -> None:
    monkeypatch.setattr(
        BaseAgent,
        "handle_message",
        AsyncMock(
            return_value={
                "status": "success",
                "content": '{"verdict":"failed","summary":"Browser assertion failed"}',
            }
        ),
    )

    result = await _agent().handle_message(_message())

    assert result["status"] == "error"
    assert result["error"] == "Browser assertion failed"


@pytest.mark.asyncio
async def test_structured_agent_rejects_unstructured_success(monkeypatch) -> None:
    monkeypatch.setattr(
        BaseAgent,
        "handle_message",
        AsyncMock(return_value={"status": "success", "content": "looks good"}),
    )

    result = await _agent().handle_message(_message())

    assert result["status"] == "error"
    assert "valid JSON object" in result["error"]
