"""工作流编排引擎"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from axonflow.config.models import RoutePayloadMapping, WorkflowConfig
from axonflow.core.agent import AgentRegistry
from axonflow.core.context import WorkflowContext
from axonflow.core.message import Message, MessageType
from axonflow.core.protocol import (
    DataItem,
    TaskCommand,
    TaskCommandType,
    protocol_context,
)
from axonflow.messaging.base import MessageBus

logger = structlog.get_logger()

OrchestratorEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def map_route_payload(payload: dict, mapping: RoutePayloadMapping | None) -> dict:
    """Apply edge-level business payload selection while preserving protocol lineage."""
    if mapping is None or not mapping.include:
        mapped = dict(payload)
    else:
        mapped = {field: payload[field] for field in mapping.include if field in payload}

    if mapping is not None and mapping.task_field:
        task_value = payload.get(mapping.task_field)
        if task_value is not None:
            mapped["task"] = task_value

    protocol = payload.get("_protocol")
    if isinstance(protocol, dict):
        mapped["_protocol"] = protocol
    return mapped


class WorkflowResult:
    """工作流执行结果"""

    def __init__(
        self,
        workflow_id: str,
        status: str,
        output: dict | None = None,
        iterations: int = 0,
        duration_seconds: float = 0,
    ) -> None:
        self.workflow_id = workflow_id
        self.status = status
        self.output = output or {}
        self.iterations = iterations
        self.duration_seconds = duration_seconds

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "output": self.output,
            "iterations": self.iterations,
            "duration_seconds": round(self.duration_seconds, 2),
        }


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class BaseOrchestrator(ABC):
    """编排器抽象基类 — 所有协作模式的公共接口"""

    ORCHESTRATOR_ID = "__orchestrator__"

    def __init__(
        self,
        config: WorkflowConfig,
        agent_registry: AgentRegistry,
        message_bus: MessageBus,
        event_callback: OrchestratorEventCallback | None = None,
        run_id: str | None = None,
        **kwargs,
    ) -> None:
        self.config = config
        self.agents = agent_registry
        self.message_bus = message_bus
        self._event_callback = event_callback
        self.run_id = run_id

    # -- 抽象方法 ----------------------------------------------------------

    @abstractmethod
    async def execute(self, initial_input: str) -> WorkflowResult:
        """执行工作流"""
        ...

    # -- 公共辅助方法 ------------------------------------------------------

    def _create_context(self, initial_input: str) -> WorkflowContext:
        """创建工作流上下文"""
        ctx = WorkflowContext(input=initial_input)
        ctx.shared_state.update(self.config.context)
        ctx.shared_state["_workflow_id"] = self.config.id
        ctx.shared_state["_run_id"] = self.run_id
        return ctx

    def _inject_context(self, ctx: WorkflowContext) -> None:
        """为所有参与的 Agent 注入上下文"""
        for agent_id in self.config.agents:
            agent = self.agents.get(agent_id)
            if agent:
                agent.set_context(ctx.workflow_id, ctx)

    def _is_terminal(self, event: Message) -> bool:
        """检查是否满足终止条件"""
        for condition in self.config.flow.terminate_on:
            agent_match = condition.get("agent") == event.sender
            status_match = condition.get("status") == event.payload.get("status")
            if agent_match and status_match:
                return True
        return False

    async def _dispatch(
        self,
        target_id: str,
        payload: dict,
        workflow_id: str,
        step_id: str,
        parent_id: str = "",
        source_id: str | None = None,
    ) -> None:
        """发送任务消息给指定 Agent"""
        task_id = f"{workflow_id}:{step_id}:{target_id}"
        dispatch_payload = dict(payload)
        previous_protocol = dispatch_payload.get("_protocol")
        task_protocol = protocol_context(
            session_id=workflow_id,
            task_id=task_id,
            selected_agent=target_id,
        )
        if isinstance(previous_protocol, dict) and previous_protocol.get("task_id"):
            task_protocol["parent_task_id"] = previous_protocol["task_id"]
        task_protocol["command"] = TaskCommand(
            session_id=workflow_id,
            task_id=task_id,
            command=TaskCommandType.START,
            sender_id=self.ORCHESTRATOR_ID,
            data_items=[
                DataItem(
                    type="data",
                    data={
                        key: value for key, value in dispatch_payload.items() if key != "_protocol"
                    },
                )
            ],
        ).model_dump(mode="json")
        dispatch_payload["_protocol"] = task_protocol
        msg = Message(
            sender=self.ORCHESTRATOR_ID,
            receiver=target_id,
            type=MessageType.TASK_REQUEST,
            payload=dispatch_payload,
            workflow_id=workflow_id,
            step_id=step_id,
            parent_message_id=parent_id,
            session_id=workflow_id,
            task_id=task_id,
        )
        await self.message_bus.send(msg)
        event_data = {
            "agent_id": target_id,
            "source_agent_id": source_id,
            "step_id": step_id,
            "payload": dispatch_payload,
        }
        await self._emit("node.task_assigned", event_data)
        await self._emit("node.task_started", event_data)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish product-level events without coupling the engine to FastAPI."""
        if self._event_callback is not None:
            await self._event_callback(event_type, data)


# ---------------------------------------------------------------------------
# 扁平编排器（原 WorkflowOrchestrator）
# ---------------------------------------------------------------------------


class FlatOrchestrator(BaseOrchestrator):
    """扁平编排器 — 线性 / 条件路由 + fan-in 汇聚

    在原有路由逻辑的基础上增加了 fan-in/join 支持：
    当某个目标 Agent 配置了 JoinConfig 时，编排器会等待其 wait_for
    列表中的所有（或任一）上游 Agent 完成后，再将合并后的 payload
    分发给该目标 Agent。
    """

    async def execute(self, initial_input: str) -> WorkflowResult:
        """执行工作流"""
        start_time = time.monotonic()

        # 1. 创建上下文
        ctx = self._create_context(initial_input)
        workflow_id = ctx.workflow_id
        await self._emit("workflow.context_ready", {"execution_id": workflow_id})

        logger.info(
            "workflow.started",
            workflow_id=workflow_id,
            name=self.config.name,
            entry=self.config.flow.entry,
        )

        # 2. 为所有参与的 Agent 注入上下文
        self._inject_context(ctx)

        # 3. 发送初始任务给入口 Agent
        await self._dispatch(
            target_id=self.config.flow.entry,
            payload={"task": initial_input},
            workflow_id=workflow_id,
            step_id="step-0",
        )

        # 4. 构建 join 反向索引：sender_id -> 它被哪些 join 目标等待
        join_cfg = self.config.flow.join  # dict[target_agent_id, JoinConfig]
        join_pending: dict[str, dict[str, dict]] = {target: {} for target in join_cfg}
        # sender_to_join_targets: 某 sender 完成后需要更新哪些 join 目标
        sender_to_join_targets: dict[str, list[str]] = {}
        for target, cfg in join_cfg.items():
            for waited in cfg.wait_for:
                sender_to_join_targets.setdefault(waited, []).append(target)
        join_routes = {
            (source, route.target): route
            for source, routes in self.config.flow.routes.items()
            for route in routes
            if route.target in join_cfg
        }

        # 5. 事件循环
        iteration = 0
        max_iter = self.config.flow.max_iterations
        timeout = self.config.flow.timeout

        while iteration < max_iter:
            elapsed = time.monotonic() - start_time
            if elapsed > timeout:
                logger.warning("workflow.timeout", workflow_id=workflow_id)
                return WorkflowResult(
                    workflow_id=workflow_id,
                    status="timeout",
                    iterations=iteration,
                    duration_seconds=elapsed,
                )

            # 监听 Orchestrator 的收件箱（Agent 完成任务后会回复到这里）
            event = await self.message_bus.receive(self.ORCHESTRATOR_ID, block_ms=5000)
            if event is None:
                continue

            iteration += 1
            ctx.increment_iteration()
            ctx.add_message(event)

            logger.info(
                "workflow.event",
                workflow_id=workflow_id,
                iteration=iteration,
                sender=event.sender,
                msg_type=event.type.value,
                status=event.payload.get("status"),
            )
            await self._emit(
                "node.result_ready" if event.type == MessageType.TASK_RESPONSE else "node.error",
                {
                    "agent_id": event.sender,
                    "step_id": event.step_id,
                    "payload": event.payload,
                    "error": event.payload.get("error"),
                },
            )

            # 检查终止条件
            if self._is_terminal(event):
                elapsed = time.monotonic() - start_time
                logger.info(
                    "workflow.completed",
                    workflow_id=workflow_id,
                    iterations=iteration,
                    duration=round(elapsed, 2),
                )
                return WorkflowResult(
                    workflow_id=workflow_id,
                    status=("completed" if event.payload.get("status") == "success" else "failed"),
                    output=event.payload,
                    iterations=iteration,
                    duration_seconds=elapsed,
                )

            # ---- fan-in/join 记录 ----
            # 如果当前 sender 是某个 join 目标的 wait_for 成员，记录其结果
            if event.sender in sender_to_join_targets:
                for join_target in sender_to_join_targets[event.sender]:
                    route = join_routes.get((event.sender, join_target))
                    if route and route.condition is not None:
                        actual_value = event.payload.get(route.condition.field)
                        if not route.condition.evaluate(actual_value):
                            continue
                    join_pending[join_target][event.sender] = map_route_payload(
                        event.payload,
                        route.payload_mapping if route else None,
                    )
                    cfg = join_cfg[join_target]

                    # 判断 join 条件是否满足
                    ready = False
                    if cfg.strategy == "all":
                        ready = all(w in join_pending[join_target] for w in cfg.wait_for)
                    elif cfg.strategy == "any":
                        ready = len(join_pending[join_target]) >= 1

                    if ready:
                        # 合并所有已收集的 payload
                        merged_payload: dict = {}
                        for agent_id, p in join_pending[join_target].items():
                            merged_payload[agent_id] = p
                        await self._dispatch(
                            target_id=join_target,
                            payload=merged_payload,
                            workflow_id=workflow_id,
                            step_id=f"step-{iteration}",
                            parent_id=event.id,
                            source_id=event.sender,
                        )
                        # 重置该 join 点的待收集状态
                        join_pending[join_target] = {}

            # ---- 常规路由 ----
            next_targets = self._resolve_next(event)
            for target_id, payload in next_targets:
                # Join 目标只能由上面的 fan-in 逻辑派发，不能再走普通路由重复派发。
                if target_id in join_cfg:
                    continue
                await self._dispatch(
                    target_id=target_id,
                    payload=payload,
                    workflow_id=workflow_id,
                    step_id=f"step-{iteration}",
                    parent_id=event.id,
                    source_id=event.sender,
                )

        elapsed = time.monotonic() - start_time
        logger.warning(
            "workflow.max_iterations",
            workflow_id=workflow_id,
            max=max_iter,
        )
        return WorkflowResult(
            workflow_id=workflow_id,
            status="max_iterations_reached",
            iterations=iteration,
            duration_seconds=elapsed,
        )

    def _resolve_next(self, event: Message) -> list[tuple[str, dict]]:
        """根据路由规则解析下一步目标"""
        sender = event.sender
        routes = self.config.flow.routes.get(sender, [])

        targets = []
        for route in routes:
            routed_payload = map_route_payload(event.payload, route.payload_mapping)
            if route.condition is None:
                # 无条件路由（默认路由）
                targets.append((route.target, routed_payload))
            else:
                # 有条件路由
                actual_value = event.payload.get(route.condition.field)
                if route.condition.evaluate(actual_value):
                    targets.append((route.target, routed_payload))

        return targets


# 向后兼容别名
WorkflowOrchestrator = FlatOrchestrator
