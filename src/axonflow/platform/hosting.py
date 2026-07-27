"""Persistent controller for continuously hosted workflow execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from axonflow.platform.store import PlatformStore

logger = structlog.get_logger()

HostingStatus = Literal[
    "idle",
    "running",
    "stopping",
    "stopped",
    "condition_met",
    "limit_reached",
    "error",
]


class HostedCycleOutcome(BaseModel):
    run_id: str
    status: str
    result: dict[str, Any]


class HostedWorkflowState(BaseModel):
    workflow_id: str
    status: HostingStatus = "idle"
    input: str = ""
    completed_cycles: int = 0
    current_cycle: int | None = None
    last_run_id: str | None = None
    last_run_status: str | None = None
    last_error: str | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    updated_at: str | None = None


CycleRunner = Callable[[str, str, int], Awaitable[HostedCycleOutcome]]


class HostedWorkflowManager:
    """Run complete workflow executions until a limit, condition, error, or manual stop."""

    def __init__(self, store: PlatformStore, cycle_runner: CycleRunner) -> None:
        self._store = store
        self._cycle_runner = cycle_runner
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    def get_state(self, workflow_id: str) -> HostedWorkflowState:
        persisted = self._store.get_hosting_state(workflow_id)
        if persisted is None:
            return HostedWorkflowState(workflow_id=workflow_id)
        return HostedWorkflowState.model_validate(persisted)

    async def start(
        self,
        workflow_id: str,
        input_override: str | None = None,
    ) -> HostedWorkflowState:
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"Workflow not found: {workflow_id}")
        if not workflow.hosting.enabled:
            raise ValueError("Hosted mode is not enabled for this workflow")

        async with self._lock:
            active = self._tasks.get(workflow_id)
            if active is not None and not active.done():
                return self.get_state(workflow_id)

            state = HostedWorkflowState(
                workflow_id=workflow_id,
                status="running",
                input=workflow.hosting.input if input_override is None else input_override,
                completed_cycles=0,
                started_at=self._now(),
            )
            self._save(state)
            self._launch(workflow_id, state)
            return state

    async def request_stop(self, workflow_id: str) -> HostedWorkflowState:
        async with self._lock:
            state = self.get_state(workflow_id)
            task = self._tasks.get(workflow_id)
            if task is None or task.done():
                if state.status in {"running", "stopping"}:
                    state.status = "stopped"
                    state.stopped_at = self._now()
                    self._save(state)
                return state
            state.status = "stopping"
            self._save(state)
            self._stop_events[workflow_id].set()
            return state

    async def resume(self) -> None:
        """Resume loops that were running before an API process restart."""
        async with self._lock:
            for payload in self._store.list_active_hosting_states():
                state = HostedWorkflowState.model_validate(payload)
                if state.status == "stopping":
                    state.status = "stopped"
                    state.stopped_at = self._now()
                    self._save(state)
                    continue
                workflow = self._store.get_workflow(state.workflow_id)
                if workflow is None or not workflow.hosting.enabled:
                    state.status = "stopped"
                    state.stopped_at = self._now()
                    self._save(state)
                    continue
                self._launch(state.workflow_id, state)

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = [task for task in self._tasks.values() if not task.done()]
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _launch(self, workflow_id: str, state: HostedWorkflowState) -> None:
        stop_event = asyncio.Event()
        self._stop_events[workflow_id] = stop_event
        self._tasks[workflow_id] = asyncio.create_task(
            self._run_loop(workflow_id, state, stop_event)
        )

    async def _run_loop(
        self,
        workflow_id: str,
        state: HostedWorkflowState,
        stop_event: asyncio.Event,
    ) -> None:
        try:
            while True:
                workflow = self._store.get_workflow(workflow_id)
                if workflow is None or not workflow.hosting.enabled:
                    self._finish(state, "stopped")
                    return
                policy = workflow.hosting
                if state.completed_cycles >= policy.max_cycles:
                    self._finish(state, "limit_reached")
                    return
                if stop_event.is_set():
                    self._finish(state, "stopped")
                    return

                cycle = state.completed_cycles + 1
                state.status = "running"
                state.current_cycle = cycle
                state.last_error = None
                self._save(state)

                try:
                    outcome = await self._cycle_runner(workflow_id, state.input, cycle)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state.last_error = str(exc)
                    self._finish(state, "error")
                    logger.exception(
                        "hosting.cycle_failed",
                        workflow_id=workflow_id,
                        cycle=cycle,
                    )
                    return

                state.completed_cycles = cycle
                state.current_cycle = None
                state.last_run_id = outcome.run_id
                state.last_run_status = outcome.status
                self._save(state)

                if policy.stop_on_error and outcome.status != "completed":
                    state.last_error = (
                        str(outcome.result.get("error"))
                        if outcome.result.get("error")
                        else f"Workflow cycle ended with status: {outcome.status}"
                    )
                    self._finish(state, "error")
                    return
                if policy.stop_condition and policy.stop_condition.evaluate(outcome.result):
                    self._finish(state, "condition_met")
                    return
                if state.completed_cycles >= policy.max_cycles:
                    self._finish(state, "limit_reached")
                    return
                if stop_event.is_set():
                    self._finish(state, "stopped")
                    return
                if policy.interval_seconds > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=policy.interval_seconds,
                        )
                    if stop_event.is_set():
                        self._finish(state, "stopped")
                        return
        except asyncio.CancelledError:
            # Process shutdown keeps the running marker so resume() can recover it.
            self._save(state)
            raise
        finally:
            self._tasks.pop(workflow_id, None)
            self._stop_events.pop(workflow_id, None)

    def _finish(self, state: HostedWorkflowState, status: HostingStatus) -> None:
        state.status = status
        state.current_cycle = None
        state.stopped_at = self._now()
        self._save(state)

    def _save(self, state: HostedWorkflowState) -> None:
        payload = state.model_dump(mode="json", exclude={"updated_at"})
        saved = self._store.save_hosting_state(
            state.workflow_id,
            state.status,
            payload,
        )
        state.updated_at = saved["updated_at"]

    @staticmethod
    def _now() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()
