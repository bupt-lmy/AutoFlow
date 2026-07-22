"""Tests for the product-facing platform graph and SQLite persistence."""

from __future__ import annotations

from pathlib import Path

from axonflow.config.loader import load_all_workflow_configs
from axonflow.config.models import (
    FlowConfig,
    ModelConfig,
    Route,
    RouteCondition,
    RoutePayloadMapping,
    TriggerConfig,
    WorkflowConfig,
)
from axonflow.engine import AxonFlowEngine
from axonflow.llm.gateway import LLMGateway
from axonflow.llm.providers import resolve_model_name
from axonflow.platform.models import PlatformWorkflow, WorkflowNode
from axonflow.platform.store import PlatformStore


def test_all_repository_workflows_convert_to_platform_models() -> None:
    workflows_dir = Path(__file__).parents[2] / "config" / "workflows"

    for workflow in load_all_workflow_configs(workflows_dir):
        platform_workflow = PlatformWorkflow.from_workflow_config(workflow)
        assert platform_workflow.id == workflow.id


def test_runtime_config_projects_to_visual_graph_and_back() -> None:
    config = WorkflowConfig(
        id="research-flow",
        name="Research flow",
        agents=["researcher", "writer"],
        flow=FlowConfig(
            entry="researcher",
            routes={
                "researcher": [
                    Route(
                        target="writer",
                        condition=RouteCondition(field="status", operator="eq", value="success"),
                        payload_mapping=RoutePayloadMapping(
                            include=["content", "evidence"],
                            task_field="content",
                        ),
                    )
                ]
            },
        ),
        trigger=TriggerConfig(
            type="cron",
            cron="0 * * * *",
            timezone="Asia/Shanghai",
            input="Prepare the hourly research brief",
        ),
    )

    workflow = PlatformWorkflow.from_workflow_config(config)
    restored = workflow.to_workflow_config()

    assert workflow.nodes[0].is_entry is True
    assert workflow.edges[0].condition is not None
    assert workflow.edges[0].payload_mapping == {
        "include": ["content", "evidence"],
        "task_field": "content",
    }
    assert restored.flow.entry == "research-flow--node-researcher"
    assert restored.flow.routes["research-flow--node-researcher"][0].target == (
        "research-flow--node-writer"
    )
    assert restored.flow.routes["research-flow--node-researcher"][0].payload_mapping is not None
    assert restored.trigger.input == "Prepare the hourly research brief"


def test_visual_graph_projects_duplicate_templates_as_distinct_entities() -> None:
    workflow = PlatformWorkflow(
        id="review-flow",
        name="Review flow",
        nodes=[
            WorkflowNode(
                id="draft",
                agent_id="writer",
                label="Draft writer",
                is_entry=True,
                config={"responsibility": "Write the first draft."},
            ),
            WorkflowNode(
                id="revision",
                agent_id="writer",
                label="Revision writer",
                config={
                    "responsibility": "Rewrite after review feedback.",
                    "model_profile_id": "profile-fast",
                    "terminate_on_success": True,
                },
            ),
        ],
        edges=[{"id": "draft-to-revision", "source": "draft", "target": "revision"}],
    )

    runtime = workflow.to_workflow_config()
    scoped = AxonFlowEngine()._scope_agent_instances(runtime)
    restored = PlatformWorkflow.from_workflow_config(runtime)

    assert [instance.template_id for instance in runtime.agent_instances] == ["writer", "writer"]
    assert runtime.agent_instances[0].id != runtime.agent_instances[1].id
    assert runtime.flow.routes[runtime.agent_instances[0].id][0].target == (
        runtime.agent_instances[1].id
    )
    assert runtime.context["agent_role_overrides"][runtime.agent_instances[1].id] == (
        "Rewrite after review feedback."
    )
    assert scoped.agent_instances[0].id != runtime.agent_instances[0].id
    assert scoped.agent_instances[0].id != scoped.agent_instances[1].id
    assert restored.nodes[1].config["model_profile_id"] == "profile-fast"
    assert workflow.node_id_for_agent("review-flow--draft") == "draft"
    assert workflow.node_id_for_agent("run-abcd--review-flow--draft") == "draft"


def test_workflow_node_responsibility_projects_to_runtime_context() -> None:
    workflow = PlatformWorkflow(
        id="content-flow",
        name="Content flow",
        nodes=[
            WorkflowNode(
                id="node-writer",
                agent_id="writer",
                label="Writer",
                is_entry=True,
                config={"responsibility": "Draft a concise first version for the reviewer."},
            )
        ],
    )

    runtime = workflow.to_workflow_config()
    restored = PlatformWorkflow.from_workflow_config(runtime)

    assert runtime.context["agent_role_overrides"]["content-flow--node-writer"] == (
        "Draft a concise first version for the reviewer."
    )
    assert restored.nodes[0].config["responsibility"] == (
        "Draft a concise first version for the reviewer."
    )


def test_workflow_end_node_projects_to_runtime_termination() -> None:
    workflow = PlatformWorkflow(
        id="review-flow",
        name="Review flow",
        nodes=[
            WorkflowNode(
                id="node-writer",
                agent_id="writer",
                label="Writer",
                is_entry=True,
                config={"terminate_on_success": False},
            ),
            WorkflowNode(
                id="node-reviewer",
                agent_id="reviewer",
                label="Reviewer",
                config={"terminate_on_success": True},
            ),
        ],
        terminate_on=[{"agent": "writer", "status": "error"}],
    )

    runtime = workflow.to_workflow_config()
    restored = PlatformWorkflow.from_workflow_config(runtime)

    assert {tuple(condition.items()) for condition in runtime.flow.terminate_on} == {
        (("agent", "review-flow--node-writer"), ("status", "error")),
        (("agent", "review-flow--node-reviewer"), ("status", "success")),
    }
    assert restored.nodes[1].config["terminate_on_success"] is True


def test_dynamic_agent_node_round_trips_discovery_policy() -> None:
    workflow = PlatformWorkflow(
        id="dynamic-review",
        name="Dynamic review",
        nodes=[
            WorkflowNode(
                id="review-slot",
                node_type="discovery",
                agent_id=None,
                label="Runtime reviewer",
                is_entry=True,
                config={
                    "discovery": {
                        "description": "Review Python code for security issues",
                        "required_skills": ["code-review"],
                        "required_tools": ["file_read"],
                        "timeout_seconds": 45,
                    },
                    "terminate_on_success": True,
                },
            )
        ],
    )

    runtime = workflow.to_workflow_config()
    restored = PlatformWorkflow.from_workflow_config(runtime)

    instance = runtime.agent_instances[0]
    assert instance.template_id is None
    assert instance.discovery is not None
    assert instance.discovery.required_skills == ["code-review"]
    assert runtime.flow.entry == "dynamic-review--review-slot"
    assert runtime.flow.terminate_on == [
        {"agent": "dynamic-review--review-slot", "status": "success"}
    ]
    assert restored.nodes[0].node_type == "discovery"
    assert restored.nodes[0].agent_id is None
    assert restored.nodes[0].config["discovery"]["timeout_seconds"] == 45


def test_fixed_agent_node_round_trips_failover_policy() -> None:
    workflow = PlatformWorkflow(
        id="resilient-coding",
        name="Resilient coding",
        nodes=[
            WorkflowNode(
                id="coder",
                agent_id="primary-coder",
                label="Coder",
                is_entry=True,
                config={
                    "fallback_discovery": {
                        "description": "Implement and repair Python application code",
                        "required_tools": ["file_write"],
                    }
                },
            )
        ],
    )

    runtime = workflow.to_workflow_config()
    restored = PlatformWorkflow.from_workflow_config(runtime)

    instance = runtime.agent_instances[0]
    assert instance.template_id == "primary-coder"
    assert instance.fallback_discovery is not None
    assert instance.fallback_discovery.required_tools == ["file_write"]
    assert restored.nodes[0].config["fallback_discovery"]["description"].startswith(
        "Implement"
    )


def test_supervisor_configuration_round_trips_with_canvas_node_identity() -> None:
    workflow = PlatformWorkflow(
        id="supervised-review",
        name="Supervised review",
        mode="supervisor",
        nodes=[
            WorkflowNode(
                id="node-supervisor",
                agent_id="supervisor-template",
                label="Supervisor",
            ),
            WorkflowNode(
                id="node-worker",
                agent_id="worker-template",
                label="Worker",
                is_entry=True,
                config={"terminate_on_success": True},
            ),
        ],
        supervisor={
            "agent_id": "node-supervisor",
            "responsibility": "Review every complete result and control routing.",
            "capabilities": ["quality review", "failure recovery"],
            "planning_enabled": True,
            "intervention_on_failure": True,
        },
    )

    runtime = workflow.to_workflow_config()
    restored = PlatformWorkflow.from_workflow_config(runtime)

    assert runtime.flow.supervisor is not None
    assert runtime.flow.supervisor.agent_id == "supervised-review--node-supervisor"
    assert runtime.flow.supervisor.capabilities == ["quality review", "failure recovery"]
    assert restored.supervisor is not None
    assert restored.supervisor["agent_id"] == "node-supervisor"
    assert restored.supervisor["responsibility"].startswith("Review every")


def test_store_persists_workflow_runs_nodes_and_events(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    workflow = PlatformWorkflow(
        id="demo",
        name="Demo",
        nodes=[WorkflowNode(id="node-writer", agent_id="writer", label="Writer", is_entry=True)],
        edges=[],
    )
    store.save_workflow(workflow)
    store.create_run("run-1", workflow, "Draft an update")
    store.update_node_run("run-1", "node-writer", "writer", "running")
    store.update_node_run(
        "run-1",
        "node-writer",
        "writer",
        "completed",
        output={"status": "success", "content": "Done"},
    )
    store.record_event(
        "run-1",
        "node.result_ready",
        {"node_id": "node-writer"},
        "2026-01-01T00:00:00+00:00",
    )
    store.complete_run("run-1", "completed", {"iterations": 1})

    detail = store.get_run("demo", "run-1")
    assert detail is not None
    assert detail["status"] == "completed"
    assert detail["workflow_snapshot"]["id"] == "demo"
    assert detail["node_runs"][0]["status"] == "completed"
    assert detail["node_runs"][0]["output"]["content"] == "Done"
    assert detail["node_runs"][0]["error"] is None
    assert detail["events"][0]["type"] == "node.result_ready"
    store.close()


def test_store_clears_recovered_node_error(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    workflow = PlatformWorkflow(
        id="demo",
        name="Demo",
        nodes=[WorkflowNode(id="node-tester", agent_id="tester", label="Tester", is_entry=True)],
        edges=[],
    )
    store.create_run("run-1", workflow, "Test the change")
    store.update_node_run("run-1", "node-tester", "tester", "error", error="First attempt")
    store.update_node_run(
        "run-1",
        "node-tester",
        "tester",
        "completed",
        output={"status": "success"},
    )

    detail = store.get_run("demo", "run-1")
    assert detail is not None
    assert detail["node_runs"][0]["status"] == "completed"
    assert detail["node_runs"][0]["error"] is None
    store.close()


def test_store_encrypts_credentials_and_persists_llm_spans(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    credential = store.create_credential(
        name="qwen-production",
        provider="dashscope",
        source="encrypted",
        secret="sk-secret-value",
    )

    assert credential["masked_value"] == "sk-s****alue"
    assert "secret" not in credential
    assert store.resolve_credential(credential["id"])["secret"] == "sk-secret-value"

    store.create_llm_span(
        {
            "id": "span-1",
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "execution_id": "execution-1",
            "agent_id": "agent-writer",
            "trace_kind": "agent",
            "provider": "dashscope",
            "model": "dashscope/qwen-plus",
            "credential_id": credential["id"],
            "started_at": "2026-01-01T00:00:00+00:00",
            "input_preview": "1 messages, 12 characters",
        }
    )
    store.complete_llm_span(
        "span-1",
        {
            "status": "completed",
            "completed_at": "2026-01-01T00:00:01+00:00",
            "latency_ms": 1000,
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "output_preview": "1 messages, 4 characters",
        },
    )

    span = store.list_llm_spans(run_id="run-1")[0]
    assert span["status"] == "completed"
    assert span["total_tokens"] == 30
    assert span["trace_kind"] == "agent"

    store.create_llm_span(
        {
            "id": "span-health",
            "workflow_id": "__health__",
            "execution_id": "health:agent-writer",
            "agent_id": "agent-writer",
            "trace_kind": "health",
            "provider": "dashscope",
            "model": "dashscope/qwen-plus",
            "started_at": "2026-01-01T00:00:02+00:00",
        }
    )
    store.create_llm_span(
        {
            "id": "span-unscoped",
            "trace_kind": "unscoped",
            "provider": "dashscope",
            "model": "dashscope/qwen-plus",
            "started_at": "2026-01-01T00:00:03+00:00",
        }
    )
    visible = store.list_llm_spans(exclude_trace_kind="health")
    assert [item["id"] for item in visible] == ["span-unscoped", "span-1"]
    attributed = store.list_llm_spans(attributed_only=True)
    assert {item["id"] for item in attributed} == {"span-1", "span-health"}
    store.close()


def test_store_updates_credentials_without_exposing_or_unintentionally_rotating_secret(
    tmp_path,
) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    credential = store.create_credential(
        name="minimax-old",
        provider="minimax",
        source="encrypted",
        secret="sk-original-secret",
    )

    updated = store.update_credential(
        credential["id"],
        name="minimax-production",
        provider="minimax",
        source="encrypted",
    )

    assert updated is not None
    assert updated["name"] == "minimax-production"
    assert "secret" not in updated
    assert store.resolve_credential(credential["id"])["secret"] == "sk-original-secret"

    rotated = store.update_credential(
        credential["id"],
        name="minimax-production",
        provider="minimax",
        source="encrypted",
        secret="sk-rotated-secret",
    )
    assert rotated is not None
    assert store.resolve_credential(credential["id"])["secret"] == "sk-rotated-secret"
    store.close()


def test_store_persists_reusable_model_profiles(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    credential = store.create_credential(
        name="minimax-production",
        provider="minimax",
        source="environment",
        env_var="MINIMAX_API_KEY",
    )
    profile = store.create_model_profile(
        "minimax-m3",
        {
            "provider": "minimax",
            "name": "MiniMax-M3",
            "credential_id": credential["id"],
            "api_base": "https://api.minimaxi.com/v1",
            "temperature": 0.2,
            "max_tokens": 2048,
            "timeout": 60,
        },
    )

    stored = store.get_model_profile(profile["id"])
    assert stored is not None
    assert stored["name"] == "minimax-m3"
    assert stored["config"]["credential_id"] == credential["id"]
    assert store.list_model_profiles()[0]["id"] == profile["id"]

    updated = store.update_model_profile(
        profile["id"],
        "minimax-m3-updated",
        {**profile["config"], "temperature": 0.4},
    )
    assert updated is not None
    assert updated["name"] == "minimax-m3-updated"
    assert updated["config"]["temperature"] == 0.4
    store.close()


def test_agent_model_override_wins_over_global_default() -> None:
    gateway = LLMGateway(default_model=ModelConfig(provider="openai", name="gpt-4o"))
    selected = gateway._select_model_config(
        ModelConfig(provider="dashscope", name="qwen-plus"),
        prefer_default=True,
    )

    assert selected.provider == "dashscope"
    assert resolve_model_name("dashscope", "qwen-plus") == "dashscope/qwen-plus"
    assert resolve_model_name("openai_compatible", "custom-model", "https://example.test/v1") == (
        "openai/custom-model"
    )
