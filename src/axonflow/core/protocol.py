"""AIP-inspired task, content, and delegation models used inside AxonFlow."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

PROTOCOL_VERSION = "aip-lite/0.1"


class TaskState(StrEnum):
    ACCEPTED = "accepted"
    WORKING = "working"
    AWAITING_INPUT = "awaiting-input"
    AWAITING_COMPLETION = "awaiting-completion"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"


class TaskCommandType(StrEnum):
    START = "start"
    CONTINUE = "continue"
    CANCEL = "cancel"
    COMPLETE = "complete"
    GET = "get"


class DataItem(BaseModel):
    type: Literal["text", "file", "data"]
    text: str | None = None
    name: str | None = None
    mime_type: str | None = None
    uri: str | None = None
    bytes: str | None = None
    data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_content(self) -> DataItem:
        if self.type == "text" and self.text is None:
            raise ValueError("text DataItem requires text")
        if self.type == "file" and self.uri is None and self.bytes is None:
            raise ValueError("file DataItem requires uri or bytes")
        if self.type == "data" and self.data is None:
            raise ValueError("data DataItem requires data")
        return self


class Product(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str | None = None
    description: str | None = None
    data_items: list[DataItem] = Field(default_factory=list)


class TaskStatus(BaseModel):
    state: TaskState
    changed_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    data_items: list[DataItem] = Field(default_factory=list)


class TaskCommand(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    task_id: str
    command: TaskCommandType
    sender_id: str
    data_items: list[DataItem] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class TaskResult(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    task_id: str
    sender_id: str
    status: TaskStatus
    products: list[Product] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DelegationRequest(BaseModel):
    """A capability request that can be resolved by a discovery provider."""

    description: str = Field(min_length=1)
    required_skills: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    exclude_agents: list[str] = Field(default_factory=list)
    max_candidates: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.05, ge=0, le=1)


def protocol_context(
    *,
    session_id: str,
    task_id: str,
    requested_capability: str | None = None,
    selected_agent: str | None = None,
    attempt: int = 1,
    previous_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the interoperable metadata added to a task payload and prompt."""

    return {
        "version": PROTOCOL_VERSION,
        "session_id": session_id,
        "task_id": task_id,
        "requested_capability": requested_capability,
        "selected_agent": selected_agent,
        "attempt": attempt,
        "previous_attempts": previous_attempts or [],
    }
