"""Agent model/endpoint health probing tests."""

from __future__ import annotations

from axonflow.api.routes import agents as agent_routes
from axonflow.config.models import AgentConfig, AgentHealthConfig, AxonFlowConfig, ModelConfig
from axonflow.core.agent import AgentHealthState, AgentRegistry, BaseAgent
from axonflow.engine import AxonFlowEngine
from axonflow.llm.gateway import LLMResponse
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry


class StubGateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return LLMResponse(content="OK", model="test-model")


def _agent(gateway: StubGateway) -> BaseAgent:
    return BaseAgent(
        config=AgentConfig(
            id="health-agent",
            name="Health Agent",
            model=ModelConfig(provider="openai", name="test-model"),
        ),
        message_bus=InMemoryMessageBus(),
        llm_gateway=gateway,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )


async def test_health_check_marks_agent_ready_after_real_model_probe() -> None:
    gateway = StubGateway()
    agent = _agent(gateway)

    health = await agent.check_health(timeout_seconds=1)

    assert health["state"] == "healthy"
    assert health["ready"] is True
    assert health["last_checked_at"] is not None
    assert health["last_success_at"] is not None
    assert gateway.calls[0]["model_config"].name == "test-model"
    assert gateway.calls[0]["messages"][-1]["content"] == "PING"
    trace_context = gateway.calls[0]["trace_context"]
    assert trace_context.agent_id == "health-agent"
    assert trace_context.trace_kind == "health"
    assert trace_context.execution_id == "health:health-agent"


async def test_health_check_marks_unavailable_and_preserves_error() -> None:
    agent = _agent(StubGateway(RuntimeError("model endpoint unavailable")))

    health = await agent.check_health(timeout_seconds=1)

    assert agent.health_state == AgentHealthState.UNHEALTHY
    assert health["ready"] is False
    assert health["error"] == "model endpoint unavailable"
    assert health["last_success_at"] is None


async def test_registry_exposes_activity_and_health_separately() -> None:
    agent = _agent(StubGateway())
    registry = AgentRegistry()
    registry.register(agent)

    assert registry.get_states() == {"health-agent": "idle"}
    assert registry.get_health()["health-agent"]["state"] == "unknown"

    await agent.check_health(timeout_seconds=1)

    assert registry.get_states() == {"health-agent": "idle"}
    assert registry.get_health()["health-agent"]["ready"] is True


async def test_periodic_monitor_rechecks_agents(monkeypatch) -> None:
    engine = AxonFlowEngine(
        config=AxonFlowConfig(
            agent_health=AgentHealthConfig(interval_seconds=10, timeout_seconds=1)
        )
    )
    engine._running = True
    checks: list[bool] = []

    async def fake_sleep(seconds: float) -> None:
        assert seconds == 10

    async def fake_check(agent_id=None):
        checks.append(True)
        engine._running = False
        return {}

    monkeypatch.setattr("axonflow.engine.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(engine, "check_agent_health", fake_check)

    await engine._health_monitor()

    assert checks == [True]


async def test_manual_health_route_rechecks_all_registered_agents(monkeypatch) -> None:
    expected = {
        "health-agent": {
            "state": "healthy",
            "ready": True,
        }
    }

    class StubEngine:
        async def check_agent_health(self):
            return expected

    monkeypatch.setattr(agent_routes, "get_engine", lambda: StubEngine())

    assert await agent_routes.check_all_agent_health() == expected
