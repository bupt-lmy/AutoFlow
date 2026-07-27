"""编排器测试 — BaseOrchestrator / FlatOrchestrator / 工厂"""

import json
from types import SimpleNamespace

import pytest

from axonflow.config.models import (
    AgentConfig,
    FlowConfig,
    JoinConfig,
    ModelConfig,
    Route,
    RouteCondition,
    RoutePayloadMapping,
    SupervisorConfig,
    WorkflowConfig,
)
from axonflow.core.agent import AgentRegistry, BaseAgent
from axonflow.core.context import WorkflowContext
from axonflow.core.message import Message, MessageType
from axonflow.core.orchestrator_factory import (
    _ORCHESTRATOR_REGISTRY,
    create_orchestrator,
    register_orchestrator_type,
)
from axonflow.core.workflow import (
    BaseOrchestrator,
    FlatOrchestrator,
    WorkflowOrchestrator,
    WorkflowResult,
)
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry


def _make_agent(agent_id: str, bus: InMemoryMessageBus) -> BaseAgent:
    """创建测试用 Agent（不启动消息循环）"""
    config = AgentConfig(
        id=agent_id,
        name=f"Test {agent_id}",
        role="test",
        model=ModelConfig(provider="openai", name="gpt-4o-mini"),
    )
    return BaseAgent(
        config=config,
        message_bus=bus,
        llm_gateway=LLMGateway(),
        tool_registry=ToolRegistry(),
    )


def _make_registry(agent_ids: list[str], bus: InMemoryMessageBus) -> AgentRegistry:
    """创建含指定 agents 的注册表"""
    registry = AgentRegistry()
    for aid in agent_ids:
        registry.register(_make_agent(aid, bus))
    return registry


class TestWorkflowResult:
    def test_to_dict(self):
        result = WorkflowResult(
            workflow_id="wf-1",
            status="completed",
            output={"content": "ok"},
            iterations=3,
            duration_seconds=1.234,
        )
        d = result.to_dict()
        assert d["workflow_id"] == "wf-1"
        assert d["status"] == "completed"
        assert d["iterations"] == 3
        assert d["duration_seconds"] == 1.23


class TestBaseOrchestrator:
    def test_cannot_instantiate_abc(self):
        """BaseOrchestrator 是抽象类，不能直接实例化"""
        with pytest.raises(TypeError):
            BaseOrchestrator(
                config=WorkflowConfig(id="x", name="x", flow=FlowConfig(entry="a")),
                agent_registry=AgentRegistry(),
                message_bus=InMemoryMessageBus(),
            )

    def test_backward_compat_alias(self):
        """WorkflowOrchestrator 是 FlatOrchestrator 的别名"""
        assert WorkflowOrchestrator is FlatOrchestrator

    @pytest.mark.asyncio
    async def test_dispatch_adds_structured_task_command(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a"],
            flow=FlowConfig(entry="a"),
        )
        orchestrator = FlatOrchestrator(config, _make_registry(["a"], bus), bus)

        await orchestrator._dispatch(
            "a",
            {"task": "Review the project"},
            "session-1",
            "step-1",
        )
        message = await bus.receive("a", block_ms=100)

        assert message is not None
        assert message.protocol_version == "aip-lite/0.1"
        assert message.session_id == "session-1"
        assert message.payload["_protocol"]["command"]["command"] == "start"
        assert message.payload["_protocol"]["command"]["data_items"][0]["data"] == {
            "task": "Review the project"
        }

    @pytest.mark.asyncio
    async def test_agent_response_adds_structured_task_result(self):
        bus = InMemoryMessageBus()
        agent = _make_agent("a", bus)
        request = Message(
            sender="__orchestrator__",
            receiver="a",
            type=MessageType.TASK_REQUEST,
            payload={"task": "Review"},
            workflow_id="workflow-1",
            session_id="session-1",
            task_id="task-1",
        )

        await agent._send_response(
            request,
            {"status": "success", "content": "review complete"},
        )
        response = await bus.receive("__orchestrator__", block_ms=100)

        assert response is not None
        assert response.payload["task_result"]["task_id"] == "task-1"
        assert response.payload["task_result"]["status"]["state"] == "completed"
        assert response.payload["task_result"]["products"][0]["data_items"][0]["text"] == (
            "review complete"
        )


class TestFlatOrchestratorRouting:
    @pytest.mark.asyncio
    async def test_join_target_is_dispatched_exactly_once(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf-join-once",
            name="join once",
            agents=["root", "left", "right", "joined"],
            flow=FlowConfig(
                entry="root",
                max_iterations=2,
                routes={
                    "left": [Route(target="joined")],
                    "right": [Route(target="joined")],
                },
                join={"joined": JoinConfig(wait_for=["left", "right"], strategy="all")},
            ),
        )

        class RecordingOrchestrator(FlatOrchestrator):
            dispatched: list[str]

            async def _dispatch(self, target_id, *args, **kwargs):
                self.dispatched.append(target_id)

        orchestrator = RecordingOrchestrator(
            config,
            _make_registry(["root", "left", "right", "joined"], bus),
            bus,
        )
        orchestrator.dispatched = []
        for sender in ("left", "right"):
            await bus.send(
                Message(
                    sender=sender,
                    receiver="__orchestrator__",
                    type=MessageType.TASK_RESPONSE,
                    payload={"status": "success", "branch": sender},
                )
            )

        await orchestrator.execute("input")

        assert orchestrator.dispatched.count("joined") == 1

    @pytest.mark.asyncio
    async def test_error_terminal_returns_failed_workflow(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a"],
            flow=FlowConfig(
                entry="a",
                terminate_on=[{"agent": "a", "status": "error"}],
            ),
        )
        orchestrator = FlatOrchestrator(config, _make_registry(["a"], bus), bus)
        await bus.send(
            Message(
                sender="a",
                receiver="__orchestrator__",
                type=MessageType.ERROR,
                payload={"status": "error", "error": "step failed"},
            )
        )

        result = await orchestrator.execute("input")

        assert result.status == "failed"
        assert result.output["error"] == "step failed"

    @pytest.mark.asyncio
    async def test_resolve_next_unconditional(self):
        """无条件路由应始终匹配"""
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a", "b"],
            flow=FlowConfig(
                entry="a",
                routes={"a": [Route(target="b")]},
            ),
        )
        registry = _make_registry(["a", "b"], bus)
        orch = FlatOrchestrator(config, registry, bus)

        event = Message(
            sender="a",
            receiver="__orchestrator__",
            type=MessageType.TASK_RESPONSE,
            payload={"status": "success"},
        )
        targets = orch._resolve_next(event)
        assert len(targets) == 1
        assert targets[0][0] == "b"

    @pytest.mark.asyncio
    async def test_resolve_next_conditional(self):
        """条件路由应根据 payload 匹配"""
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a", "b", "c"],
            flow=FlowConfig(
                entry="a",
                routes={
                    "a": [
                        Route(
                            target="b",
                            condition=RouteCondition(
                                field="status", operator="eq", value="success"
                            ),
                        ),
                        Route(
                            target="c",
                            condition=RouteCondition(field="status", operator="eq", value="error"),
                        ),
                    ],
                },
            ),
        )
        registry = _make_registry(["a", "b", "c"], bus)
        orch = FlatOrchestrator(config, registry, bus)

        success_event = Message(
            sender="a",
            receiver="__orchestrator__",
            type=MessageType.TASK_RESPONSE,
            payload={"status": "success"},
        )
        targets = orch._resolve_next(success_event)
        assert len(targets) == 1
        assert targets[0][0] == "b"

        error_event = Message(
            sender="a",
            receiver="__orchestrator__",
            type=MessageType.TASK_RESPONSE,
            payload={"status": "error"},
        )
        targets = orch._resolve_next(error_event)
        assert len(targets) == 1
        assert targets[0][0] == "c"

    @pytest.mark.asyncio
    async def test_route_selects_payload_fields_and_promotes_task_field(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a", "b"],
            flow=FlowConfig(
                entry="a",
                routes={
                    "a": [
                        Route(
                            target="b",
                            condition=RouteCondition(field="status", value="error"),
                            payload_mapping=RoutePayloadMapping(
                                include=["content", "feedback"],
                                task_field="feedback",
                            ),
                        )
                    ]
                },
            ),
        )
        orch = FlatOrchestrator(config, _make_registry(["a", "b"], bus), bus)
        protocol = {"task_id": "parent-task"}
        event = Message(
            sender="a",
            receiver="__orchestrator__",
            type=MessageType.TASK_RESPONSE,
            payload={
                "status": "error",
                "content": "Tests failed",
                "feedback": "Fix empty input handling",
                "evidence": {"exit_code": 1},
                "_protocol": protocol,
            },
        )

        targets = orch._resolve_next(event)

        assert targets == [
            (
                "b",
                {
                    "content": "Tests failed",
                    "feedback": "Fix empty input handling",
                    "task": "Fix empty input handling",
                    "_protocol": protocol,
                },
            )
        ]


class TestOrchestratorFactory:
    def test_create_flat(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a"],
            flow=FlowConfig(mode="flat", entry="a"),
        )
        registry = _make_registry(["a"], bus)
        orch = create_orchestrator(config, registry, bus)
        assert isinstance(orch, FlatOrchestrator)

    def test_create_unknown_fallback(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a"],
            flow=FlowConfig(mode="nonexistent_mode", entry="a"),
        )
        registry = _make_registry(["a"], bus)
        orch = create_orchestrator(config, registry, bus)
        assert isinstance(orch, FlatOrchestrator)

    def test_create_supervisor(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="a",
                supervisor=SupervisorConfig(agent_id="supervisor"),
            ),
        )
        registry = _make_registry(["a", "supervisor"], bus)
        from axonflow.core.supervisor import SupervisorOrchestrator

        orch = create_orchestrator(config, registry, bus, llm_gateway=LLMGateway())
        assert isinstance(orch, SupervisorOrchestrator)


class TestSupervisorReview:
    @pytest.mark.asyncio
    async def test_supervisor_keeps_waiting_after_empty_receive(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        response = Message(
            sender="worker",
            receiver="__orchestrator__",
            type=MessageType.TASK_RESPONSE,
            payload={"status": "success", "content": "Done"},
            step_id="step-0",
        )

        class DelayedBus(InMemoryMessageBus):
            def __init__(self):
                super().__init__()
                self.receives = 0

            async def receive(self, agent_id, block_ms=0):
                if agent_id == "__orchestrator__":
                    self.receives += 1
                    return None if self.receives == 1 else response
                return await super().receive(agent_id, block_ms)

        bus = DelayedBus()
        config = WorkflowConfig(
            id="supervised",
            name="Supervised",
            agents=["worker", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                terminate_on=[{"agent": "worker", "status": "success"}],
                supervisor=SupervisorConfig(
                    agent_id="supervisor",
                    planning_enabled=False,
                ),
            ),
        )
        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "supervisor"], bus),
            bus,
        )

        result = await orchestrator.execute("Inspect AxonFlow")

        assert bus.receives == 2
        assert result.status == "completed"
        assert result.iterations == 1

    def test_initial_target_preserves_original_input_without_plan(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="supervised",
            name="Supervised",
            agents=["worker", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                supervisor=SupervisorConfig(agent_id="supervisor", planning_enabled=False),
            ),
        )
        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "supervisor"], bus),
            bus,
        )

        assert orchestrator._get_initial_targets(None, "Inspect AxonFlow") == [
            ("worker", {"task": "Inspect AxonFlow"})
        ]

    def test_supervisor_instruction_includes_assigned_skill(self, tmp_path):
        from axonflow.core.supervisor import SupervisorOrchestrator

        skill_dir = tmp_path / "skills" / "requirement-gate"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Gate\nRequire evidence.")
        bus = InMemoryMessageBus()
        registry = _make_registry(["worker"], bus)
        supervisor = _make_agent("supervisor", bus)
        supervisor.config.skills = ["requirement-gate"]
        supervisor._skills_dir = tmp_path / "skills"
        registry.register(supervisor)
        config = WorkflowConfig(
            id="supervised",
            name="Supervised",
            agents=["worker", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                supervisor=SupervisorConfig(agent_id="supervisor"),
            ),
        )
        orchestrator = SupervisorOrchestrator(config, registry, bus)

        instruction = orchestrator._supervisor_instruction()

        assert "审核 Skill" in instruction
        assert "Require evidence" in instruction

    @pytest.mark.asyncio
    async def test_supervisor_accepts_json_inside_markdown_fence(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="supervised",
            name="Supervised",
            agents=["worker", "reviewer", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                routes={"worker": [Route(target="reviewer")]},
                supervisor=SupervisorConfig(agent_id="supervisor"),
            ),
        )

        class Gateway:
            def __init__(self):
                self.kwargs = None

            async def chat(self, messages, **_kwargs):
                self.kwargs = _kwargs
                return SimpleNamespace(
                    content=(
                        "```json\n"
                        '{"done":false,"next":[{"agent_id":"reviewer",'
                        '"payload":{"task":"Review"}}]}\n'
                        "```"
                    )
                )

        gateway = Gateway()
        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "reviewer", "supervisor"], bus),
            bus,
            llm_gateway=gateway,  # type: ignore[arg-type]
            run_id="run-123",
        )

        ctx = WorkflowContext()
        targets = await orchestrator._decide_next(
            step_results=[
                {
                    "agent": "worker",
                    "status": "success",
                    "payload": {"status": "success", "content": "Ready"},
                }
            ],
            completed_steps=[],
            initial_input="Build",
            ctx=ctx,
        )

        assert targets == [("reviewer", {"task": "Review"})]
        trace_context = gateway.kwargs["trace_context"]
        assert trace_context.agent_id == "supervisor"
        assert trace_context.workflow_id == "supervised"
        assert trace_context.execution_id == ctx.workflow_id
        assert trace_context.run_id == "run-123"
        assert trace_context.trace_kind == "supervisor"

    @pytest.mark.asyncio
    async def test_static_route_is_reviewed_with_complete_payload(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="supervised",
            name="Supervised",
            agents=["worker", "reviewer", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                routes={"worker": [Route(target="reviewer")]},
                supervisor=SupervisorConfig(
                    agent_id="supervisor",
                    responsibility="Review every result before routing.",
                    capabilities=["quality review", "routing control"],
                ),
            ),
        )
        gateway_calls: list[list[dict]] = []

        class Gateway:
            async def chat(self, messages, **_kwargs):
                gateway_calls.append(messages)
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "done": False,
                            "reason": "Route after reviewing evidence",
                            "next": [
                                {
                                    "agent_id": "reviewer",
                                    "payload": {"task": "Inspect the implementation"},
                                }
                            ],
                        }
                    )
                )

        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "reviewer", "supervisor"], bus),
            bus,
            llm_gateway=Gateway(),  # type: ignore[arg-type]
        )
        result_payload = {
            "status": "success",
            "content": "Implemented feature",
            "evidence": {"files": ["app.py"], "tests": 12},
        }

        targets = await orchestrator._decide_next(
            step_results=[
                {
                    "agent": "worker",
                    "step_id": "step-1",
                    "message_type": "task_response",
                    "status": "success",
                    "content": "Implemented feature",
                    "payload": result_payload,
                }
            ],
            completed_steps=[],
            initial_input="Build the feature",
            ctx=WorkflowContext(),
        )

        assert targets == [("reviewer", {"task": "Inspect the implementation"})]
        assert len(gateway_calls) == 1
        prompt = "\n".join(str(message["content"]) for message in gateway_calls[0])
        assert "Review every result before routing" in prompt
        assert "evidence" in prompt
        assert "静态路由建议" in prompt

    def test_static_route_conditions_read_original_result_payload(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="supervised",
            name="Supervised",
            agents=["worker", "reviewer", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                routes={
                    "worker": [
                        Route(
                            target="reviewer",
                            condition=RouteCondition(field="status", value="success"),
                            payload_mapping=RoutePayloadMapping(
                                include=["content", "evidence"],
                                task_field="content",
                            ),
                        )
                    ]
                },
                supervisor=SupervisorConfig(agent_id="supervisor"),
            ),
        )
        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "reviewer", "supervisor"], bus),
            bus,
        )

        targets = orchestrator._resolve_static_routes(
            [
                {
                    "agent": "worker",
                    "status": "success",
                    "payload": {
                        "status": "success",
                        "content": "Implementation ready",
                        "evidence": {"tests": 12},
                        "tokens_used": 50,
                    },
                }
            ]
        )

        assert targets == [
            (
                "reviewer",
                {
                    "content": "Implementation ready",
                    "evidence": {"tests": 12},
                    "task": "Implementation ready",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_supervisor_rejects_unsupported_completion(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="strict-supervision",
            name="Strict supervision",
            agents=["worker", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                terminate_on=[{"agent": "worker", "status": "success"}],
                supervisor=SupervisorConfig(
                    agent_id="supervisor",
                    acceptance_criteria=["测试通过"],
                    require_terminal_candidate=True,
                    require_evidence=True,
                ),
            ),
        )

        class Gateway:
            async def chat(self, messages, **_kwargs):
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "verdict": "accept",
                            "done": True,
                            "reason": "Agent said success",
                            "acceptance": {
                                "passed": True,
                                "criteria": [
                                    {
                                        "criterion": "测试通过",
                                        "passed": True,
                                        "evidence": [],
                                    }
                                ],
                                "evidence": [],
                                "unresolved_issues": [],
                            },
                            "next": [],
                        }
                    )
                )

        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "supervisor"], bus),
            bus,
            llm_gateway=Gateway(),  # type: ignore[arg-type]
        )
        ctx = WorkflowContext()
        ctx.update_state("supervision", orchestrator._new_supervision_state())

        targets = await orchestrator._decide_next(
            step_results=[
                {
                    "agent": "worker",
                    "status": "success",
                    "payload": {"status": "success", "content": "Done"},
                }
            ],
            completed_steps=[],
            initial_input="Build",
            ctx=ctx,
            terminal_reached=True,
        )

        assert targets == []
        assert orchestrator._completion_status == "verification_failed"
        report = orchestrator._build_output(ctx, [], content="failed", status="error")[
            "supervision_report"
        ]
        assert report["completion_violations"] == ["Agent 未返回配置要求的结构化证据"]

    def test_supervisor_blocks_repeated_identical_decisions(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="bounded-supervision",
            name="Bounded supervision",
            agents=["worker", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                supervisor=SupervisorConfig(
                    agent_id="supervisor",
                    max_repeated_decisions=2,
                ),
            ),
        )
        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "supervisor"], bus),
            bus,
        )
        ctx = WorkflowContext()
        target = [("worker", {"task": "retry"})]

        assert orchestrator._guard_targets(target, ctx) == target
        assert orchestrator._guard_targets(target, ctx) == target
        assert orchestrator._guard_targets(target, ctx) == []
        assert orchestrator._completion_status == "blocked"
        assert "防死循环" in orchestrator._supervision_state(ctx)["blocked_reason"]

    @pytest.mark.asyncio
    async def test_supervisor_repairs_then_reverifies_before_completion(self):
        from axonflow.core.supervisor import SupervisorOrchestrator

        responses = {
            "worker": {"status": "success", "content": "Implemented, not tested"},
            "fixer": {
                "status": "success",
                "content": "Fixed the defect",
                "artifacts": [{"path": "app.py"}],
            },
            "verifier": {
                "status": "success",
                "content": "All tests passed",
                "test_results": {"passed": 12, "failed": 0},
            },
        }

        class AutoResponseBus(InMemoryMessageBus):
            async def send(self, message):
                await super().send(message)
                if message.type != MessageType.TASK_REQUEST:
                    return
                await super().send(
                    Message(
                        sender=message.receiver,
                        receiver="__orchestrator__",
                        type=MessageType.TASK_RESPONSE,
                        payload=responses[message.receiver],
                        workflow_id=message.workflow_id,
                        step_id=message.step_id,
                    )
                )

        class Gateway:
            def __init__(self):
                self.review = 0

            async def chat(self, messages, **_kwargs):
                system = str(messages[0]["content"])
                if "汇总以下工作流" in system:
                    return SimpleNamespace(content="修复完成且复验通过")
                self.review += 1
                decisions = [
                    {
                        "verdict": "rework",
                        "done": False,
                        "reason": "缺少测试证据",
                        "acceptance": {
                            "passed": False,
                            "criteria": [],
                            "evidence": [],
                            "unresolved_issues": ["尚未测试"],
                        },
                        "next": [
                            {
                                "agent_id": "fixer",
                                "payload": {"task": "修复实现问题"},
                            }
                        ],
                    },
                    {
                        "verdict": "continue",
                        "done": False,
                        "reason": "修复后必须复验",
                        "acceptance": {
                            "passed": False,
                            "criteria": [],
                            "evidence": ["artifacts"],
                            "unresolved_issues": ["等待复验"],
                        },
                        "next": [
                            {
                                "agent_id": "verifier",
                                "payload": {"task": "运行回归测试"},
                            }
                        ],
                    },
                    {
                        "verdict": "accept",
                        "done": True,
                        "reason": "终止节点和测试证据均满足",
                        "acceptance": {
                            "passed": True,
                            "criteria": [
                                {
                                    "criterion": "测试全部通过",
                                    "passed": True,
                                    "evidence": ["test_results.failed=0"],
                                }
                            ],
                            "evidence": ["test_results"],
                            "unresolved_issues": [],
                        },
                        "next": [],
                    },
                ]
                return SimpleNamespace(content=json.dumps(decisions[self.review - 1]))

        bus = AutoResponseBus()
        config = WorkflowConfig(
            id="repair-loop",
            name="Repair loop",
            agents=["worker", "fixer", "verifier", "supervisor"],
            flow=FlowConfig(
                mode="supervisor",
                entry="worker",
                max_iterations=6,
                terminate_on=[{"agent": "verifier", "status": "success"}],
                supervisor=SupervisorConfig(
                    agent_id="supervisor",
                    planning_enabled=False,
                    acceptance_criteria=["测试全部通过"],
                    require_terminal_candidate=True,
                    require_evidence=True,
                ),
            ),
        )
        orchestrator = SupervisorOrchestrator(
            config,
            _make_registry(["worker", "fixer", "verifier", "supervisor"], bus),
            bus,
            llm_gateway=Gateway(),  # type: ignore[arg-type]
        )

        result = await orchestrator.execute("实现并验证功能")

        assert result.status == "completed"
        assert result.iterations == 3
        assert result.output["test_results"] == {"passed": 12, "failed": 0}
        report = result.output["supervision_report"]
        assert report["review_cycles"] == 3
        assert report["attempts_by_agent"] == {
            "worker": 1,
            "fixer": 1,
            "verifier": 1,
        }
        assert [item["verdict"] for item in report["decision_history"]] == [
            "rework",
            "continue",
            "accept",
        ]
        assert {item["field"] for item in report["evidence"]} == {
            "artifacts",
            "test_results",
        }

    def test_register_custom_orchestrator(self):
        class CustomOrch(FlatOrchestrator):
            pass

        register_orchestrator_type("custom_test", CustomOrch)
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a"],
            flow=FlowConfig(mode="custom_test", entry="a"),
        )
        registry = _make_registry(["a"], bus)
        orch = create_orchestrator(config, registry, bus)
        assert isinstance(orch, CustomOrch)
        _ORCHESTRATOR_REGISTRY.pop("custom_test", None)

    def test_import_invalid_class_path(self):
        bus = InMemoryMessageBus()
        config = WorkflowConfig(
            id="wf",
            name="test",
            agents=["a"],
            flow=FlowConfig(mode="nonexistent.module.Class", entry="a"),
        )
        registry = _make_registry(["a"], bus)
        with pytest.raises(ImportError, match="Cannot import"):
            create_orchestrator(config, registry, bus)


class TestConfigModels:
    def test_join_config(self):
        cfg = JoinConfig(wait_for=["a", "b"], strategy="all")
        assert cfg.wait_for == ["a", "b"]
        assert cfg.strategy == "all"

    def test_supervisor_config(self):
        cfg = SupervisorConfig(agent_id="sup")
        assert cfg.planning_enabled is True
        assert cfg.intervention_on_failure is True
        assert cfg.max_attempts_per_agent == 4
        assert cfg.max_repeated_decisions == 2
        assert "evidence" in cfg.evidence_fields

    def test_flow_config_mode_default(self):
        cfg = FlowConfig(entry="a")
        assert cfg.mode == "flat"
        assert cfg.join == {}
        assert cfg.supervisor is None

    def test_workflow_config_extends(self):
        cfg = WorkflowConfig(
            id="wf",
            name="test",
            extends="base-wf",
            flow=FlowConfig(entry="a"),
        )
        assert cfg.extends == "base-wf"
