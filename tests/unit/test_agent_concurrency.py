"""Bounded Agent concurrency and workflow-isolation tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import yaml

from axonflow.api.routes import agents as agent_routes
from axonflow.config.models import AgentConfig
from axonflow.core.agent import AgentRegistry, BaseAgent
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry

_TEST_TIMEOUT_SECONDS = 5


class BlockingAgent(BaseAgent):
    """Test Agent that exposes when each message enters its handler."""

    def __init__(self, config: AgentConfig, bus: InMemoryMessageBus) -> None:
        super().__init__(config, bus, LLMGateway(), ToolRegistry())
        self.release = asyncio.Event()
        self.started: dict[str, asyncio.Event] = {}
        self.active_handlers = 0
        self.max_active_handlers = 0

    async def handle_message(self, message: Message) -> dict[str, Any]:
        job = str(message.payload["job"])
        self.active_handlers += 1
        self.max_active_handlers = max(self.max_active_handlers, self.active_handlers)
        self.started.setdefault(job, asyncio.Event()).set()
        try:
            await self.release.wait()
        finally:
            self.active_handlers -= 1
        return {"status": "success", "content": job}


def _message(job: str, workflow_id: str) -> Message:
    return Message(
        sender="caller",
        receiver="worker",
        type=MessageType.TASK_REQUEST,
        payload={"job": job},
        workflow_id=workflow_id,
        task_id=job,
    )


async def _wait_for_response(bus: InMemoryMessageBus) -> Message:
    response = await bus.receive("caller", block_ms=_TEST_TIMEOUT_SECONDS * 1000)
    assert response is not None
    return response


async def _stop_agent(agent: BaseAgent, listener: asyncio.Task[None]) -> None:
    await agent.stop()
    listener.cancel()
    await asyncio.gather(listener, return_exceptions=True)


async def test_agent_runs_different_workflows_concurrently() -> None:
    bus = InMemoryMessageBus()
    agent = BlockingAgent(
        AgentConfig(id="worker", name="Worker", max_concurrent=2),
        bus,
    )
    listener = asyncio.create_task(agent.start())

    await bus.send(_message("job-a", "workflow-a"))
    await bus.send(_message("job-b", "workflow-b"))
    await asyncio.wait_for(
        agent.started.setdefault("job-a", asyncio.Event()).wait(),
        _TEST_TIMEOUT_SECONDS,
    )
    await asyncio.wait_for(
        agent.started.setdefault("job-b", asyncio.Event()).wait(),
        _TEST_TIMEOUT_SECONDS,
    )

    status = agent.runtime_status()
    assert agent.max_active_handlers == 2
    assert status["active_tasks"] == 2
    assert status["active_workflows"] == ["workflow-a", "workflow-b"]

    agent.release.set()
    responses = [await _wait_for_response(bus), await _wait_for_response(bus)]
    assert {response.task_id for response in responses} == {"job-a", "job-b"}
    await _stop_agent(agent, listener)


async def test_agent_serializes_messages_from_the_same_workflow() -> None:
    bus = InMemoryMessageBus()
    agent = BlockingAgent(
        AgentConfig(id="worker", name="Worker", max_concurrent=2),
        bus,
    )
    listener = asyncio.create_task(agent.start())

    await bus.send(_message("first", "workflow-a"))
    await bus.send(_message("second", "workflow-a"))
    await asyncio.wait_for(
        agent.started.setdefault("first", asyncio.Event()).wait(),
        _TEST_TIMEOUT_SECONDS,
    )
    await asyncio.sleep(0.05)

    assert not agent.started.setdefault("second", asyncio.Event()).is_set()
    assert agent.runtime_status()["queued_tasks"] == 1

    agent.release.set()
    await asyncio.wait_for(agent.started["second"].wait(), _TEST_TIMEOUT_SECONDS)
    await _wait_for_response(bus)
    await _wait_for_response(bus)
    assert agent.max_active_handlers == 1
    await _stop_agent(agent, listener)


async def test_agent_does_not_receive_past_its_concurrency_limit() -> None:
    bus = InMemoryMessageBus()
    agent = BlockingAgent(
        AgentConfig(id="worker", name="Worker", max_concurrent=2),
        bus,
    )
    listener = asyncio.create_task(agent.start())

    for index in range(3):
        await bus.send(_message(f"job-{index}", f"workflow-{index}"))
    await asyncio.wait_for(
        agent.started.setdefault("job-0", asyncio.Event()).wait(),
        _TEST_TIMEOUT_SECONDS,
    )
    await asyncio.wait_for(
        agent.started.setdefault("job-1", asyncio.Event()).wait(),
        _TEST_TIMEOUT_SECONDS,
    )
    await asyncio.sleep(0.05)

    assert not agent.started.setdefault("job-2", asyncio.Event()).is_set()
    assert await bus.get_queue_depth("worker") == 1

    agent.release.set()
    await asyncio.wait_for(agent.started["job-2"].wait(), _TEST_TIMEOUT_SECONDS)
    for _ in range(3):
        await _wait_for_response(bus)
    assert agent.max_active_handlers == 2
    await _stop_agent(agent, listener)


async def test_agent_concurrency_limit_can_expand_without_restart() -> None:
    bus = InMemoryMessageBus()
    agent = BlockingAgent(
        AgentConfig(id="worker", name="Worker", max_concurrent=1),
        bus,
    )
    listener = asyncio.create_task(agent.start())

    await bus.send(_message("job-a", "workflow-a"))
    await bus.send(_message("job-b", "workflow-b"))
    await asyncio.wait_for(
        agent.started.setdefault("job-a", asyncio.Event()).wait(),
        _TEST_TIMEOUT_SECONDS,
    )
    await asyncio.sleep(0.05)
    assert not agent.started.setdefault("job-b", asyncio.Event()).is_set()

    await agent.update_max_concurrent(2)
    await asyncio.wait_for(agent.started["job-b"].wait(), _TEST_TIMEOUT_SECONDS)
    assert agent.runtime_status()["max_concurrent"] == 2
    assert agent.max_active_handlers == 2

    agent.release.set()
    await _wait_for_response(bus)
    await _wait_for_response(bus)
    await _stop_agent(agent, listener)


def test_agent_concurrency_limit_is_validated() -> None:
    for invalid in (0, 129):
        try:
            AgentConfig(id="worker", name="Worker", max_concurrent=invalid)
        except ValueError:
            continue
        raise AssertionError(f"max_concurrent={invalid} should be rejected")


async def test_agent_concurrency_api_persists_and_applies_limit(
    tmp_path,
    monkeypatch,
) -> None:
    bus = InMemoryMessageBus()
    agent = BlockingAgent(
        AgentConfig(id="worker", name="Worker", max_concurrent=1),
        bus,
    )
    registry = AgentRegistry()
    registry.register(agent)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    config_path = agents_dir / "worker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"agent": agent.config.model_dump(mode="json")},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_routes, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        agent_routes,
        "get_engine",
        lambda: SimpleNamespace(agent_registry=registry),
    )

    result = await agent_routes.update_agent_concurrency(
        "worker",
        agent_routes.AgentConcurrencyUpdateRequest(max_concurrent=3),
    )

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["agent"]["max_concurrent"] == 3
    assert agent.config.max_concurrent == 3
    assert result["runtime"]["max_concurrent"] == 3
