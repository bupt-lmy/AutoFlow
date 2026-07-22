"""Observable LLM span and LangSmith configuration APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from axonflow.api.deps import get_platform_store

router = APIRouter(prefix="/api/observability", tags=["observability"])


class ObservabilitySettingsRequest(BaseModel):
    langsmith_enabled: bool = False
    langsmith_project: str = Field(default="axonflow", min_length=1, max_length=200)
    langsmith_endpoint: str | None = None
    langsmith_credential_id: str | None = None
    content_policy: str = "masked_content"


@router.get("/settings")
async def get_settings() -> dict:
    return get_platform_store().get_observability_settings()


@router.put("/settings")
async def update_settings(body: ObservabilitySettingsRequest) -> dict:
    if body.content_policy not in {"metadata_only", "masked_content", "full_content"}:
        raise HTTPException(status_code=422, detail="Invalid content policy")
    if body.langsmith_enabled and not body.langsmith_credential_id:
        raise HTTPException(
            status_code=422,
            detail="A LangSmith credential is required when tracing is enabled",
        )
    if (
        body.langsmith_credential_id
        and not get_platform_store().get_credential(body.langsmith_credential_id)
    ):
        raise HTTPException(status_code=422, detail="LangSmith credential not found")
    return get_platform_store().save_observability_settings(body.model_dump())


@router.get("/spans")
async def list_spans(
    run_id: str | None = Query(None),
    workflow_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    trace_kind: str | None = Query(None),
    exclude_trace_kind: str | None = Query(None),
    attributed_only: bool = Query(False),
) -> list[dict]:
    return get_platform_store().list_llm_spans(
        run_id=run_id,
        workflow_id=workflow_id,
        agent_id=agent_id,
        trace_kind=trace_kind,
        exclude_trace_kind=exclude_trace_kind,
        attributed_only=attributed_only,
    )
