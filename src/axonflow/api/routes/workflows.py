"""Platform workflow API: visual definitions, runs, and live events."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from axonflow.api.deps import (
    get_config_dir,
    get_engine,
    get_hosting_manager,
    get_platform_store,
)
from axonflow.api.workflow_execution import execute_platform_workflow_run
from axonflow.config.loader import load_all_agent_configs, load_all_workflow_configs
from axonflow.config.models import WorkflowConfig
from axonflow.platform.models import PlatformWorkflow

logger = structlog.get_logger()
router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class RunRequest(BaseModel):
    input: str = "Hello"


class HostingStartRequest(BaseModel):
    input: str | None = None


class WorkflowUpdateRequest(BaseModel):
    """Visual graph update, with YAML accepted for existing API clients."""

    workflow: PlatformWorkflow | None = None
    yaml_content: str | None = None

    @model_validator(mode="after")
    def validate_update(self) -> WorkflowUpdateRequest:
        if (self.workflow is None) == (self.yaml_content is None):
            raise ValueError("Provide exactly one of workflow or yaml_content")
        return self


class WorkflowCreateRequest(BaseModel):
    workflow: PlatformWorkflow


_WORKFLOW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


def _response(workflow: PlatformWorkflow) -> dict[str, Any]:
    payload = workflow.model_dump(mode="json")
    payload["agent_count"] = len(workflow.nodes)
    return payload


def _config_for_id(workflow_id: str) -> WorkflowConfig:
    configs = load_all_workflow_configs(get_config_dir() / "workflows")
    for config in configs:
        if config.id == workflow_id:
            return config
    raise HTTPException(status_code=404, detail=f"Workflow not found: {workflow_id}")


def _get_or_seed_workflow(workflow_id: str) -> PlatformWorkflow:
    store = get_platform_store()
    workflow = store.get_workflow(workflow_id)
    if workflow is not None:
        return workflow
    workflow = PlatformWorkflow.from_workflow_config(_config_for_id(workflow_id))
    store.save_workflow(workflow)
    return workflow


def _find_workflow_file(workflow_id: str) -> Path:
    workflow_dir = get_config_dir() / "workflows"
    for path in list(workflow_dir.glob("*.yaml")) + list(workflow_dir.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            config = raw.get("workflow", raw)
            if config.get("id") == workflow_id:
                return path
        except (OSError, yaml.YAMLError, AttributeError):
            continue
    return workflow_dir / f"{workflow_id}.yaml"


def _write_runtime_config(workflow: PlatformWorkflow) -> None:
    """Keep YAML as the runtime/CLI source while SQLite retains visual metadata."""
    path = _find_workflow_file(workflow.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime = workflow.to_workflow_config().model_dump(mode="json")
    path.write_text(
        yaml.safe_dump({"workflow": runtime}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _validate_agents(workflow: PlatformWorkflow) -> None:
    if not workflow.nodes:
        raise HTTPException(
            status_code=422,
            detail="A workflow must contain at least one Agent node",
        )
    available = {agent.id for agent in load_all_agent_configs(get_config_dir() / "agents")}
    missing = sorted(
        {
            node.agent_id
            for node in workflow.nodes
            if node.node_type == "agent" and node.agent_id is not None
        }
        - available
    )
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown Agent IDs: {', '.join(missing)}")


@router.get("")
async def list_workflows() -> list[dict[str, Any]]:
    store = get_platform_store()
    # Existing YAML workflows are materialized once; subsequent edits retain canvas positions.
    for config in load_all_workflow_configs(get_config_dir() / "workflows"):
        if store.get_workflow(config.id) is None:
            store.save_workflow(PlatformWorkflow.from_workflow_config(config))
    return [_response(workflow) for workflow in store.list_workflows()]


@router.post("", status_code=201)
async def create_workflow(body: WorkflowCreateRequest) -> dict[str, Any]:
    """Persist a visual workflow and make it immediately runnable."""
    workflow = body.workflow
    if not _WORKFLOW_ID_PATTERN.fullmatch(workflow.id):
        raise HTTPException(
            status_code=422,
            detail=(
                "Workflow ID must use lowercase letters, numbers, and hyphens, "
                "and start with a letter"
            ),
        )
    if not workflow.name.strip():
        raise HTTPException(status_code=422, detail="Workflow name is required")
    workflow.name = workflow.name.strip()

    store = get_platform_store()
    exists_in_config = any(
        config.id == workflow.id
        for config in load_all_workflow_configs(get_config_dir() / "workflows")
    )
    if store.get_workflow(workflow.id) is not None or exists_in_config:
        raise HTTPException(status_code=409, detail=f"Workflow already exists: {workflow.id}")

    _validate_agents(workflow)
    _write_runtime_config(workflow)
    store.save_workflow(workflow)
    get_engine().sync_workflow_schedule(workflow.to_workflow_config())
    return _response(workflow)


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str) -> dict[str, Any]:
    return _response(_get_or_seed_workflow(workflow_id))


@router.get("/{workflow_id}/hosting")
async def get_hosting_status(workflow_id: str) -> dict[str, Any]:
    _get_or_seed_workflow(workflow_id)
    return get_hosting_manager().get_state(workflow_id).model_dump(mode="json")


@router.post("/{workflow_id}/hosting/start")
async def start_hosting(
    workflow_id: str,
    body: HostingStartRequest,
) -> dict[str, Any]:
    _get_or_seed_workflow(workflow_id)
    try:
        state = await get_hosting_manager().start(workflow_id, body.input)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.model_dump(mode="json")


@router.post("/{workflow_id}/hosting/stop")
async def stop_hosting(workflow_id: str) -> dict[str, Any]:
    _get_or_seed_workflow(workflow_id)
    state = await get_hosting_manager().request_stop(workflow_id)
    return state.model_dump(mode="json")


@router.put("/{workflow_id}")
async def update_workflow(workflow_id: str, body: WorkflowUpdateRequest) -> dict[str, Any]:
    if body.workflow is not None:
        if body.workflow.id != workflow_id:
            raise HTTPException(status_code=422, detail="Workflow ID cannot be changed")
        workflow = body.workflow
    else:
        try:
            raw = yaml.safe_load(body.yaml_content or "") or {}
            config_data = raw.get("workflow", raw)
            workflow = PlatformWorkflow.from_workflow_config(WorkflowConfig(**config_data))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if workflow.id != workflow_id:
            raise HTTPException(status_code=422, detail="Workflow ID cannot be changed")

    _validate_agents(workflow)
    _write_runtime_config(workflow)
    get_platform_store().save_workflow(workflow)
    get_engine().sync_workflow_schedule(workflow.to_workflow_config())
    return _response(workflow)


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, body: RunRequest) -> dict[str, str]:
    engine = get_engine()
    store = get_platform_store()
    workflow = _get_or_seed_workflow(workflow_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    asyncio.create_task(
        execute_platform_workflow_run(
            engine,
            store,
            workflow,
            body.input,
            run_id,
        )
    )
    return {"run_id": run_id, "workflow_id": workflow_id, "status": "started"}


@router.get("/{workflow_id}/runs")
async def get_runs(workflow_id: str) -> list[dict[str, Any]]:
    _get_or_seed_workflow(workflow_id)
    return get_platform_store().list_runs(workflow_id)


@router.get("/{workflow_id}/runs/{run_id}")
async def get_run(workflow_id: str, run_id: str) -> dict[str, Any]:
    run = get_platform_store().get_run(workflow_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run
