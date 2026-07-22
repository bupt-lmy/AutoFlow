"""Agent 基类、AgentRegistry 与 Agent 工厂"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from axonflow.config.loader import load_skill_content
from axonflow.config.models import AgentConfig
from axonflow.core.context import WorkflowContext
from axonflow.core.message import Message, MessageType
from axonflow.core.protocol import DataItem, Product, TaskResult, TaskState, TaskStatus
from axonflow.llm.gateway import LLMGateway, LLMTraceContext
from axonflow.llm.prompt_builder import PromptBuilder
from axonflow.memory.base import MemoryRecord, MemoryScope, MemoryStore
from axonflow.memory.local import InMemoryStore
from axonflow.messaging.base import MessageBus
from axonflow.observability.execution_log import ExecutionLogEntry, ExecutionLogger
from axonflow.tools.base import ToolRegistry

logger = structlog.get_logger()


class AgentState(StrEnum):
    """智能体状态"""

    IDLE = "idle"
    RUNNING = "running"
    WORKING = "working"
    ERROR = "error"
    STOPPED = "stopped"


class AgentHealthState(StrEnum):
    """Actual availability of the Agent's model or remote endpoint."""

    UNKNOWN = "unknown"
    CHECKING = "checking"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class BaseAgent:
    """智能体基类

    每个 Agent 是一个独立的异步任务，持续监听自己的消息队列，
    收到消息后调用 LLM 进行推理，并根据结果执行工具或向其他 Agent 发起请求。

    支持:
    - 记忆系统: Agent 可在处理消息时存取记忆，记忆可按 scope 跨 Agent/跨 Workflow 共享
    - 自定义参数: 通过 config.parameters 传递的参数可在子类中使用
    """

    def __init__(
        self,
        config: AgentConfig,
        message_bus: MessageBus,
        llm_gateway: LLMGateway,
        tool_registry: ToolRegistry,
        memory_store: MemoryStore | None = None,
        execution_logger: ExecutionLogger | None = None,
        skills_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.id = config.id
        self.name = config.name
        self.message_bus = message_bus
        self.llm_gateway = llm_gateway
        self.tool_registry = tool_registry
        self.state = AgentState.IDLE
        self.health_state = AgentHealthState.UNKNOWN
        self.last_health_check_at: str | None = None
        self.last_health_success_at: str | None = None
        self.health_error: str | None = None
        self.health_latency_ms: int | None = None
        self._health_lock = asyncio.Lock()
        self.parameters: dict[str, Any] = config.parameters

        # 记忆系统
        self.memory: MemoryStore = memory_store or InMemoryStore()

        # 执行日志
        self.execution_logger = execution_logger

        # Skills 目录
        self._skills_dir = skills_dir

        # 当前活跃的工作流上下文（由 Orchestrator 注入）
        self._contexts: dict[str, WorkflowContext] = {}

    def set_context(self, workflow_id: str, context: WorkflowContext) -> None:
        """注入工作流上下文"""
        self._contexts[workflow_id] = context

    def get_context(self, workflow_id: str) -> WorkflowContext | None:
        """获取工作流上下文"""
        return self._contexts.get(workflow_id)

    async def start(self) -> None:
        """启动 Agent 消息监听循环"""
        self.state = AgentState.RUNNING
        logger.info("agent.started", agent_id=self.id, name=self.name)

        while self.state == AgentState.RUNNING:
            try:
                message = await self.message_bus.receive(self.id)
                if message is None:
                    continue

                self.state = AgentState.WORKING
                logger.info(
                    "agent.processing",
                    agent_id=self.id,
                    msg_type=message.type.value,
                    sender=message.sender,
                    workflow_id=message.workflow_id,
                )

                result = await self._process_with_retry(message)
                await self._send_response(message, result)
                if self.state != AgentState.STOPPED:
                    self.state = AgentState.RUNNING

            except asyncio.CancelledError:
                logger.info("agent.cancelled", agent_id=self.id)
                break
            except Exception as e:
                logger.error("agent.error", agent_id=self.id, error=str(e))
                self.state = AgentState.ERROR
                await asyncio.sleep(1)  # 短暂等待后恢复
                self.state = AgentState.RUNNING

        self.state = AgentState.STOPPED
        logger.info("agent.stopped", agent_id=self.id)

    async def stop(self) -> None:
        """停止 Agent"""
        self.state = AgentState.STOPPED

    async def _health_probe(self) -> None:
        """Send a minimal command through the configured model endpoint."""
        await self.llm_gateway.chat(
            messages=[
                {
                    "role": "system",
                    "content": "This is an availability probe. Reply with OK only.",
                },
                {"role": "user", "content": "PING"},
            ],
            model_config=self.config.model,
            prefer_default=False,
            max_tokens=8,
            temperature=0,
            trace_context=LLMTraceContext(
                workflow_id="__health__",
                execution_id=f"health:{self.id}",
                agent_id=self.id,
                trace_kind="health",
            ),
        )

    async def check_health(self, timeout_seconds: float = 15) -> dict[str, Any]:
        """Probe actual model/endpoint availability and update health metadata."""
        async with self._health_lock:
            self.health_state = AgentHealthState.CHECKING
            started = time.monotonic()
            checked_at = datetime.now(UTC).isoformat()
            try:
                await asyncio.wait_for(self._health_probe(), timeout=timeout_seconds)
            except Exception as exc:
                self.health_state = AgentHealthState.UNHEALTHY
                self.health_error = str(exc)[:1000]
                logger.warning(
                    "agent.health_check_failed",
                    agent_id=self.id,
                    error=self.health_error,
                )
            else:
                self.health_state = AgentHealthState.HEALTHY
                self.health_error = None
                self.last_health_success_at = checked_at
                logger.info("agent.health_check_passed", agent_id=self.id)
            self.last_health_check_at = checked_at
            self.health_latency_ms = int((time.monotonic() - started) * 1000)
            return self.health_status()

    def health_status(self) -> dict[str, Any]:
        """Return API-safe health information independently from activity state."""
        return {
            "state": self.health_state.value,
            "ready": self.health_state == AgentHealthState.HEALTHY,
            "last_checked_at": self.last_health_check_at,
            "last_success_at": self.last_health_success_at,
            "latency_ms": self.health_latency_ms,
            "error": self.health_error,
        }

    async def _process_with_retry(self, message: Message) -> dict:
        """带重试的消息处理"""
        last_error = None
        for attempt in range(1, self.config.retry_limit + 1):
            try:
                return await self.handle_message(message)
            except Exception as e:
                last_error = e
                will_retry = attempt < self.config.retry_limit
                logger.warning(
                    "agent.retry",
                    agent_id=self.id,
                    attempt=attempt,
                    max_retries=self.config.retry_limit,
                    error=str(e),
                )
                if will_retry:
                    self._log_execution(
                        workflow_id=message.workflow_id,
                        action="agent_retry",
                        tool_name=None,
                        arguments={
                            "failed_attempt": attempt,
                            "next_attempt": attempt + 1,
                            "max_attempts": self.config.retry_limit,
                        },
                        round_num=attempt,
                        error=str(e),
                        message_id=message.id,
                        task_preview=str(
                            message.payload.get("task") or message.payload.get("content") or ""
                        )[:200],
                    )
                    await asyncio.sleep(2**attempt)  # 指数退避

        return {
            "status": "error",
            "error": f"Failed after {self.config.retry_limit} attempts: {last_error}",
        }

    async def handle_message(self, message: Message) -> dict:
        """处理消息的核心逻辑 — 含多轮工具调用循环

        1. 构建 Prompt（含记忆上下文）
        2. 循环：调用 LLM → 检查 tool_calls → 执行工具 → 回填结果 → 重新调用 LLM
        3. LLM 产出 text content 时结束循环
        """
        context = self.get_context(message.workflow_id)
        tool_schemas = self.tool_registry.get_schemas(self.config.tools)

        memories = await self._recall_memories(message)

        # 加载 skill 内容
        skill_content: str | None = None
        if self.config.skills and self._skills_dir:
            loaded = load_skill_content(self._skills_dir, self.config.skills)
            if loaded:
                skill_content = loaded

        messages = PromptBuilder.build(
            agent_config=self.config,
            incoming_message=message,
            context=context,
            tool_schemas=tool_schemas if tool_schemas else None,
            memories=memories,
            skill_content=skill_content,
        )

        configured_rounds = self.parameters.get("max_tool_rounds", 10)
        try:
            max_tool_rounds = int(configured_rounds)
        except (TypeError, ValueError):
            max_tool_rounds = 10
        max_tool_rounds = min(max(max_tool_rounds, 1), 50)
        trace_workflow_id = (
            str(context.get_state("_workflow_id", message.workflow_id))
            if context
            else message.workflow_id
        )
        trace_run_id = context.get_state("_run_id") if context else None
        if not trace_run_id and self.execution_logger:
            trace_run_id = self.execution_logger.get_run_id(message.workflow_id)
        last_tool_name: str | None = None
        last_tool_arguments: str | None = None
        for round_num in range(1, max_tool_rounds + 1):
            llm_response = await self.llm_gateway.chat(
                messages=messages,
                model_config=self.config.model,
                tools=tool_schemas if tool_schemas else None,
                prefer_default=False,
                trace_context=LLMTraceContext(
                    workflow_id=trace_workflow_id,
                    execution_id=message.workflow_id,
                    agent_id=self.id,
                    run_id=trace_run_id,
                    trace_kind="agent",
                ),
            )

            # Case 1: LLM 返回了 tool_calls
            if llm_response.tool_calls:
                # 追加 assistant message（含 tool_calls 信息）
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": llm_response.content or "",
                    "tool_calls": llm_response.tool_calls,
                }
                messages.append(assistant_msg)

                # 逐个执行 tool call
                for tc in llm_response.tool_calls:
                    tc_id = tc["id"]
                    func_name = tc["function"]["name"]
                    func_args_raw = tc["function"]["arguments"]
                    last_tool_name = func_name
                    last_tool_arguments = (
                        func_args_raw
                        if isinstance(func_args_raw, str)
                        else json.dumps(func_args_raw, ensure_ascii=False, default=str)
                    )[:500]

                    # 解析 JSON 参数
                    try:
                        func_args = (
                            json.loads(func_args_raw)
                            if isinstance(func_args_raw, str)
                            else func_args_raw
                        )
                    except json.JSONDecodeError as e:
                        error_msg = f"Invalid JSON in tool arguments: {e}"
                        self._log_execution(
                            workflow_id=message.workflow_id,
                            action="tool_error",
                            tool_name=func_name,
                            arguments={
                                "raw": func_args_raw[:500]
                                if isinstance(func_args_raw, str)
                                else str(func_args_raw)[:500]
                            },
                            error=error_msg,
                            round_num=round_num,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": f"Error: {error_msg}",
                            }
                        )
                        continue

                    # 执行工具
                    tool_result = await self.tool_registry.execute(func_name, arguments=func_args)

                    if tool_result.success:
                        result_content = tool_result.output or ""
                        self._log_execution(
                            workflow_id=message.workflow_id,
                            action="tool_call",
                            tool_name=func_name,
                            arguments=func_args,
                            result=result_content,
                            round_num=round_num,
                        )
                    else:
                        result_content = f"Error: {tool_result.error}"
                        self._log_execution(
                            workflow_id=message.workflow_id,
                            action="tool_error",
                            tool_name=func_name,
                            arguments=func_args,
                            error=tool_result.error,
                            round_num=round_num,
                        )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result_content,
                        }
                    )

                continue  # 回到循环，让 LLM 看到工具结果

            # Case 2: LLM 返回了 text content（无 tool_calls）
            if llm_response.content and llm_response.content.strip():
                result = {
                    "status": "success",
                    "content": llm_response.content,
                    "model": llm_response.model,
                    "tokens_used": llm_response.total_tokens,
                }
                if context:
                    context.add_message(message)
                await self._store_memory(message, result)
                self.state = AgentState.RUNNING
                return result

            # Case 3: 既无 tool_calls 也无 content — 给 LLM 重试机会
            logger.warning(
                "agent.empty_llm_response",
                agent_id=self.id,
                round=round_num,
            )

        # 10 轮用尽
        self._log_execution(
            workflow_id=message.workflow_id,
            action="tool_error",
            tool_name=None,
            arguments=None,
            error="Max tool call rounds exceeded",
            round_num=max_tool_rounds,
            message_id=message.id,
            task_preview=str(
                message.payload.get("task") or message.payload.get("content") or ""
            )[:200],
            rounds_used=max_tool_rounds,
            last_tool_name=last_tool_name,
            last_tool_arguments=last_tool_arguments,
        )
        return {
            "status": "error",
            "error": "Max tool call rounds exceeded",
        }

    def _log_execution(
        self,
        workflow_id: str,
        action: str,
        tool_name: str | None,
        arguments: dict | None,
        round_num: int,
        result: str | None = None,
        error: str | None = None,
        message_id: str | None = None,
        task_preview: str | None = None,
        rounds_used: int | None = None,
        last_tool_name: str | None = None,
        last_tool_arguments: str | None = None,
    ) -> None:
        """记录执行日志（如果 logger 存在）"""
        if self.execution_logger is None:
            return
        entry = ExecutionLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            workflow_id=workflow_id,
            agent_id=self.id,
            action=action,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            error=error,
            round=round_num,
            message_id=message_id,
            task_preview=task_preview,
            rounds_used=rounds_used,
            last_tool_name=last_tool_name,
            last_tool_arguments=last_tool_arguments,
        )
        self.execution_logger.log(entry)

    async def _recall_memories(self, message: Message) -> list[MemoryRecord]:
        """从记忆系统中检索与当前任务相关的记忆"""
        if not self.config.memory.enabled:
            return []

        all_memories: list[MemoryRecord] = []

        # 检索 Agent 自身的记忆
        if "agent" in self.config.memory.scopes:
            agent_memories = await self.memory.search(
                query="",
                scope=MemoryScope.AGENT,
                agent_id=self.id,
                limit=5,
            )
            all_memories.extend(agent_memories)

        # 检索工作流共享记忆
        if "workflow" in self.config.memory.scopes and message.workflow_id:
            workflow_memories = await self.memory.search(
                query="",
                scope=MemoryScope.WORKFLOW,
                workflow_id=message.workflow_id,
                limit=5,
            )
            all_memories.extend(workflow_memories)

        # 检索全局记忆
        if "global" in self.config.memory.scopes:
            global_memories = await self.memory.search(
                query="",
                scope=MemoryScope.GLOBAL,
                limit=3,
            )
            all_memories.extend(global_memories)

        return all_memories

    async def _store_memory(self, message: Message, result: dict) -> None:
        """将对话结果存入记忆"""
        if not self.config.memory.enabled:
            return

        task_content = message.payload.get("task", message.payload.get("content", ""))
        response_content = result.get("content", "")

        # 存入 Agent 私有记忆
        agent_record = MemoryRecord(
            key=f"task:{message.id[:8]}",
            value={"task": task_content, "response": response_content[:500]},
            scope=MemoryScope.AGENT,
            agent_id=self.id,
            workflow_id=message.workflow_id,
            ttl=self.config.memory.default_ttl,
        )
        await self.memory.store(agent_record)

        # 存入工作流共享记忆（其他 Agent 可见）
        if message.workflow_id:
            workflow_record = MemoryRecord(
                key=f"{self.id}:result:{message.id[:8]}",
                value={
                    "agent": self.id,
                    "task": task_content,
                    "result": response_content[:500],
                    "status": result.get("status"),
                },
                scope=MemoryScope.WORKFLOW,
                agent_id=self.id,
                workflow_id=message.workflow_id,
                ttl=self.config.memory.default_ttl,
            )
            await self.memory.store(workflow_record)

    async def _send_response(self, original_message: Message, result: dict) -> None:
        """发送处理结果"""
        if "task_result" not in result:
            succeeded = result.get("status") == "success"
            content = result.get("content")
            error = result.get("error")
            result["task_result"] = TaskResult(
                session_id=original_message.session_id or original_message.workflow_id,
                task_id=original_message.task_id or original_message.step_id,
                sender_id=self.id,
                status=TaskStatus(
                    state=TaskState.COMPLETED if succeeded else TaskState.FAILED,
                    data_items=(
                        [DataItem(type="text", text=str(error))]
                        if error is not None and not succeeded
                        else []
                    ),
                ),
                products=(
                    [
                        Product(
                            name="Agent result",
                            data_items=[DataItem(type="text", text=str(content))],
                        )
                    ]
                    if content is not None
                    else []
                ),
            ).model_dump(mode="json")
        response = original_message.reply(
            payload=result,
            msg_type=(
                MessageType.TASK_RESPONSE
                if result.get("status") == "success"
                else MessageType.ERROR
            ),
        )
        await self.message_bus.send(response)

    async def send_request(
        self, target_agent_id: str, payload: dict, workflow_id: str = ""
    ) -> None:
        """主动向其他 Agent 发起请求"""
        if target_agent_id not in self.config.can_request:
            logger.warning(
                "agent.request_denied",
                agent_id=self.id,
                target=target_agent_id,
                allowed=self.config.can_request,
            )
            return

        message = Message(
            sender=self.id,
            receiver=target_agent_id,
            type=MessageType.TASK_REQUEST,
            payload=payload,
            workflow_id=workflow_id,
        )
        await self.message_bus.send(message)
        logger.info(
            "agent.request_sent",
            sender=self.id,
            target=target_agent_id,
        )


# ============================================================
# Agent 工厂 — 根据配置创建不同类型的 Agent
# ============================================================

# 内置 Agent 类型注册表
_AGENT_TYPE_REGISTRY: dict[str, type[BaseAgent]] = {
    "base": BaseAgent,
}


def register_agent_type(type_name: str, agent_class: type[BaseAgent]) -> None:
    """注册自定义 Agent 类型"""
    _AGENT_TYPE_REGISTRY[type_name] = agent_class
    logger.info("agent_type.registered", type_name=type_name, cls=agent_class.__name__)


def create_agent(
    config: AgentConfig,
    message_bus: MessageBus,
    llm_gateway: LLMGateway,
    tool_registry: ToolRegistry,
    memory_store: MemoryStore | None = None,
    execution_logger: ExecutionLogger | None = None,
    skills_dir: Path | None = None,
) -> BaseAgent:
    """Agent 工厂方法

    根据 config.agent_type 或 config.class_path 创建对应的 Agent 实例:
    - agent_type 匹配内置注册表 → 使用注册的类
    - class_path 指定了自定义类路径 → 动态导入该类
    - 都没有 → 使用 BaseAgent
    """
    agent_cls: type[BaseAgent]

    # 优先使用 class_path 动态导入
    if config.class_path:
        agent_cls = _import_agent_class(config.class_path)
    elif config.agent_type == "remote":
        from axonflow.agents.remote import RemoteAgent

        agent_cls = RemoteAgent
    elif config.agent_type == "codex":
        from axonflow.agents.codex import CodexAgent

        agent_cls = CodexAgent
    elif config.agent_type in _AGENT_TYPE_REGISTRY:
        agent_cls = _AGENT_TYPE_REGISTRY[config.agent_type]
    else:
        logger.warning(
            "agent_factory.unknown_type",
            agent_type=config.agent_type,
            fallback="BaseAgent",
        )
        agent_cls = BaseAgent

    return agent_cls(
        config=config,
        message_bus=message_bus,
        llm_gateway=llm_gateway,
        tool_registry=tool_registry,
        memory_store=memory_store,
        execution_logger=execution_logger,
        skills_dir=skills_dir,
    )


def _import_agent_class(class_path: str) -> type[BaseAgent]:
    """动态导入 Agent 类

    class_path 格式: "module.path.ClassName"
    例如: "axonflow.agents.planner.PlannerAgent"
    """
    try:
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        if not (isinstance(cls, type) and issubclass(cls, BaseAgent)):
            raise TypeError(f"{class_path} is not a subclass of BaseAgent")
        return cls
    except (ImportError, AttributeError, ValueError) as e:
        raise ImportError(f"Cannot import agent class '{class_path}': {e}") from e


# ============================================================
# AgentRegistry
# ============================================================


class AgentRegistry:
    """智能体注册中心"""

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        """注册智能体"""
        self._agents[agent.id] = agent
        logger.info("agent_registry.registered", agent_id=agent.id, name=agent.name)

    def get(self, agent_id: str) -> BaseAgent | None:
        """获取智能体"""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[BaseAgent]:
        """列出所有已注册智能体"""
        return list(self._agents.values())

    def get_states(self) -> dict[str, str]:
        """获取所有智能体状态"""
        return {agent.id: agent.state.value for agent in self._agents.values()}

    def get_health(self) -> dict[str, dict[str, Any]]:
        """Return model/endpoint readiness for every registered Agent."""
        return {agent.id: agent.health_status() for agent in self._agents.values()}
