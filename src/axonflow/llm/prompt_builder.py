"""Prompt 构建器 — 将 Agent 配置、上下文、工具转化为 LLM 消息列表"""

from __future__ import annotations

import json

from axonflow.config.models import AgentConfig
from axonflow.core.context import WorkflowContext
from axonflow.core.message import Message
from axonflow.memory.base import MemoryRecord


class PromptBuilder:
    """构建发送给 LLM 的消息列表"""

    @staticmethod
    def build(
        agent_config: AgentConfig,
        incoming_message: Message,
        context: WorkflowContext | None = None,
        tool_schemas: list[dict] | None = None,
        memories: list[MemoryRecord] | None = None,
        skill_content: str | None = None,
    ) -> list[dict]:
        """构建完整的 prompt 消息列表

        Returns:
            OpenAI 格式的 messages 列表
        """
        messages: list[dict] = []

        # 1. System Prompt
        system_parts: list[str] = []

        # 1a. Persona 人设注入（在 role 之前）
        if agent_config.persona.soul:
            system_parts.append(f"## 价值观与行为准则\n{agent_config.persona.soul}")
        if agent_config.persona.user:
            system_parts.append(f"## 用户档案\n{agent_config.persona.user}")
        if agent_config.persona.workflow:
            system_parts.append(f"## 工作流程指南\n{agent_config.persona.workflow}")

        # 1b. 角色描述
        if agent_config.role:
            system_parts.append(agent_config.role)

        # 1b.1 工作流内职责覆盖：仅对当前工作流生效，不修改 Agent 默认角色。
        if context:
            overrides = context.shared_state.get("agent_role_overrides", {})
            responsibility = overrides.get(agent_config.id) if isinstance(overrides, dict) else None
            if isinstance(responsibility, str) and responsibility.strip():
                system_parts.append(
                    "## 当前工作流职责\n"
                    f"{responsibility.strip()}\n"
                    "在本次工作流中优先遵循此职责。"
                )

        # 1c. Skill 内容注入（在 role 之后，tool schemas 之前）
        if skill_content:
            system_parts.append(f"\n## Skills\n{skill_content}")

        if context and context.shared_state:
            visible_state = {
                key: value
                for key, value in context.shared_state.items()
                if key != "agent_role_overrides"
            }
            state_str = "\n".join(f"- {key}: {value}" for key, value in visible_state.items())
            if state_str:
                system_parts.append(f"\n当前工作流上下文:\n{state_str}")

        protocol = incoming_message.payload.get("_protocol")
        if isinstance(protocol, dict):
            protocol_lines = [
                f"- 协议版本: {protocol.get('version', incoming_message.protocol_version)}",
                f"- Session ID: {protocol.get('session_id', incoming_message.session_id)}",
                f"- Task ID: {protocol.get('task_id', incoming_message.task_id)}",
            ]
            if protocol.get("requested_capability"):
                protocol_lines.append(f"- 当前职责: {protocol['requested_capability']}")
            if protocol.get("selected_agent"):
                protocol_lines.append(f"- 运行时选中的 Agent: {protocol['selected_agent']}")
            command = protocol.get("command")
            if isinstance(command, dict) and command.get("command"):
                protocol_lines.append(f"- 任务指令: {command['command']}")
            protocol_lines.append(f"- 当前尝试: {protocol.get('attempt', 1)}")
            previous_attempts = protocol.get("previous_attempts")
            if previous_attempts:
                protocol_lines.append(f"- 已失败尝试: {previous_attempts}")
            system_parts.append(
                "\n## 任务协作协议\n"
                + "\n".join(protocol_lines)
                + "\n请完成当前职责，并在结果中保留可供下游使用的证据、产物和失败原因。"
            )

        if tool_schemas:
            tool_names = [t["function"]["name"] for t in tool_schemas]
            system_parts.append(
                f"\n你可以使用以下工具: {', '.join(tool_names)}"
                "\n当需要执行操作时，使用 function calling 调用对应工具。"
            )

        # 记忆上下文注入
        if memories:
            memory_lines: list[str] = []
            for mem in memories:
                scope_label = mem.scope.value if hasattr(mem.scope, "value") else str(mem.scope)
                agent_label = mem.agent_id or "global"
                value_summary = str(mem.value)[:200]
                memory_lines.append(f"- [{scope_label}/{agent_label}] {mem.key}: {value_summary}")
            system_parts.append("\n相关记忆:\n" + "\n".join(memory_lines))

        messages.append(
            {
                "role": "system",
                "content": "\n".join(system_parts),
            }
        )

        # 2. 历史消息（最近的 N 条，避免上下文过长）
        if context and context.history:
            recent = context.history[-10:]  # 最近 10 条
            for hist_msg in recent:
                role = "assistant" if hist_msg.sender == agent_config.id else "user"
                content = hist_msg.payload.get("content", str(hist_msg.payload))
                messages.append({"role": role, "content": content})

        # 3. 当前任务消息
        task_content = incoming_message.payload.get(
            "task",
            incoming_message.payload.get("content", str(incoming_message.payload)),
        )
        if not isinstance(task_content, str):
            task_content = str(task_content)
        business_payload = {
            key: value
            for key, value in incoming_message.payload.items()
            if key not in {"_protocol", "task_result"}
        }
        extra_fields = set(business_payload) - {"task", "content"}
        if extra_fields:
            task_content += (
                "\n\n完整上游结构化数据（用于核对证据、产物和业务状态）：\n"
                + json.dumps(business_payload, ensure_ascii=False, indent=2, default=str)
            )
        messages.append({"role": "user", "content": task_content})

        return messages
