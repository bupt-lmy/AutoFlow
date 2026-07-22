"""AIP-lite protocol, local discovery, and runtime failover tests."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from axonflow.agents.discovered import DiscoveredAgent
from axonflow.config.models import AgentConfig, AxonFlowConfig, DiscoveryConfig, ModelConfig
from axonflow.core.agent import AgentRegistry, BaseAgent
from axonflow.core.message import Message, MessageType
from axonflow.core.protocol import DataItem, DelegationRequest, TaskCommand, TaskCommandType
from axonflow.core.workflow import FlatOrchestrator
from axonflow.discovery.local import LocalDiscoveryService
from axonflow.engine import AxonFlowEngine
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.platform.models import PlatformWorkflow, WorkflowNode
from axonflow.tools.base import ToolRegistry


def _config(agent_id: str, role: str, *, tools: list[str] | None = None) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        name=agent_id,
        role=role,
        tools=tools or [],
        model=ModelConfig(provider="openai", name="test"),
    )


def test_aip_lite_models_validate_content_and_commands() -> None:
    command = TaskCommand(
        session_id="session-1",
        task_id="task-1",
        command=TaskCommandType.START,
        sender_id="leader",
        data_items=[DataItem(type="text", text="Review this project")],
    )

    assert command.protocol_version == "aip-lite/0.1"
    assert command.data_items[0].text == "Review this project"
    with pytest.raises(ValidationError):
        DataItem(type="file")
    with pytest.raises(ValidationError):
        DiscoveryConfig(description="   ")


def test_local_discovery_applies_capability_constraints_and_ranking() -> None:
    reviewer = _config(
        "security-reviewer",
        "Review Python code for security vulnerabilities and provide a report.",
        tools=["file_read", "text_search"],
    )
    reviewer.skills = ["code-review"]
    writer = _config("writer", "Write product articles.", tools=["file_read"])
    service = LocalDiscoveryService([writer, reviewer])

    candidates = service.discover(
        DelegationRequest(
            description="Perform a Python security code review",
            required_skills=["code-review"],
            required_tools=["text_search"],
        )
    )

    assert [candidate.agent_id for candidate in candidates] == ["security-reviewer"]
    assert candidates[0].score > 0


def test_local_discovery_supports_explicit_agent_tags() -> None:
    local = _config("local-reviewer", "Review source code.")
    local.tags = ["trusted", "internal"]
    external = _config("external-reviewer", "Review source code.")
    service = LocalDiscoveryService([external, local])

    candidates = service.discover(
        DelegationRequest(description="Review source code", tags=["trusted"])
    )

    assert [candidate.agent_id for candidate in candidates] == ["local-reviewer"]


@pytest.mark.asyncio
async def test_discovered_agent_falls_back_after_error(monkeypatch) -> None:
    configs = [
        _config("preferred-coder", "Implement application code."),
        _config("reviewer", "Review code quality and security."),
    ]

    class StubAgent:
        def __init__(self, selected_id: str) -> None:
            self.selected_id = selected_id

        def set_context(self, workflow_id, context) -> None:
            pass

        async def _process_with_retry(self, message):
            if self.selected_id == "preferred-coder":
                return {"status": "error", "error": "candidate failed"}
            return {"status": "success", "content": "review complete"}

    def fake_create_agent(*, config, **kwargs):
        return StubAgent(config.parameters["discovered_template_id"])

    monkeypatch.setattr("axonflow.agents.discovered.create_agent", fake_create_agent)
    slot = DiscoveredAgent(
        config=_config("workflow--review-slot", "Runtime slot"),
        message_bus=InMemoryMessageBus(),
        llm_gateway=LLMGateway(),
        tool_registry=ToolRegistry(),
        candidate_configs=configs,
        discovery=DiscoveryConfig(description="Review code quality and security"),
        preferred_template_id="preferred-coder",
    )
    message = Message(
        sender="__orchestrator__",
        receiver=slot.id,
        type=MessageType.TASK_REQUEST,
        payload={"task": "Review the repository"},
        workflow_id="workflow-1",
        session_id="workflow-1",
        task_id="task-1",
    )

    result = await slot.handle_message(message)

    assert result["status"] == "success"
    assert result["discovery"]["selected_agent_id"] == "reviewer"
    assert result["discovery"]["previous_attempts"][0]["agent_id"] == "preferred-coder"
    assert result["task_result"]["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_discovered_agent_falls_back_after_timeout(monkeypatch) -> None:
    configs = [
        _config("slow-agent", "Analyze data."),
        _config("fast-agent", "Analyze data quickly."),
    ]

    class StubAgent:
        def __init__(self, selected_id: str) -> None:
            self.selected_id = selected_id

        def set_context(self, workflow_id, context) -> None:
            pass

        async def _process_with_retry(self, message):
            if self.selected_id == "slow-agent":
                await asyncio.sleep(0.05)
            return {"status": "success", "content": self.selected_id}

    def fake_create_agent(*, config, **kwargs):
        return StubAgent(config.parameters["discovered_template_id"])

    monkeypatch.setattr("axonflow.agents.discovered.create_agent", fake_create_agent)
    slot = DiscoveredAgent(
        config=_config("workflow--analysis-slot", "Runtime slot"),
        message_bus=InMemoryMessageBus(),
        llm_gateway=LLMGateway(),
        tool_registry=ToolRegistry(),
        candidate_configs=configs,
        discovery=DiscoveryConfig(description="Analyze data", timeout_seconds=0.01),
        preferred_template_id="slow-agent",
    )

    result = await slot.handle_message(
        Message(
            sender="__orchestrator__",
            receiver=slot.id,
            type=MessageType.TASK_REQUEST,
            payload={"task": "Analyze"},
            workflow_id="workflow-1",
        )
    )

    assert result["status"] == "success"
    assert result["discovery"]["selected_agent_id"] == "fast-agent"
    assert result["discovery"]["previous_attempts"][0]["status"] == "timeout"


@pytest.mark.asyncio
async def test_dynamic_slot_joins_and_completes_flat_workflow(monkeypatch) -> None:
    bus = InMemoryMessageBus()
    gateway = LLMGateway()
    tools = ToolRegistry()
    source_registry = AgentRegistry()
    candidate_config = _config(
        "security-reviewer",
        "Review Python code for security vulnerabilities.",
        tools=["file_read"],
    )
    source_registry.register(
        BaseAgent(
            config=candidate_config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=tools,
        )
    )

    class StubAgent:
        def set_context(self, workflow_id, context) -> None:
            pass

        async def _process_with_retry(self, message):
            return {"status": "success", "content": "security review complete"}

    monkeypatch.setattr(
        "axonflow.agents.discovered.create_agent",
        lambda **kwargs: StubAgent(),
    )
    engine = AxonFlowEngine(config=AxonFlowConfig())
    engine._message_bus = bus
    engine._llm_gateway = gateway
    engine._tool_registry = tools
    engine._agent_registry = source_registry

    platform = PlatformWorkflow(
        id="dynamic-flow",
        name="Dynamic flow",
        nodes=[
            WorkflowNode(
                id="review-slot",
                node_type="discovery",
                label="Dynamic reviewer",
                is_entry=True,
                config={
                    "discovery": {
                        "description": "Review Python code for security vulnerabilities",
                        "required_tools": ["file_read"],
                    },
                    "terminate_on_success": True,
                },
            )
        ],
    )
    runtime = platform.to_workflow_config()
    execution_registry, execution_agents = engine._create_execution_agents(runtime)
    orchestrator = FlatOrchestrator(
        config=runtime,
        agent_registry=execution_registry,
        message_bus=bus,
    )
    agent_task = asyncio.create_task(execution_agents[0].start())

    try:
        result = await asyncio.wait_for(
            orchestrator.execute("Review the project"),
            timeout=1,
        )
    finally:
        await execution_agents[0].stop()
        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)

    assert result.status == "completed"
    assert result.output["discovery"]["selected_agent_id"] == "security-reviewer"
    assert result.output["task_result"]["status"]["state"] == "completed"
