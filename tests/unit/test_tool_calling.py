"""Tool Calling 闭环测试"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from axonflow.config.models import AgentConfig, ModelConfig
from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMGateway, LLMResponse
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.observability.execution_log import ExecutionLogger
from axonflow.tools.base import ToolRegistry, ToolResult


class TestLLMResponseToolCalls:
    def test_tool_calls_default_none(self):
        resp = LLMResponse(content="hello", model="test-model")
        assert resp.tool_calls is None

    def test_tool_calls_with_data(self):
        tc = [
            {
                "id": "call_001",
                "function": {"name": "shell_exec", "arguments": '{"command": "ls"}'},
                "type": "function",
            }
        ]
        resp = LLMResponse(content="", model="test-model", tool_calls=tc)
        assert resp.tool_calls is not None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "shell_exec"

    def test_gateway_normalizes_provider_native_tool_call_dict(self):
        normalized = LLMGateway._normalize_tool_call(
            {
                "id": "call-native-1",
                "type": "function",
                "function": {
                    "name": "file_read",
                    "arguments": '{"path":"README.md"}',
                },
            }
        )

        assert normalized == {
            "id": "call-native-1",
            "type": "function",
            "function": {
                "name": "file_read",
                "arguments": '{"path":"README.md"}',
            },
        }


class TestGatewayThinkingModelCompat:
    """thinking 模型兼容性：reasoning_content 不应污染 content 字段"""

    @pytest.mark.asyncio
    async def test_thinking_model_with_tool_calls_content_is_empty(self):
        """thinking 模型返回 tool_calls 时，content 应为空字符串（不是 reasoning_content）"""
        gateway = LLMGateway(
            default_model=ModelConfig(provider="openai", name="test-model"),
        )

        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_think_001"
        mock_tool_call.type = "function"
        mock_tool_call.function.name = "file_write"
        mock_tool_call.function.arguments = '{"path": "test.txt", "content": "hello"}'

        mock_msg = MagicMock()
        mock_msg.content = "\n\n"  # thinking 模型 tool_call 时 content 是空白
        mock_msg.tool_calls = [mock_tool_call]
        mock_msg.reasoning_content = "这是模型的推理过程，不应出现在 content 里"

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_choice = MagicMock()
        mock_choice.message = mock_msg

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await gateway.chat(messages=[{"role": "user", "content": "test"}])

        # tool_calls 必须存在
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        # content 不应包含 reasoning_content 的内容
        assert "推理过程" not in result.content
        # content 应为空（tool_call 时正常行为）
        assert result.content.strip() == ""

    @pytest.mark.asyncio
    async def test_thinking_model_text_only_uses_content(self):
        """thinking 模型无 tool_calls 时，content 非空则直接使用（不用 reasoning_content）"""
        gateway = LLMGateway(
            default_model=ModelConfig(provider="openai", name="test-model"),
        )

        mock_msg = MagicMock()
        mock_msg.content = "任务完成，文件已写入。"
        mock_msg.tool_calls = None
        mock_msg.reasoning_content = "这是推理过程，不应覆盖 content"

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_choice = MagicMock()
        mock_choice.message = mock_msg

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await gateway.chat(messages=[{"role": "user", "content": "test"}])

        assert result.tool_calls is None
        assert result.content == "任务完成，文件已写入。"
        assert "推理过程" not in result.content

    @pytest.mark.asyncio
    async def test_thinking_model_truly_empty_content_uses_reasoning(self):
        """content 真正为空且无 tool_calls 时，才回退到 reasoning_content"""
        gateway = LLMGateway(
            default_model=ModelConfig(provider="openai", name="test-model"),
        )

        mock_msg = MagicMock()
        mock_msg.content = ""  # 真正空，非 tool_call 场景
        mock_msg.tool_calls = None
        mock_msg.reasoning_content = "只有推理内容，没有正式回复"

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_choice = MagicMock()
        mock_choice.message = mock_msg

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await gateway.chat(messages=[{"role": "user", "content": "test"}])

        assert result.tool_calls is None
        assert "只有推理内容" in result.content


class TestGatewayParsesToolCalls:
    @pytest.mark.asyncio
    async def test_chat_returns_tool_calls_when_present(self):
        """LLM 返回 tool_calls 时，chat() 应解析并填入 LLMResponse"""
        gateway = LLMGateway(
            default_model=ModelConfig(provider="openai", name="test-model"),
        )

        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_abc123"
        mock_tool_call.type = "function"
        mock_tool_call.function.name = "shell_exec"
        mock_tool_call.function.arguments = '{"command": "echo hi"}'

        mock_msg = MagicMock()
        mock_msg.content = None
        mock_msg.tool_calls = [mock_tool_call]
        mock_msg.reasoning_content = None

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_choice = MagicMock()
        mock_choice.message = mock_msg

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await gateway.chat(messages=[{"role": "user", "content": "test"}])

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["id"] == "call_abc123"
        assert result.tool_calls[0]["function"]["name"] == "shell_exec"
        assert result.tool_calls[0]["function"]["arguments"] == '{"command": "echo hi"}'

    @pytest.mark.asyncio
    async def test_chat_returns_none_tool_calls_when_absent(self):
        """LLM 没有返回 tool_calls 时，chat() 的 tool_calls 应为 None"""
        gateway = LLMGateway(
            default_model=ModelConfig(provider="openai", name="test-model"),
        )

        mock_msg = MagicMock()
        mock_msg.content = "Hello world"
        mock_msg.tool_calls = None

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_choice = MagicMock()
        mock_choice.message = mock_msg

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await gateway.chat(messages=[{"role": "user", "content": "test"}])

        assert result.tool_calls is None
        assert result.content == "Hello world"


class TestToolRegistryExecute:
    @pytest.mark.asyncio
    async def test_execute_known_tool(self):
        """已注册的工具应正常执行"""
        registry = ToolRegistry()

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_tool.execute = AsyncMock(return_value=ToolResult(success=True, output="ok"))

        registry.register(mock_tool)

        result = await registry.execute("test_tool", arguments={"key": "value"})
        assert result.success is True
        assert result.output == "ok"
        mock_tool.execute.assert_called_once_with(key="value")

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        """未注册的工具应返回错误 ToolResult"""
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", arguments={})
        assert result.success is False
        assert "Unknown tool" in result.error

    @pytest.mark.asyncio
    async def test_execute_tool_exception(self):
        """工具抛异常应被捕获并返回 error ToolResult"""
        registry = ToolRegistry()

        mock_tool = MagicMock()
        mock_tool.name = "boom_tool"
        mock_tool.execute = AsyncMock(side_effect=RuntimeError("kaboom"))

        registry.register(mock_tool)

        result = await registry.execute("boom_tool", arguments={"x": 1})
        assert result.success is False
        assert "kaboom" in result.error


# ============================================================
# Tool Calling Loop 集成测试
# ============================================================

def _make_message(task: str = "test task") -> Message:
    return Message(
        sender="user",
        receiver="test-agent",
        type=MessageType.TASK_REQUEST,
        payload={"task": task},
        workflow_id="wf-test",
    )


def _make_agent_config(**overrides) -> AgentConfig:
    defaults = {
        "id": "test-agent",
        "name": "Test Agent",
        "role": "You are a test agent.",
        "model": ModelConfig(provider="openai", name="test-model"),
        "tools": ["shell_exec"],
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


class TestToolCallingLoop:
    @pytest.mark.asyncio
    async def test_configured_tool_round_limit_allows_complex_agent(self):
        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        mock_tool = MagicMock()
        mock_tool.name = "shell_exec"
        mock_tool.to_schema.return_value = {
            "type": "function",
            "function": {"name": "shell_exec", "parameters": {}},
        }
        mock_tool.execute = AsyncMock(return_value=ToolResult(success=True, output="ok"))
        registry.register(mock_tool)

        gateway = MagicMock()
        gateway.chat = AsyncMock(
            side_effect=[
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": "shell_exec",
                                "arguments": '{"command":"true"}',
                            },
                        }
                    ],
                )
                for index in range(11)
            ]
            + [LLMResponse(content="done", model="test")]
        )
        config = _make_agent_config(parameters={"max_tool_rounds": 12})
        agent = BaseAgent(config, bus, gateway, registry)

        result = await agent.handle_message(_make_message())

        assert result["status"] == "success"
        assert result["content"] == "done"
        assert gateway.chat.await_count == 12

    @pytest.mark.asyncio
    async def test_single_tool_call_then_text(self, tmp_path):
        """LLM 先返回 tool_call，执行后 LLM 返回 text → 成功"""
        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))

        mock_tool = MagicMock()
        mock_tool.name = "shell_exec"
        mock_tool.execute = AsyncMock(return_value=ToolResult(success=True, output="hello\n"))
        mock_tool.to_schema.return_value = {
            "type": "function",
            "function": {
                "name": "shell_exec",
                "description": "Run shell commands",
                "parameters": {},
            },
        }
        registry.register(mock_tool)

        gateway = MagicMock()

        resp1 = LLMResponse(
            content="",
            model="test-model",
            tool_calls=[
                {
                    "id": "call_001",
                    "function": {"name": "shell_exec", "arguments": '{"command": "echo hello"}'},
                    "type": "function",
                }
            ],
        )
        resp2 = LLMResponse(content="Done! Output was hello.", model="test-model")

        gateway.chat = AsyncMock(side_effect=[resp1, resp2])

        agent = BaseAgent(
            config=_make_agent_config(),
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            execution_logger=exec_logger,
        )

        result = await agent.handle_message(_make_message())

        assert result["status"] == "success"
        assert "Done" in result["content"]

        mock_tool.execute.assert_called_once_with(command="echo hello")
        assert gateway.chat.call_count == 2

        # Verify second call has tool result message
        second_call_messages = gateway.chat.call_args_list[1][1]["messages"]
        tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_001"

        # Verify execution log
        entries = exec_logger.get_entries(action="tool_call")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_unknown_tool_error_fed_back(self, tmp_path):
        """LLM 请求不存在的工具 → error 回填给 LLM → LLM 产出 text"""
        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))

        gateway = MagicMock()

        resp1 = LLMResponse(
            content="",
            model="test-model",
            tool_calls=[
                {
                    "id": "call_bad",
                    "function": {"name": "nonexistent_tool", "arguments": "{}"},
                    "type": "function",
                }
            ],
        )
        resp2 = LLMResponse(content="Sorry, I used the wrong tool.", model="test-model")

        gateway.chat = AsyncMock(side_effect=[resp1, resp2])

        agent = BaseAgent(
            config=_make_agent_config(),
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            execution_logger=exec_logger,
        )

        result = await agent.handle_message(_make_message())
        assert result["status"] == "success"

        entries = exec_logger.get_entries(action="tool_error")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_json_parse_error_fed_back(self, tmp_path):
        """LLM 返回无效 JSON 参数 → error 回填 → LLM 纠正"""
        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))

        mock_tool = MagicMock()
        mock_tool.name = "shell_exec"
        mock_tool.execute = AsyncMock(return_value=ToolResult(success=True, output="ok"))
        mock_tool.to_schema.return_value = {
            "type": "function",
            "function": {
                "name": "shell_exec",
                "description": "Run shell commands",
                "parameters": {},
            },
        }
        registry.register(mock_tool)

        gateway = MagicMock()

        resp1 = LLMResponse(
            content="",
            model="test-model",
            tool_calls=[
                {
                    "id": "call_bad_json",
                    "function": {"name": "shell_exec", "arguments": "not valid json!!!"},
                    "type": "function",
                }
            ],
        )
        resp2 = LLMResponse(content="Fixed my approach.", model="test-model")

        gateway.chat = AsyncMock(side_effect=[resp1, resp2])

        agent = BaseAgent(
            config=_make_agent_config(),
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            execution_logger=exec_logger,
        )

        result = await agent.handle_message(_make_message())
        assert result["status"] == "success"

        entries = exec_logger.get_entries(action="tool_error")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_max_rounds_exceeded(self, tmp_path):
        """10 轮 tool_calls 后仍无 content → 返回 error"""
        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))

        mock_tool = MagicMock()
        mock_tool.name = "shell_exec"
        mock_tool.execute = AsyncMock(return_value=ToolResult(success=True, output="ok"))
        mock_tool.to_schema.return_value = {
            "type": "function",
            "function": {
                "name": "shell_exec",
                "description": "Run shell commands",
                "parameters": {},
            },
        }
        registry.register(mock_tool)

        gateway = MagicMock()

        endless_resp = LLMResponse(
            content="",
            model="test-model",
            tool_calls=[
                {
                    "id": "call_loop",
                    "function": {"name": "shell_exec", "arguments": '{"command": "echo loop"}'},
                    "type": "function",
                }
            ],
        )
        gateway.chat = AsyncMock(return_value=endless_resp)

        agent = BaseAgent(
            config=_make_agent_config(),
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            execution_logger=exec_logger,
        )

        message = _make_message("x" * 250)
        result = await agent.handle_message(message)
        assert result["status"] == "error"
        assert "exceeded" in result["error"].lower() or "rounds" in result["error"].lower()

        entry = exec_logger.get_entries(action="tool_error")[-1]
        assert entry.action == "tool_error"
        assert entry.tool_name is None
        assert entry.message_id == message.id
        assert isinstance(entry.task_preview, str)
        assert len(entry.task_preview) == 200
        assert entry.rounds_used == 10
        assert entry.last_tool_name == "shell_exec"
        assert entry.last_tool_arguments == '{"command": "echo loop"}'

        log_file = tmp_path / "logs" / "execution-wf-test.jsonl"
        persisted = json.loads(log_file.read_text().splitlines()[-1])
        assert persisted["message_id"] == message.id
        assert persisted["task_preview"] == entry.task_preview
        assert persisted["rounds_used"] == 10
        assert persisted["last_tool_name"] == "shell_exec"
        assert persisted["last_tool_arguments"] == '{"command": "echo loop"}'

    @pytest.mark.asyncio
    async def test_no_tool_calls_no_content_retries(self, tmp_path):
        """LLM 返回空（无 content 无 tool_calls）→ 重试 → 最终有 content"""
        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))

        gateway = MagicMock()

        empty_resp = LLMResponse(content="", model="test-model")
        good_resp = LLMResponse(content="Finally got it.", model="test-model")

        gateway.chat = AsyncMock(side_effect=[empty_resp, good_resp])

        agent = BaseAgent(
            config=_make_agent_config(),
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            execution_logger=exec_logger,
        )

        result = await agent.handle_message(_make_message())
        assert result["status"] == "success"
        assert gateway.chat.call_count == 2


class TestBaseAgent:
    @pytest.mark.asyncio
    async def test_internal_retry_is_written_as_run_visible_execution_event(self, tmp_path):
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))
        agent = BaseAgent(
            config=_make_agent_config(retry_limit=2),
            message_bus=InMemoryMessageBus(),
            llm_gateway=MagicMock(),
            tool_registry=ToolRegistry(),
            execution_logger=exec_logger,
        )
        agent.handle_message = AsyncMock(
            side_effect=[RuntimeError("temporary model outage"), {"status": "success"}]
        )
        message = _make_message("retry this task")

        with patch("axonflow.core.agent.asyncio.sleep", new_callable=AsyncMock):
            result = await agent._process_with_retry(message)

        assert result == {"status": "success"}
        retry = exec_logger.get_entries(action="agent_retry")[-1]
        assert retry.agent_id == "test-agent"
        assert retry.error == "temporary model outage"
        assert retry.arguments == {
            "failed_attempt": 1,
            "next_attempt": 2,
            "max_attempts": 2,
        }
        assert retry.task_preview == "retry this task"

    @pytest.mark.asyncio
    async def test_max_rounds_writes_recoverable_entry(self, tmp_path):
        bus = InMemoryMessageBus()
        gateway = MagicMock()
        gateway.chat = AsyncMock(return_value=LLMResponse(content="", model="test-model"))
        exec_logger = ExecutionLogger(workspace_dir=str(tmp_path))
        agent = BaseAgent(
            config=_make_agent_config(parameters={"max_tool_rounds": 1}),
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=ToolRegistry(),
            execution_logger=exec_logger,
        )
        message = Message(
            sender="user",
            receiver="test-agent",
            type=MessageType.TASK_REQUEST,
            payload={"content": "fallback task preview"},
            workflow_id="wf-no-tool",
        )

        result = await agent.handle_message(message)

        assert result["status"] == "error"
        entry = exec_logger.get_entries(action="tool_error")[-1]
        assert entry.message_id == message.id
        assert entry.task_preview == "fallback task preview"
        assert entry.rounds_used == 1
        assert entry.last_tool_name is None
        assert entry.last_tool_arguments is None
        log_file = tmp_path / "logs" / "execution-wf-no-tool.jsonl"
        persisted = json.loads(log_file.read_text())
        assert persisted["last_tool_name"] is None
        assert persisted["last_tool_arguments"] is None
