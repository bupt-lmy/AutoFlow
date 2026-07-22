"""Agent 工厂与类型注册测试"""

import pytest

from axonflow.config.models import AgentConfig, MemoryConfig, ModelConfig
from axonflow.core.agent import (
    _AGENT_TYPE_REGISTRY,
    BaseAgent,
    create_agent,
    register_agent_type,
)
from axonflow.llm.gateway import LLMGateway
from axonflow.memory.local import InMemoryStore
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry


def _make_agent_config(**overrides) -> AgentConfig:
    """构建测试用 AgentConfig"""
    defaults = {
        "id": "test-agent",
        "name": "测试智能体",
        "role": "测试用角色",
        "model": ModelConfig(provider="openai", name="gpt-4o-mini"),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_deps():
    """创建 Agent 依赖"""
    bus = InMemoryMessageBus()
    gateway = LLMGateway()
    registry = ToolRegistry()
    memory = InMemoryStore()
    return bus, gateway, registry, memory


class TestAgentFactory:
    def test_create_base_agent(self):
        config = _make_agent_config(agent_type="base")
        bus, gateway, registry, memory = _make_deps()

        agent = create_agent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            memory_store=memory,
        )
        assert isinstance(agent, BaseAgent)
        assert agent.id == "test-agent"

    def test_create_codex_agent(self):
        from axonflow.agents.codex import CodexAgent

        config = _make_agent_config(agent_type="codex", parameters={"codex": {}})
        bus, gateway, registry, memory = _make_deps()

        agent = create_agent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            memory_store=memory,
        )

        assert isinstance(agent, CodexAgent)

    def test_create_agent_with_unknown_type_fallback(self):
        config = _make_agent_config(agent_type="unknown_type_xyz")
        bus, gateway, registry, memory = _make_deps()

        agent = create_agent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            memory_store=memory,
        )
        # 未知类型应降级到 BaseAgent
        assert isinstance(agent, BaseAgent)

    def test_create_agent_without_memory_store(self):
        config = _make_agent_config()
        bus, gateway, registry, _ = _make_deps()

        agent = create_agent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            memory_store=None,
        )
        # 应自动创建一个 InMemoryStore
        assert agent.memory is not None

    def test_create_agent_with_shared_memory_store(self):
        config = _make_agent_config()
        bus, gateway, registry, memory = _make_deps()

        agent = create_agent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            memory_store=memory,
        )
        assert agent.memory is memory

    def test_create_agent_with_invalid_class_path(self):
        config = _make_agent_config(class_path="nonexistent.module.FakeClass")
        bus, gateway, registry, memory = _make_deps()

        with pytest.raises(ImportError, match="Cannot import agent class"):
            create_agent(
                config=config,
                message_bus=bus,
                llm_gateway=gateway,
                tool_registry=registry,
                memory_store=memory,
            )


class TestRegisterAgentType:
    def test_register_and_create_custom_type(self):
        class CustomAgent(BaseAgent):
            pass

        register_agent_type("custom_test", CustomAgent)

        config = _make_agent_config(agent_type="custom_test")
        bus, gateway, registry, memory = _make_deps()

        agent = create_agent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            memory_store=memory,
        )
        assert isinstance(agent, CustomAgent)

        # 清理：从注册表中移除测试类型
        _AGENT_TYPE_REGISTRY.pop("custom_test", None)

    def test_registry_contains_base(self):
        assert "base" in _AGENT_TYPE_REGISTRY
        assert _AGENT_TYPE_REGISTRY["base"] is BaseAgent


class TestAgentConfig:
    def test_default_memory_config(self):
        config = _make_agent_config()
        assert config.memory.enabled is True
        assert config.memory.backend == "in_memory"
        assert "agent" in config.memory.scopes
        assert "workflow" in config.memory.scopes

    def test_custom_memory_config(self):
        mem_cfg = MemoryConfig(
            enabled=False,
            backend="redis",
            max_records=100,
            default_ttl=3600,
            scopes=["global"],
        )
        config = _make_agent_config(memory=mem_cfg)
        assert config.memory.enabled is False
        assert config.memory.backend == "redis"
        assert config.memory.max_records == 100
        assert config.memory.default_ttl == 3600
        assert config.memory.scopes == ["global"]

    def test_agent_type_field(self):
        config = _make_agent_config(agent_type="planner")
        assert config.agent_type == "planner"

    def test_class_path_field(self):
        config = _make_agent_config(class_path="my.module.MyAgent")
        assert config.class_path == "my.module.MyAgent"

    def test_parameters_field(self):
        config = _make_agent_config(parameters={"depth": 3, "verbose": True})
        assert config.parameters == {"depth": 3, "verbose": True}

    def test_global_health_check_defaults(self):
        from axonflow.config.models import AxonFlowConfig

        config = AxonFlowConfig()
        assert config.agent_health.enabled is True
        assert config.agent_health.interval_seconds == 300
        assert config.agent_health.timeout_seconds == 15
