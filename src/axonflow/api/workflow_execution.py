"""Shared platform workflow execution used by manual and hosted runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from axonflow.api.ws import broadcaster
from axonflow.engine import AxonFlowEngine
from axonflow.platform.hosting import HostedCycleOutcome
from axonflow.platform.models import PlatformWorkflow
from axonflow.platform.store import PlatformStore

logger = structlog.get_logger()


async def _publish_event(
    store: PlatformStore,
    run_id: str,
    workflow_id: str,
    event_type: str,
    data: dict[str, Any],
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    event = {
        "type": event_type,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "timestamp": timestamp,
        "data": data,
    }
    store.record_event(run_id, event_type, data, timestamp)
    await broadcaster.broadcast(run_id, event)


async def execute_platform_workflow_run(
    engine: AxonFlowEngine,
    store: PlatformStore,
    workflow: PlatformWorkflow,
    input_data: str,
    run_id: str,
) -> HostedCycleOutcome:
    """Execute one complete platform run and persist the same evidence for every trigger."""
    workflow_id = workflow.id
    execution_ids: set[str] = set()
    store.create_run(run_id, workflow, input_data)
    trace_result: dict[str, Any] | None = None
    trace_error: str | None = None

    async def on_orchestrator_event(event_type: str, data: dict[str, Any]) -> None:
        if event_type == "workflow.context_ready":
            execution_id = str(data["execution_id"])
            execution_ids.add(execution_id)
            if engine._execution_logger is not None:
                engine._execution_logger.set_run_context(
                    execution_id,
                    run_id,
                    workflow_id,
                )
            return

        event_data = dict(data)
        agent_id = event_data.get("agent_id") or event_data.get("supervisor_agent_id")
        if isinstance(agent_id, str):
            node_id = workflow.node_id_for_agent(agent_id)
            if node_id:
                event_data["node_id"] = node_id
                if event_type == "node.task_assigned":
                    store.update_node_run(run_id, node_id, agent_id, "queued")
                elif event_type == "node.task_started":
                    store.update_node_run(run_id, node_id, agent_id, "running")
                elif event_type == "node.result_ready":
                    store.update_node_run(
                        run_id,
                        node_id,
                        agent_id,
                        "completed",
                        output=event_data.get("payload"),
                    )
                elif event_type == "node.error":
                    store.update_node_run(
                        run_id,
                        node_id,
                        agent_id,
                        "error",
                        output=event_data.get("payload"),
                        error=event_data.get("error"),
                    )
                elif event_type == "supervisor.review_started":
                    store.update_node_run(run_id, node_id, agent_id, "reviewing")
                elif event_type == "supervisor.decision_ready":
                    store.update_node_run(run_id, node_id, agent_id, "completed")
        await _publish_event(store, run_id, workflow_id, event_type, event_data)

    try:
        await engine.start_workflow_trace(run_id, workflow_id, input_data)
        await _publish_event(
            store,
            run_id,
            workflow_id,
            "workflow.started",
            {"input": input_data},
        )
        result = await engine.run_workflow(
            workflow_id,
            input_data,
            event_callback=on_orchestrator_event,
            run_id=run_id,
        )
        result_data = result.to_dict()
        trace_result = result_data
        store.complete_run(run_id, result.status, result_data)
        event_type = "workflow.completed" if result.status == "completed" else "workflow.failed"
        await _publish_event(store, run_id, workflow_id, event_type, result_data)
        return HostedCycleOutcome(
            run_id=run_id,
            status=result.status,
            result=result_data,
        )
    except asyncio.CancelledError:
        trace_error = "Workflow execution cancelled during shutdown"
        error = {"status": "error", "error": trace_error}
        store.complete_run(run_id, "error", error)
        await _publish_event(store, run_id, workflow_id, "workflow.failed", error)
        raise
    except Exception as exc:
        logger.exception("api.workflow_run_failed", workflow_id=workflow_id)
        trace_error = str(exc)
        error = {"status": "error", "error": trace_error}
        store.complete_run(run_id, "error", error)
        await _publish_event(store, run_id, workflow_id, "workflow.failed", error)
        return HostedCycleOutcome(
            run_id=run_id,
            status="error",
            result=error,
        )
    finally:
        await engine.finish_workflow_trace(
            run_id,
            result=trace_result,
            error=trace_error,
        )
        if engine._execution_logger is not None:
            for execution_id in execution_ids:
                engine._execution_logger.clear_run_id(execution_id)
