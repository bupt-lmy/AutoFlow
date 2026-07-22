"""统一消息协议定义"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from axonflow.core.protocol import PROTOCOL_VERSION


class MessageType(StrEnum):
    """消息类型"""

    TASK_REQUEST = "task_request"
    TASK_RESPONSE = "task_response"
    FEEDBACK = "feedback"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
    CONTROL = "control"


class ControlAction(StrEnum):
    """控制指令"""

    PAUSE = "pause"
    RESUME = "resume"
    TERMINATE = "terminate"


@dataclass
class Message:
    """智能体间通信的统一消息格式"""

    sender: str
    receiver: str
    type: MessageType
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    workflow_id: str = ""
    step_id: str = ""
    priority: int = 5
    context: dict = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    ttl: int | None = None
    parent_message_id: str | None = None
    protocol_version: str = PROTOCOL_VERSION
    session_id: str = ""
    task_id: str = ""

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        data = {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "type": self.type.value,
            "priority": self.priority,
            "payload": self.payload,
            "context": self.context,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "parent_message_id": self.parent_message_id,
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
        }
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Message:
        """从 JSON 字符串反序列化"""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        data["type"] = MessageType(data["type"])
        return cls(**data)

    def reply(
        self,
        payload: dict,
        msg_type: MessageType = MessageType.TASK_RESPONSE,
    ) -> Message:
        """创建回复消息"""
        return Message(
            sender=self.receiver,
            receiver=self.sender,
            type=msg_type,
            payload=payload,
            workflow_id=self.workflow_id,
            step_id=self.step_id,
            parent_message_id=self.id,
            protocol_version=self.protocol_version,
            session_id=self.session_id,
            task_id=self.task_id,
        )
