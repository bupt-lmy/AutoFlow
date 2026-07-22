"""Reusable model profile APIs."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from axonflow.api.deps import get_config_dir, get_engine, get_platform_store
from axonflow.config.models import ModelConfig

router = APIRouter(prefix="/api/model-profiles", tags=["model-profiles"])


class ModelProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    config: ModelConfig

    @model_validator(mode="after")
    def validate_api_key_environment(self) -> ModelProfileRequest:
        env_var = self.config.api_key_env
        if env_var and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_var):
            raise ValueError(
                "api_key_env must be an environment variable name, such as MINIMAX_API_KEY"
            )
        return self


@router.get("")
async def list_model_profiles() -> list[dict]:
    return [_safe_profile_response(item) for item in get_platform_store().list_model_profiles()]


@router.post("", status_code=201)
async def create_model_profile(body: ModelProfileRequest) -> dict:
    if (
        body.config.credential_id
        and not get_platform_store().get_credential(body.config.credential_id)
    ):
        raise HTTPException(status_code=422, detail="Credential not found")
    try:
        return get_platform_store().create_model_profile(
            body.name,
            body.config.model_dump(mode="json", exclude_none=True),
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/{profile_id}")
async def update_model_profile(profile_id: str, body: ModelProfileRequest) -> dict:
    if (
        body.config.credential_id
        and not get_platform_store().get_credential(body.config.credential_id)
    ):
        raise HTTPException(status_code=422, detail="Credential not found")
    try:
        profile = get_platform_store().update_model_profile(
            profile_id,
            body.name,
            body.config.model_dump(mode="json", exclude_none=True),
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if profile is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    _sync_profile_to_agents(profile_id, body.config)
    return profile


@router.delete("/{profile_id}", status_code=204)
async def delete_model_profile(profile_id: str) -> None:
    if not get_platform_store().delete_model_profile(profile_id):
        raise HTTPException(status_code=404, detail="Model profile not found")


def _agent_config_paths(agents_dir: Path) -> list[Path]:
    return sorted([*agents_dir.glob("*.yaml"), *agents_dir.glob("*/config.yaml")])


def _safe_profile_response(profile: dict) -> dict:
    """Do not return secret-like legacy api_key_env values to the browser."""
    response = {**profile, "config": dict(profile.get("config", {}))}
    env_var = response["config"].get("api_key_env")
    if env_var and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_var):
        response["config"]["api_key_env"] = None
        response["api_key_env_invalid"] = True
    return response


def _sync_profile_to_agents(profile_id: str, model: ModelConfig) -> None:
    """Keep Agent templates and running instances aligned with an edited profile."""
    agents_dir = get_config_dir() / "agents"
    model_payload = model.model_dump(mode="json", exclude_none=True)
    engine = get_engine()
    for target in _agent_config_paths(agents_dir):
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        agent_data = raw["agent"] if isinstance(raw.get("agent"), dict) else raw
        parameters = agent_data.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("model_profile_id") != profile_id:
            continue
        agent_data["model"] = model_payload
        target.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        agent_id = agent_data.get("id")
        agent = engine.agent_registry.get(agent_id) if isinstance(agent_id, str) else None
        if agent is not None:
            agent.config.model = model.model_copy(deep=True)
