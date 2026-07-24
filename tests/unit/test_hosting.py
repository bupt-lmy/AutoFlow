"""Continuous hosted workflow controller tests."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from axonflow.api.deps import set_hosting_manager, set_platform_store
from axonflow.api.routes.workflows import (
    HostingStartRequest,
    get_hosting_status,
    start_hosting,
)
from axonflow.config.models import HostingStopCondition
from axonflow.platform.hosting import HostedCycleOutcome, HostedWorkflowManager
from axonflow.platform.models import PlatformWorkflow, WorkflowNode
from axonflow.platform.store import PlatformStore


def _workflow(**hosting) -> PlatformWorkflow:
    return PlatformWorkflow(
        id="hosted-flow",
        name="Hosted flow",
        nodes=[
            WorkflowNode(
                id="worker",
                agent_id="worker",
                label="Worker",
                is_entry=True,
                config={"terminate_on_success": True},
            )
        ],
        hosting={"enabled": True, "max_cycles": 3, **hosting},
    )


def test_hosting_stop_condition_supports_nested_fields() -> None:
    condition = HostingStopCondition(field="output.review.done", operator="eq", value=True)

    assert condition.evaluate({"output": {"review": {"done": True}}}) is True
    assert condition.evaluate({"output": {"review": {"done": False}}}) is False
    assert condition.evaluate({"output": {}}) is False


async def test_hosted_workflow_stops_at_cycle_limit(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    store.save_workflow(_workflow(max_cycles=3))
    calls: list[int] = []

    async def runner(workflow_id: str, input_data: str, cycle: int) -> HostedCycleOutcome:
        calls.append(cycle)
        return HostedCycleOutcome(
            run_id=f"hosted-{cycle}",
            status="completed",
            result={"status": "completed", "output": {}},
        )

    manager = HostedWorkflowManager(store, runner)
    await manager.start("hosted-flow", "repeat this")
    while manager.get_state("hosted-flow").status == "running":
        await asyncio.sleep(0)

    state = manager.get_state("hosted-flow")
    assert calls == [1, 2, 3]
    assert state.status == "limit_reached"
    assert state.completed_cycles == 3
    assert state.input == "repeat this"
    store.close()


async def test_hosted_workflow_stops_on_result_condition(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    store.save_workflow(
        _workflow(
            max_cycles=10,
            stop_condition={"field": "output.done", "operator": "eq", "value": True},
        )
    )

    async def runner(workflow_id: str, input_data: str, cycle: int) -> HostedCycleOutcome:
        return HostedCycleOutcome(
            run_id=f"hosted-{cycle}",
            status="completed",
            result={"status": "completed", "output": {"done": cycle == 2}},
        )

    manager = HostedWorkflowManager(store, runner)
    await manager.start("hosted-flow")
    while manager.get_state("hosted-flow").status == "running":
        await asyncio.sleep(0)

    state = manager.get_state("hosted-flow")
    assert state.status == "condition_met"
    assert state.completed_cycles == 2
    store.close()


async def test_hosted_workflow_stops_after_abnormal_cycle(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    store.save_workflow(_workflow(max_cycles=5))

    async def runner(workflow_id: str, input_data: str, cycle: int) -> HostedCycleOutcome:
        return HostedCycleOutcome(
            run_id="hosted-error",
            status="timeout",
            result={"status": "timeout"},
        )

    manager = HostedWorkflowManager(store, runner)
    await manager.start("hosted-flow")
    while manager.get_state("hosted-flow").status == "running":
        await asyncio.sleep(0)

    state = manager.get_state("hosted-flow")
    assert state.status == "error"
    assert state.completed_cycles == 1
    assert state.last_run_status == "timeout"
    store.close()


async def test_hosted_workflow_honors_manual_stop_between_cycles(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    store.save_workflow(_workflow(max_cycles=50, interval_seconds=30))

    async def runner(workflow_id: str, input_data: str, cycle: int) -> HostedCycleOutcome:
        return HostedCycleOutcome(
            run_id=f"hosted-{cycle}",
            status="completed",
            result={"status": "completed", "output": {}},
        )

    manager = HostedWorkflowManager(store, runner)
    await manager.start("hosted-flow")
    while manager.get_state("hosted-flow").completed_cycles < 1:
        await asyncio.sleep(0)
    await manager.request_stop("hosted-flow")
    while manager.get_state("hosted-flow").status in {"running", "stopping"}:
        await asyncio.sleep(0)

    assert manager.get_state("hosted-flow").status == "stopped"
    store.close()


async def test_hosted_workflow_resumes_after_process_restart(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    store.save_workflow(_workflow(max_cycles=1))
    first_cycle_started = asyncio.Event()

    async def interrupted_runner(
        workflow_id: str,
        input_data: str,
        cycle: int,
    ) -> HostedCycleOutcome:
        first_cycle_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    first_manager = HostedWorkflowManager(store, interrupted_runner)
    await first_manager.start("hosted-flow", "resume me")
    await first_cycle_started.wait()
    await first_manager.shutdown()

    resumed_calls: list[tuple[str, int]] = []

    async def resumed_runner(
        workflow_id: str,
        input_data: str,
        cycle: int,
    ) -> HostedCycleOutcome:
        resumed_calls.append((input_data, cycle))
        return HostedCycleOutcome(
            run_id="hosted-resumed",
            status="completed",
            result={"status": "completed", "output": {}},
        )

    second_manager = HostedWorkflowManager(store, resumed_runner)
    await second_manager.resume()
    while second_manager.get_state("hosted-flow").status == "running":
        await asyncio.sleep(0)

    state = second_manager.get_state("hosted-flow")
    assert resumed_calls == [("resume me", 1)]
    assert state.status == "limit_reached"
    assert state.completed_cycles == 1
    store.close()


async def test_hosting_api_exposes_status_and_start_conflict(tmp_path) -> None:
    store = PlatformStore(tmp_path / "axonflow.db")
    disabled = _workflow()
    disabled.hosting.enabled = False
    store.save_workflow(disabled)

    async def runner(workflow_id: str, input_data: str, cycle: int) -> HostedCycleOutcome:
        return HostedCycleOutcome(
            run_id=f"hosted-{cycle}",
            status="completed",
            result={"status": "completed"},
        )

    manager = HostedWorkflowManager(store, runner)
    set_platform_store(store)
    set_hosting_manager(manager)

    state = await get_hosting_status("hosted-flow")
    assert state["status"] == "idle"
    with pytest.raises(HTTPException) as exc_info:
        await start_hosting("hosted-flow", HostingStartRequest())
    assert exc_info.value.status_code == 409
    assert "not enabled" in str(exc_info.value.detail)
    store.close()
