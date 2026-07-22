"""执行日志 API"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query

from axonflow.api.deps import get_engine

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def get_logs(
    workflow_id: str | None = Query(None),
    run_id: str | None = Query(None),
    execution_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    action: str | None = Query(None),
):
    engine = get_engine()
    if engine._execution_logger is None:
        return []
    entries = engine._execution_logger.get_entries(
        workflow_id=workflow_id,
        run_id=run_id,
        execution_id=execution_id,
        agent_id=agent_id,
        action=action,
    )
    return [asdict(e) for e in entries]


@router.get("/{run_id}")
async def get_run_logs(run_id: str):
    engine = get_engine()
    if engine._execution_logger is None:
        return []
    entries = engine._execution_logger.get_entries(run_id=run_id)
    return [asdict(e) for e in entries]
