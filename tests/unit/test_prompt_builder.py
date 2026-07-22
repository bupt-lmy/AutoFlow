"""PromptBuilder 测试"""

from axonflow.config.models import AgentConfig, ModelConfig
from axonflow.core.context import WorkflowContext
from axonflow.core.message import Message, MessageType
from axonflow.llm.prompt_builder import PromptBuilder
from axonflow.memory.base import MemoryRecord, MemoryScope


def _make_config() -> AgentConfig:
    return AgentConfig(
        id="agent-test",
        name="测试",
        role="你是一个测试用的智能体。",
        model=ModelConfig(provider="openai", name="gpt-4o-mini"),
    )


def _make_message() -> Message:
    return Message(
        sender="user",
        receiver="agent-test",
        type=MessageType.TASK_REQUEST,
        payload={"task": "写一个 hello world"},
    )


class TestPromptBuilder:
    def test_basic_build(self):
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
        )
        assert len(messages) == 2  # system + user
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "hello world" in messages[1]["content"]

    def test_build_includes_complete_structured_upstream_payload(self):
        message = _make_message()
        message.payload.update(
            {
                "files_changed": ["src/app.py"],
                "tests": [{"command": "pytest -q", "status": "passed"}],
                "artifacts": [{"uri": "src/app.py"}],
            }
        )

        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=message,
        )

        user_content = messages[-1]["content"]
        assert "完整上游结构化数据" in user_content
        assert "files_changed" in user_content
        assert "pytest -q" in user_content
        assert "src/app.py" in user_content

    def test_build_includes_role(self):
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
        )
        assert "测试用的智能体" in messages[0]["content"]

    def test_build_includes_workflow_responsibility_override(self):
        context = WorkflowContext(
            shared_state={
                "agent_role_overrides": {
                    "agent-test": "只负责核查事实，不要撰写最终答案。",
                }
            }
        )
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            context=context,
        )

        assert "当前工作流职责" in messages[0]["content"]
        assert "只负责核查事实" in messages[0]["content"]

    def test_build_includes_runtime_discovery_protocol(self):
        message = _make_message()
        message.session_id = "session-1"
        message.task_id = "task-1"
        message.payload["_protocol"] = {
            "version": "aip-lite/0.1",
            "session_id": "session-1",
            "task_id": "task-1",
            "requested_capability": "执行安全代码评审",
            "selected_agent": "security-reviewer",
            "attempt": 2,
            "command": {"command": "start"},
            "previous_attempts": [{"agent_id": "reviewer-1", "status": "timeout"}],
        }

        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=message,
        )

        system_content = messages[0]["content"]
        assert "任务协作协议" in system_content
        assert "security-reviewer" in system_content
        assert "任务指令: start" in system_content
        assert "reviewer-1" in system_content
        assert "证据、产物和失败原因" in system_content

    def test_build_with_no_memories(self):
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            memories=None,
        )
        assert "相关记忆" not in messages[0]["content"]

    def test_build_with_empty_memories(self):
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            memories=[],
        )
        assert "相关记忆" not in messages[0]["content"]

    def test_build_with_memories(self):
        memories = [
            MemoryRecord(
                key="task:001",
                value={"task": "previous task", "result": "success"},
                scope=MemoryScope.AGENT,
                agent_id="agent-test",
            ),
            MemoryRecord(
                key="shared:info",
                value="workflow context data",
                scope=MemoryScope.WORKFLOW,
                agent_id="agent-other",
                workflow_id="wf-001",
            ),
        ]
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            memories=memories,
        )
        system_content = messages[0]["content"]
        assert "相关记忆" in system_content
        assert "task:001" in system_content
        assert "shared:info" in system_content
        assert "agent/agent-test" in system_content
        assert "workflow/agent-other" in system_content

    def test_build_with_tool_schemas(self):
        tool_schemas = [
            {"type": "function", "function": {"name": "shell_exec", "parameters": {}}},
        ]
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            tool_schemas=tool_schemas,
        )
        assert "shell_exec" in messages[0]["content"]

    def test_build_with_memories_and_tools(self):
        """记忆和工具信息都应出现在 system prompt 中"""
        memories = [
            MemoryRecord(
                key="prev-result",
                value="some data",
                scope=MemoryScope.GLOBAL,
            ),
        ]
        tool_schemas = [
            {"type": "function", "function": {"name": "file_read", "parameters": {}}},
        ]
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            tool_schemas=tool_schemas,
            memories=memories,
        )
        system_content = messages[0]["content"]
        assert "file_read" in system_content
        assert "相关记忆" in system_content
        assert "prev-result" in system_content

    def test_build_with_skill_content(self):
        """skill_content 应出现在 system prompt 中"""
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            skill_content="# TDD Skill\nAlways write tests first.",
        )
        system_content = messages[0]["content"]
        assert "TDD Skill" in system_content
        assert "write tests first" in system_content

    def test_skill_content_between_role_and_tools(self):
        """skill 内容应在 role 之后、tool 描述之前"""
        tool_schemas = [
            {"type": "function", "function": {"name": "file_read", "parameters": {}}},
        ]
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
            tool_schemas=tool_schemas,
            skill_content="# Review Skill\nCheck code quality.",
        )
        system_content = messages[0]["content"]
        role_pos = system_content.find("测试用的智能体")
        skill_pos = system_content.find("Review Skill")
        tool_pos = system_content.find("file_read")
        assert role_pos < skill_pos < tool_pos

    def test_build_without_skill_content(self):
        """不传 skill_content 时不影响现有行为"""
        messages = PromptBuilder.build(
            agent_config=_make_config(),
            incoming_message=_make_message(),
        )
        assert len(messages) == 2
