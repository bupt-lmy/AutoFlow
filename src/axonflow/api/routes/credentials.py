"""Credential and provider configuration APIs.

The create endpoint accepts a plaintext secret once over the authenticated transport.
It is encrypted before persistence and never returned by this API.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from axonflow.api.deps import get_platform_store
from axonflow.llm.providers import provider_catalog

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


class CredentialCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=64)
    source: str
    secret: str | None = Field(default=None, min_length=1)
    env_var: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_source(self) -> CredentialCreateRequest:
        if self.source == "encrypted" and not self.secret:
            raise ValueError("secret is required for encrypted credentials")
        if self.source == "environment" and not self.env_var:
            raise ValueError("env_var is required for environment credentials")
        if self.source not in {"encrypted", "environment"}:
            raise ValueError("source must be encrypted or environment")
        return self


class CredentialUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=64)
    source: str
    secret: str | None = Field(default=None, min_length=1)
    env_var: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_source(self) -> CredentialUpdateRequest:
        if self.source == "environment" and not self.env_var:
            raise ValueError("env_var is required for environment credentials")
        if self.source not in {"encrypted", "environment"}:
            raise ValueError("source must be encrypted or environment")
        return self


@router.get("")
async def list_credentials() -> list[dict]:
    return get_platform_store().list_credentials()


@router.post("", status_code=201)
async def create_credential(body: CredentialCreateRequest) -> dict:
    try:
        return get_platform_store().create_credential(
            name=body.name,
            provider=body.provider,
            source=body.source,
            secret=body.secret,
            env_var=body.env_var,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/{credential_id}")
async def update_credential(
    credential_id: str,
    body: CredentialUpdateRequest,
) -> dict:
    try:
        credential = get_platform_store().update_credential(
            credential_id=credential_id,
            name=body.name,
            provider=body.provider,
            source=body.source,
            secret=body.secret,
            env_var=body.env_var,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return credential


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(credential_id: str) -> None:
    if not get_platform_store().delete_credential(credential_id):
        raise HTTPException(status_code=404, detail="Credential not found")


@router.get("/catalog/providers")
async def list_providers() -> list[dict]:
    return provider_catalog()
