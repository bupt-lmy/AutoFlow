"""Supervisor 编排器 — 由 LLM 驱动的全局规划与监控"""

from __future__ import annotations

import json
import time

import structlog

from axonflow.config.loader import load_skill_content
from axonflow.config.models import WorkflowConfig
from axonflow.core.agent import AgentRegistry
from axonflow.core.context import WorkflowContext
from axonflow.core.message import MessageType
from axonflow.core.workflow import (
    BaseOrchestrator,
    OrchestratorEventCallback,
    WorkflowResult,
    map_route_payload,
)
from axonflow.json_utils import parse_json_object
from axonflow.llm.gateway import LLMGateway, LLMTraceContext
from axonflow.messaging.base import MessageBus

logger = structlog.get_logger()


class SupervisorOrchestrator(BaseOrchestrator):
    """Supervisor 编排模式

    工作流程：
    1. Supervisor Agent 做全局规划（拆分任务 + 分配 Agent）
    2. 按规划依次/并行派发任务给各 Agent
    3. 每个 Agent 返回结果后，Supervisor 评估：
       - 是否需要纠偏（intervention_on_failure）
       - 下一步交给哪个 Agent
    4. 所有任务完成后，Supervisor 做最终汇总
    """

    def __init__(
        self,
        config: WorkflowConfig,
        agent_registry: AgentRegistry,
        message_bus: MessageBus,
        llm_gateway: LLMGateway | None = None,
        event_callback: OrchestratorEventCallback | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(
            config,
            agent_registry,
            message_bus,
            event_callback=event_callback,
            run_id=run_id,
        )
        self.llm_gateway = llm_gateway

        if config.flow.supervisor is None:
            raise ValueError("SupervisorOrchestrator requires flow.supervisor config")
        self.supervisor_config = config.flow.supervisor
        self._completion_status = "completed"
        self._last_review_decision: dict | None = None

    # ------------------------------------------------------------------
    # 主执行入口
    # ------------------------------------------------------------------

    async def execute(self, initial_input: str) -> WorkflowResult:
        """执行 Supervisor 模式工作流"""
        start_time = time.monotonic()
        ctx = self._create_context(initial_input)
        self._completion_status = "completed"
        self._last_review_decision = None
        ctx.update_state("supervision", self._new_supervision_state())
        workflow_id = ctx.workflow_id
        await self._emit("workflow.context_ready", {"execution_id": workflow_id})
        self._inject_context(ctx)

        logger.info(
            "supervisor.started",
            workflow_id=workflow_id,
            supervisor=self.supervisor_config.agent_id,
        )

        # Phase 1: 全局规划
        plan = None
        if self.supervisor_config.planning_enabled:
            plan = await self._create_plan(initial_input, ctx)
            if plan:
                ctx.update_state("supervisor_plan", plan)
                logger.info("supervisor.plan_created", steps=len(plan.get("steps", [])))

        # Phase 2: 执行循环
        iteration = 0
        max_iter = self.config.flow.max_iterations
        timeout = self.config.flow.timeout
        completed_steps: list[dict] = []

        # 从规划或 entry 配置获取初始派发目标
        pending_targets = self._guard_targets(
            self._get_initial_targets(plan, initial_input),
            ctx,
            track_repetition=False,
        )

        while iteration < max_iter and pending_targets:
            elapsed = time.monotonic() - start_time
            if elapsed > timeout:
                logger.warning("supervisor.timeout", workflow_id=workflow_id)
                return WorkflowResult(
                    workflow_id=workflow_id,
                    status="timeout",
                    output=self._build_output(
                        ctx,
                        completed_steps,
                        content="Supervisor 工作流执行超时。",
                        status="timeout",
                    ),
                    iterations=iteration,
                    duration_seconds=elapsed,
                )

            # 派发所有待处理目标
            for target_id, task_payload in pending_targets:
                await self._dispatch(
                    target_id=target_id,
                    payload=task_payload,
                    workflow_id=workflow_id,
                    step_id=f"step-{iteration}",
                )

            # 等待各 Agent 返回
            responses_needed = len(pending_targets)
            step_results: list[dict] = []
            terminal_reached = False

            responses_received = 0
            while responses_received < responses_needed:
                elapsed = time.monotonic() - start_time
                if elapsed > timeout:
                    logger.warning(
                        "supervisor.response_timeout",
                        workflow_id=workflow_id,
                        received=responses_received,
                        expected=responses_needed,
                    )
                    return WorkflowResult(
                        workflow_id=workflow_id,
                        status="timeout",
                        output=self._build_output(
                            ctx,
                            completed_steps,
                            content="等待 Agent 返回结果时超时。",
                            status="timeout",
                        ),
                        iterations=iteration,
                        duration_seconds=elapsed,
                    )
                event = await self.message_bus.receive(self.ORCHESTRATOR_ID, block_ms=5000)
                if event is None:
                    continue

                responses_received += 1
                iteration += 1
                ctx.increment_iteration()
                ctx.add_message(event)

                step_results.append(
                    {
                        "agent": event.sender,
                        "step_id": event.step_id,
                        "message_type": event.type.value,
                        "status": event.payload.get("status"),
                        "content": event.payload.get("content", ""),
                        "payload": event.payload,
                    }
                )

                logger.info(
                    "supervisor.step_completed",
                    agent=event.sender,
                    status=event.payload.get("status"),
                    iteration=iteration,
                )
                event_type = (
                    "node.result_ready" if event.type == MessageType.TASK_RESPONSE else "node.error"
                )
                await self._emit(
                    event_type,
                    {
                        "agent_id": event.sender,
                        "step_id": event.step_id,
                        "payload": event.payload,
                        "error": event.payload.get("error"),
                    },
                )

                # 终止条件只是提交给 Supervisor 的完成候选，不绕过本轮审阅。
                terminal_reached = terminal_reached or self._is_terminal(event)

            completed_steps.extend(step_results)
            self._collect_evidence(step_results, ctx)
            await self._emit(
                "supervisor.review_started",
                {
                    "supervisor_agent_id": self.supervisor_config.agent_id,
                    "results": step_results,
                    "terminal_candidate": terminal_reached,
                },
            )

            # Phase 3: Supervisor 决策 — 下一步行动
            failed_steps = [s for s in step_results if s["status"] == "error"]
            if failed_steps and self.supervisor_config.intervention_on_failure:
                pending_targets = await self._handle_failure(
                    failed_steps, completed_steps, initial_input, ctx
                )
            else:
                pending_targets = await self._decide_next(
                    step_results,
                    completed_steps,
                    initial_input,
                    ctx,
                    terminal_reached=terminal_reached,
                )
            pending_targets = self._guard_targets(pending_targets, ctx)
            await self._emit(
                "supervisor.decision_ready",
                {
                    "supervisor_agent_id": self.supervisor_config.agent_id,
                    "next_agents": [target for target, _payload in pending_targets],
                    "outcome": self._completion_status if not pending_targets else "continue",
                    "decision": self._last_review_decision,
                },
            )

        # 循环结束：已无待派发目标或达到最大迭代
        elapsed = time.monotonic() - start_time
        if not pending_targets:
            # Supervisor 判定任务完成，执行汇总
            summary = await self._summarize(completed_steps, initial_input, ctx)
            completed = self._completion_status == "completed"
            return WorkflowResult(
                workflow_id=workflow_id,
                status=self._completion_status,
                output=self._build_output(
                    ctx,
                    completed_steps,
                    content=summary,
                    status="success" if completed else "error",
                ),
                iterations=iteration,
                duration_seconds=elapsed,
            )

        return WorkflowResult(
            workflow_id=workflow_id,
            status="max_iterations_reached",
            output=self._build_output(
                ctx,
                completed_steps,
                content="Supervisor 已达到最大执行步数，任务尚未通过验收。",
                status="max_iterations_reached",
            ),
            iterations=iteration,
            duration_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _new_supervision_state() -> dict:
        return {
            "review_cycles": 0,
            "attempts_by_agent": {},
            "decision_history": [],
            "evidence": [],
            "unresolved_issues": [],
            "completion_violations": [],
            "blocked_reason": None,
            "last_target_signature": None,
            "repeated_decisions": 0,
            "completion_payload": None,
        }

    def _supervision_state(self, ctx: WorkflowContext) -> dict:
        state = ctx.get_state("supervision")
        if not isinstance(state, dict):
            state = self._new_supervision_state()
            ctx.update_state("supervision", state)
        return state

    @staticmethod
    def _value_at_path(payload: dict, path: str):
        value: object = payload
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    def _collect_evidence(self, step_results: list[dict], ctx: WorkflowContext) -> None:
        """Collect structured evidence independently from the Supervisor's prose."""
        state = self._supervision_state(ctx)
        evidence = state["evidence"]
        for result in step_results:
            payload = result.get("payload")
            if not isinstance(payload, dict):
                continue
            for field in self.supervisor_config.evidence_fields:
                value = self._value_at_path(payload, field)
                if value in (None, "", [], {}):
                    continue
                item = {
                    "agent": result.get("agent"),
                    "step_id": result.get("step_id"),
                    "field": field,
                    "value": value,
                }
                if item not in evidence:
                    evidence.append(item)

    def _completion_violations(
        self,
        decision: dict,
        terminal_reached: bool,
        ctx: WorkflowContext,
    ) -> list[str]:
        """Apply deterministic gates after the LLM proposes completion."""
        violations: list[str] = []
        acceptance = decision.get("acceptance")
        if not isinstance(acceptance, dict):
            acceptance = {}

        if self.supervisor_config.require_terminal_candidate and not terminal_reached:
            violations.append("尚未命中工作流终止条件")

        criteria = self.supervisor_config.acceptance_criteria
        if criteria:
            results = acceptance.get("criteria")
            if acceptance.get("passed") is not True:
                violations.append("Supervisor 未确认全部验收标准通过")
            result_by_criterion = (
                {
                    item.get("criterion", "").strip(): item
                    for item in results
                    if isinstance(item, dict) and isinstance(item.get("criterion"), str)
                }
                if isinstance(results, list)
                else {}
            )
            if any(criterion not in result_by_criterion for criterion in criteria):
                violations.append("验收标准缺少逐项判定")
            elif any(
                result_by_criterion[criterion].get("passed") is not True for criterion in criteria
            ):
                violations.append("至少一项验收标准未通过")

        unresolved = acceptance.get("unresolved_issues", [])
        if isinstance(unresolved, list) and unresolved:
            violations.append("仍存在未解决问题")

        if self.supervisor_config.require_evidence:
            evidence = self._supervision_state(ctx)["evidence"]
            if not evidence:
                violations.append("Agent 未返回配置要求的结构化证据")

        return list(dict.fromkeys(violations))

    def _record_decision(
        self,
        decision: dict,
        terminal_reached: bool,
        violations: list[str],
        ctx: WorkflowContext,
    ) -> None:
        state = self._supervision_state(ctx)
        state["review_cycles"] += 1
        acceptance = decision.get("acceptance")
        unresolved = acceptance.get("unresolved_issues", []) if isinstance(acceptance, dict) else []
        state["unresolved_issues"] = unresolved if isinstance(unresolved, list) else []
        state["completion_violations"] = violations
        record = {
            "cycle": state["review_cycles"],
            "verdict": decision.get("verdict"),
            "done": bool(decision.get("done", False)),
            "reason": decision.get("reason", ""),
            "terminal_candidate": terminal_reached,
            "violations": violations,
            "next_agents": [
                item.get("agent_id") for item in decision.get("next", []) if isinstance(item, dict)
            ],
        }
        state["decision_history"].append(record)
        self._last_review_decision = record

    def _guard_targets(
        self,
        targets: list[tuple[str, dict]],
        ctx: WorkflowContext,
        *,
        track_repetition: bool = True,
    ) -> list[tuple[str, dict]]:
        """Bound retries and repeated decisions before dispatching more work."""
        if not targets:
            return []
        state = self._supervision_state(ctx)
        attempts = state["attempts_by_agent"]

        exhausted = [
            agent_id
            for agent_id, _payload in targets
            if attempts.get(agent_id, 0) >= self.supervisor_config.max_attempts_per_agent
        ]
        if exhausted:
            reason = (
                "以下 Agent 已达到单 Agent 最大执行次数 "
                f"{self.supervisor_config.max_attempts_per_agent}: {', '.join(exhausted)}"
            )
            state["blocked_reason"] = reason
            state["unresolved_issues"] = [reason]
            self._completion_status = "blocked"
            return []

        if track_repetition:
            signature = json.dumps(targets, ensure_ascii=False, sort_keys=True, default=str)
            if signature == state["last_target_signature"]:
                state["repeated_decisions"] += 1
            else:
                state["last_target_signature"] = signature
                state["repeated_decisions"] = 1
            if state["repeated_decisions"] > self.supervisor_config.max_repeated_decisions:
                reason = (
                    "Supervisor 连续生成相同派发决策，已触发防死循环限制 "
                    f"({self.supervisor_config.max_repeated_decisions})"
                )
                state["blocked_reason"] = reason
                state["unresolved_issues"] = [reason]
                self._completion_status = "blocked"
                return []

        for agent_id, _payload in targets:
            attempts[agent_id] = attempts.get(agent_id, 0) + 1
        return targets

    def _build_output(
        self,
        ctx: WorkflowContext,
        completed_steps: list[dict],
        *,
        content: str,
        status: str,
    ) -> dict:
        state = self._supervision_state(ctx)
        terminal_payload = state.get("completion_payload")
        output = dict(terminal_payload) if isinstance(terminal_payload, dict) else {}
        output.update(
            {
                "content": content,
                "status": status,
                "supervision_report": {
                    "review_cycles": state["review_cycles"],
                    "attempts_by_agent": dict(state["attempts_by_agent"]),
                    "decision_history": list(state["decision_history"]),
                    "evidence": list(state["evidence"]),
                    "unresolved_issues": list(state["unresolved_issues"]),
                    "completion_violations": list(state["completion_violations"]),
                    "blocked_reason": state["blocked_reason"],
                    "completed_steps": len(completed_steps),
                },
            }
        )
        return output

    def _get_initial_targets(self, plan: dict | None, initial_input: str) -> list[tuple[str, dict]]:
        """从规划或 entry 配置获取初始目标"""
        if plan and "steps" in plan:
            first_steps = [s for s in plan["steps"] if s.get("order", 1) == 1]
            if first_steps:
                return self._decision_targets(first_steps)
        # 回退到 entry agent
        return [(self.config.flow.entry, {"task": initial_input})]

    def _terminal_payload(self, step_results: list[dict]) -> dict | None:
        for result in reversed(step_results):
            payload = result.get("payload")
            if not isinstance(payload, dict):
                continue
            for condition in self.config.flow.terminate_on:
                if condition.get("agent") == result.get("agent") and condition.get(
                    "status"
                ) == payload.get("status"):
                    return payload
        return None

    def _get_supervisor_model_config(self):
        """获取 Supervisor Agent 的模型配置"""
        supervisor_agent = self.agents.get(self.supervisor_config.agent_id)
        return supervisor_agent.config.model if supervisor_agent else None

    def _trace_context(self, ctx: WorkflowContext) -> LLMTraceContext:
        """Attach the Supervisor identity and all workflow IDs to every model call."""
        return LLMTraceContext(
            workflow_id=self.config.id,
            execution_id=ctx.workflow_id,
            agent_id=self.supervisor_config.agent_id,
            run_id=self.run_id,
            trace_kind="supervisor",
        )

    def _supervisor_instruction(self) -> str:
        """Combine the selected template role with workflow-specific supervision policy."""
        supervisor_agent = self.agents.get(self.supervisor_config.agent_id)
        parts: list[str] = []
        if supervisor_agent and supervisor_agent.config.role.strip():
            parts.append(f"基础角色:\n{supervisor_agent.config.role.strip()}")
        if supervisor_agent and supervisor_agent.config.skills and supervisor_agent._skills_dir:
            skill_content = load_skill_content(
                supervisor_agent._skills_dir,
                supervisor_agent.config.skills,
            )
            if skill_content:
                parts.append(f"审核 Skill:\n{skill_content}")
        if self.supervisor_config.responsibility:
            parts.append(f"本工作流职责:\n{self.supervisor_config.responsibility}")
        if self.supervisor_config.capabilities:
            parts.append(
                "允许使用的监督能力:\n- " + "\n- ".join(self.supervisor_config.capabilities)
            )
        return "\n\n".join(parts)

    def _available_agents(self) -> list[dict]:
        """Describe executable Agents so Supervisor decisions are grounded in the workflow."""
        available_agents: list[dict] = []
        role_overrides = self.config.context.get("agent_role_overrides", {})
        if not isinstance(role_overrides, dict):
            role_overrides = {}
        for agent_id in self.config.agents:
            agent = self.agents.get(agent_id)
            if agent and agent.id != self.supervisor_config.agent_id:
                responsibility = role_overrides.get(agent.id)
                role = (
                    responsibility.strip()
                    if isinstance(responsibility, str) and responsibility.strip()
                    else agent.config.role
                )
                available_agents.append(
                    {
                        "id": agent.id,
                        "name": agent.name,
                        "role": role[:500],
                        "tools": agent.config.tools,
                        "skills": agent.config.skills,
                        "tags": agent.config.tags,
                    }
                )
        return available_agents

    def _decision_targets(self, items: list[dict]) -> list[tuple[str, dict]]:
        """Accept only registered non-Supervisor targets and preserve structured payloads."""
        allowed = {item["id"] for item in self._available_agents()}
        targets: list[tuple[str, dict]] = []
        for item in items:
            agent_id = item.get("agent_id")
            if agent_id not in allowed:
                logger.warning("supervisor.invalid_target", agent_id=agent_id)
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                payload = {"task": str(item.get("task", ""))}
            targets.append((agent_id, payload))
        return targets

    # ------------------------------------------------------------------
    # LLM 驱动的规划与决策
    # ------------------------------------------------------------------

    async def _create_plan(self, initial_input: str, ctx: WorkflowContext) -> dict | None:
        """让 Supervisor Agent 用 LLM 创建全局执行计划"""
        if not self.llm_gateway:
            return None

        available_agents = self._available_agents()

        planning_prompt = [
            {
                "role": "system",
                "content": (
                    f"{self._supervisor_instruction()}\n\n"
                    "你是本工作流的 Supervisor。根据用户需求和可用 Agent 列表，"
                    "创建一个执行计划。返回 JSON 格式：\n"
                    '{"steps": [{"order": 1, "agent_id": "...", "task": "...", '
                    '"depends_on": []}]}\n'
                    "order 相同的步骤可以并行执行。depends_on 列出依赖的步骤 order。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"需求: {initial_input}\n\n"
                    f"可用 Agent:\n"
                    f"{json.dumps(available_agents, ensure_ascii=False, indent=2)}"
                ),
            },
        ]

        try:
            response = await self.llm_gateway.chat(
                messages=planning_prompt,
                model_config=self._get_supervisor_model_config(),
                trace_context=self._trace_context(ctx),
            )
            return parse_json_object(response.content)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("supervisor.plan_parse_failed", error=str(e))
            return None

    async def _decide_next(
        self,
        step_results: list[dict],
        completed_steps: list[dict],
        initial_input: str,
        ctx: WorkflowContext,
        terminal_reached: bool = False,
    ) -> list[tuple[str, dict]]:
        """让 Supervisor 审阅完整结果，并将静态路由仅作为可覆盖的建议。"""
        route_targets = self._resolve_static_routes(step_results)
        if not self.llm_gateway:
            decision = {
                "verdict": "accept" if terminal_reached else "continue",
                "done": terminal_reached,
                "reason": "无 LLM Supervisor，按静态路由和确定性验收门执行。",
                "acceptance": {
                    "passed": not self.supervisor_config.acceptance_criteria,
                    "criteria": [],
                    "evidence": [],
                    "unresolved_issues": [],
                },
                "next": [],
            }
            if terminal_reached:
                violations = self._completion_violations(decision, terminal_reached, ctx)
                self._record_decision(decision, terminal_reached, violations, ctx)
                if not violations:
                    self._completion_status = "completed"
                    self._supervision_state(ctx)["completion_payload"] = self._terminal_payload(
                        step_results
                    )
                    return []
                if not route_targets:
                    self._completion_status = "verification_failed"
                    return []
            if not route_targets:
                self._completion_status = "stalled"
            if not terminal_reached:
                self._record_decision(decision, terminal_reached, [], ctx)
            return route_targets

        recent_steps = completed_steps[-self.supervisor_config.review_history_limit :]
        decision_prompt = [
            {
                "role": "system",
                "content": (
                    f"{self._supervisor_instruction()}\n\n"
                    "你必须像持续值守的工程监督者一样，审阅最新一批 Agent 的完整结构化"
                    "结果，并控制“执行—取证—验收—局部修复—重新验证”闭环。"
                    "status=success 只代表调用成功，不代表结果正确；必须依据结果中的真实证据"
                    "逐项验收。静态路由只是建议，你可以接受、改派、要求返工或结束。\n"
                    "返回 JSON:\n"
                    '{"verdict":"accept|continue|rework|abort","done":false,'
                    '"reason":"...","acceptance":{"passed":false,'
                    '"criteria":[{"criterion":"...","passed":false,'
                    '"evidence":["payload 中的字段或原文"]}],'
                    '"evidence":["本轮实际证据"],"unresolved_issues":["..."]},'
                    '"next":[{"agent_id":"...","task":"...","payload":{}}]}\n'
                    "只允许选择给出的可用 Agent。若发现问题，优先把最小修复任务交给合适的"
                    " Agent；修复后必须重新派发测试/审核 Agent 验证。只有全部验收项通过且"
                    "没有未解决问题时，才能设 done=true、verdict=accept、next=[]。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"原始需求: {initial_input}\n\n"
                    "配置的验收标准:\n"
                    f"{json.dumps(self.supervisor_config.acceptance_criteria, ensure_ascii=False)}"
                    "\n\n"
                    f"可用 Agent:\n{json.dumps(self._available_agents(), ensure_ascii=False)}\n\n"
                    f"静态路由建议:\n{json.dumps(route_targets, ensure_ascii=False)}\n\n"
                    f"是否达到配置的终止条件: {terminal_reached}\n\n"
                    f"最近已完成步骤:\n{json.dumps(recent_steps, ensure_ascii=False)}\n\n"
                    f"最新结果:\n{json.dumps(step_results, ensure_ascii=False)}"
                ),
            },
        ]

        try:
            response = await self.llm_gateway.chat(
                messages=decision_prompt,
                model_config=self._get_supervisor_model_config(),
                trace_context=self._trace_context(ctx),
            )
            decision = parse_json_object(response.content)
            if decision.get("verdict") == "accept":
                decision["done"] = True
            if decision.get("verdict") == "abort":
                self._record_decision(decision, terminal_reached, [], ctx)
                self._completion_status = "aborted"
                return []

            if decision.get("done", False):
                violations = self._completion_violations(decision, terminal_reached, ctx)
                self._record_decision(decision, terminal_reached, violations, ctx)
                if violations:
                    targets = self._decision_targets(decision.get("next", []))
                    if not targets:
                        targets = route_targets
                    if not targets:
                        self._completion_status = "verification_failed"
                    return targets
                self._completion_status = "completed"
                self._supervision_state(ctx)["completion_payload"] = self._terminal_payload(
                    step_results
                ) or (
                    step_results[-1].get("payload")
                    if step_results and isinstance(step_results[-1].get("payload"), dict)
                    else None
                )
                return []
            targets = self._decision_targets(decision.get("next", []))
            self._record_decision(decision, terminal_reached, [], ctx)
            if not targets:
                self._completion_status = "stalled"
            return targets
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("supervisor.decision_failed", error=str(e))
            if terminal_reached:
                fallback_decision = {
                    "verdict": "accept",
                    "done": True,
                    "reason": f"Supervisor 决策解析失败，尝试确定性验收: {e}",
                    "acceptance": {},
                    "next": [],
                }
                violations = self._completion_violations(fallback_decision, terminal_reached, ctx)
                self._record_decision(fallback_decision, terminal_reached, violations, ctx)
                if not violations:
                    self._completion_status = "completed"
                    self._supervision_state(ctx)["completion_payload"] = self._terminal_payload(
                        step_results
                    )
                    return []
            if not route_targets:
                self._completion_status = "error"
            return route_targets

    async def _handle_failure(
        self,
        failed_steps: list[dict],
        completed_steps: list[dict],
        initial_input: str,
        ctx: WorkflowContext,
    ) -> list[tuple[str, dict]]:
        """Supervisor 处理失败步骤 — 决定重试、换 agent、或终止"""
        route_targets = self._resolve_static_routes(failed_steps)
        if not self.llm_gateway:
            if not route_targets:
                self._completion_status = "error"
            self._record_decision(
                {
                    "verdict": "rework" if route_targets else "abort",
                    "done": False,
                    "reason": "Agent 返回 error，按静态失败路由处理。",
                    "next": [
                        {"agent_id": agent_id, "payload": payload}
                        for agent_id, payload in route_targets
                    ],
                },
                False,
                [],
                ctx,
            )
            return route_targets

        recent_steps = completed_steps[-self.supervisor_config.review_history_limit :]
        intervention_prompt = [
            {
                "role": "system",
                "content": (
                    f"{self._supervisor_instruction()}\n\n"
                    "工作流中有步骤失败。分析完整失败 payload 并决定:\n"
                    '{"action": "retry|reassign|skip|abort", '
                    '"targets": [{"agent_id": "...", "task": "...", "payload": {}}]}\n'
                    "- retry: 让同一 agent 重试\n"
                    "- reassign: 交给另一个 agent\n"
                    "- skip: 跳过该步骤继续\n"
                    "- abort: 终止工作流"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"原始需求: {initial_input}\n\n"
                    f"可用 Agent:\n{json.dumps(self._available_agents(), ensure_ascii=False)}\n\n"
                    f"静态失败路由建议:\n{json.dumps(route_targets, ensure_ascii=False)}\n\n"
                    f"失败步骤:\n{json.dumps(failed_steps, ensure_ascii=False)}\n\n"
                    f"最近已完成步骤:\n{json.dumps(recent_steps, ensure_ascii=False)}"
                ),
            },
        ]

        try:
            response = await self.llm_gateway.chat(
                messages=intervention_prompt,
                model_config=self._get_supervisor_model_config(),
                trace_context=self._trace_context(ctx),
            )
            decision = parse_json_object(response.content)
            action = decision.get("action", "abort")

            if action == "abort":
                self._record_decision(
                    {
                        "verdict": "abort",
                        "done": False,
                        "reason": decision.get("reason", "失败介入决定终止"),
                        "next": [],
                    },
                    False,
                    [],
                    ctx,
                )
                self._completion_status = "aborted"
                return []
            if action == "skip":
                # 跳过失败步骤，继续正常决策流程
                return await self._decide_next([], completed_steps, initial_input, ctx)
            # retry 或 reassign
            targets = self._decision_targets(decision.get("targets", []))
            self._record_decision(
                {
                    "verdict": "rework",
                    "done": False,
                    "reason": decision.get("reason", f"失败介入动作: {action}"),
                    "next": decision.get("targets", []),
                },
                False,
                [],
                ctx,
            )
            if not targets:
                self._completion_status = "stalled"
            return targets
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("supervisor.intervention_failed", error=str(e))
            if not route_targets:
                self._completion_status = "error"
            return route_targets

    async def _summarize(
        self,
        completed_steps: list[dict],
        initial_input: str,
        ctx: WorkflowContext,
    ) -> str:
        """让 Supervisor 做最终汇总"""
        if not self.llm_gateway:
            return json.dumps(completed_steps, ensure_ascii=False)

        summary_prompt = [
            {
                "role": "system",
                "content": (
                    f"{self._supervisor_instruction()}\n\n"
                    "汇总以下工作流的完整执行结果，说明关键判断、产物和未解决问题。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"原始需求: {initial_input}\n\n"
                    f"执行步骤与结果:\n"
                    f"{json.dumps(completed_steps, ensure_ascii=False)}"
                ),
            },
        ]

        try:
            response = await self.llm_gateway.chat(
                messages=summary_prompt,
                model_config=self._get_supervisor_model_config(),
                trace_context=self._trace_context(ctx),
            )
            return response.content
        except Exception:
            return json.dumps(completed_steps, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 静态路由解析
    # ------------------------------------------------------------------

    def _resolve_static_routes(self, step_results: list[dict]) -> list[tuple[str, dict]]:
        """根据 flow.routes 中的静态路由规则解析下一步目标

        如果路由表中存在匹配规则则返回目标列表，否则返回空列表
        以便调用方回退到 LLM 决策。
        """
        targets: list[tuple[str, dict]] = []
        for result in step_results:
            sender = result["agent"]
            result_payload = result.get("payload")
            if not isinstance(result_payload, dict):
                result_payload = result
            routes = self.config.flow.routes.get(sender, [])
            for route in routes:
                payload = map_route_payload(result_payload, route.payload_mapping)
                if route.condition is None:
                    targets.append((route.target, payload))
                else:
                    actual = result_payload.get(route.condition.field)
                    if route.condition.evaluate(actual):
                        targets.append((route.target, payload))
        return targets
