"""Message 数据模型测试"""

from axonflow.core.message import Message, MessageType


class TestMessage:
    def test_create_message(self):
        msg = Message(
            sender="agent-a",
            receiver="agent-b",
            type=MessageType.TASK_REQUEST,
            payload={"task": "hello"},
        )
        assert msg.sender == "agent-a"
        assert msg.receiver == "agent-b"
        assert msg.type == MessageType.TASK_REQUEST
        assert msg.payload == {"task": "hello"}
        assert msg.id  # UUID 自动生成

    def test_serialize_deserialize(self):
        msg = Message(
            sender="agent-a",
            receiver="agent-b",
            type=MessageType.TASK_RESPONSE,
            payload={"result": "ok", "data": [1, 2, 3]},
            workflow_id="wf-001",
            session_id="session-001",
            task_id="task-001",
        )
        json_str = msg.to_json()
        restored = Message.from_json(json_str)

        assert restored.sender == msg.sender
        assert restored.receiver == msg.receiver
        assert restored.type == msg.type
        assert restored.payload == msg.payload
        assert restored.workflow_id == msg.workflow_id
        assert restored.protocol_version == "aip-lite/0.1"
        assert restored.session_id == "session-001"
        assert restored.task_id == "task-001"
        assert restored.id == msg.id

    def test_reply(self):
        original = Message(
            sender="agent-a",
            receiver="agent-b",
            type=MessageType.TASK_REQUEST,
            payload={"task": "do something"},
            workflow_id="wf-001",
            session_id="session-001",
            task_id="task-001",
        )
        reply = original.reply(payload={"result": "done"})

        assert reply.sender == "agent-b"  # 发送方反转
        assert reply.receiver == "agent-a"  # 接收方反转
        assert reply.type == MessageType.TASK_RESPONSE
        assert reply.parent_message_id == original.id
        assert reply.workflow_id == original.workflow_id
        assert reply.protocol_version == original.protocol_version
        assert reply.session_id == original.session_id
        assert reply.task_id == original.task_id

    def test_message_priority_default(self):
        msg = Message(
            sender="a", receiver="b", type=MessageType.HEARTBEAT
        )
        assert msg.priority == 5
