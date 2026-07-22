"""ExecutionLogger 单元测试"""

from __future__ import annotations

import json

from axonflow.observability.execution_log import ExecutionLogEntry, ExecutionLogger


def _make_entry(**overrides) -> ExecutionLogEntry:
    defaults = {
        "timestamp": "2026-04-02T12:00:00Z",
        "workflow_id": "wf-001",
        "agent_id": "agent-coder",
        "action": "tool_call",
        "tool_name": "shell_exec",
        "arguments": {"command": "echo hello"},
        "result": "hello\n",
        "error": None,
        "round": 1,
    }
    defaults.update(overrides)
    return ExecutionLogEntry(**defaults)


class TestExecutionLogEntry:
    def test_fields(self):
        entry = _make_entry()
        assert entry.workflow_id == "wf-001"
        assert entry.action == "tool_call"
        assert entry.round == 1

    def test_error_entry(self):
        entry = _make_entry(action="tool_error", result=None, error="timeout")
        assert entry.action == "tool_error"
        assert entry.error == "timeout"

    def test_recovery_fields_default_to_none(self):
        entry = _make_entry()
        assert entry.message_id is None
        assert entry.task_preview is None
        assert entry.rounds_used is None
        assert entry.last_tool_name is None
        assert entry.last_tool_arguments is None


class TestExecutionLogger:
    def test_log_and_get_entries(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        entry = _make_entry()
        logger.log(entry)
        assert len(logger.get_entries()) == 1
        assert logger.get_entries()[0] is entry

    def test_filter_by_workflow_id(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.log(_make_entry(workflow_id="wf-001"))
        logger.log(_make_entry(workflow_id="wf-002"))
        logger.log(_make_entry(workflow_id="wf-001"))

        results = logger.get_entries(workflow_id="wf-001")
        assert len(results) == 2

    def test_filter_by_agent_id(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.log(_make_entry(agent_id="agent-a"))
        logger.log(_make_entry(agent_id="agent-b"))

        results = logger.get_entries(agent_id="agent-a")
        assert len(results) == 1

    def test_filter_by_action(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.log(_make_entry(action="tool_call"))
        logger.log(_make_entry(action="tool_error"))
        logger.log(_make_entry(action="llm_error"))

        results = logger.get_entries(action="tool_error")
        assert len(results) == 1

    def test_combined_filters(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.log(_make_entry(workflow_id="wf-001", agent_id="a", action="tool_call"))
        logger.log(_make_entry(workflow_id="wf-001", agent_id="b", action="tool_call"))
        logger.log(_make_entry(workflow_id="wf-002", agent_id="a", action="tool_error"))

        results = logger.get_entries(workflow_id="wf-001", agent_id="a")
        assert len(results) == 1

    def test_disk_write(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        entry = _make_entry(workflow_id="wf-disk-test")
        logger.log(entry)

        log_file = tmp_path / "logs" / "execution-wf-disk-test.jsonl"
        assert log_file.exists()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["workflow_id"] == "wf-disk-test"
        assert data["action"] == "tool_call"
        assert data["tool_name"] == "shell_exec"

    def test_recovery_fields_round_trip(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        expected = {
            "message_id": "message-123",
            "task_preview": "Recover the interrupted task",
            "rounds_used": 10,
            "last_tool_name": "shell_exec",
            "last_tool_arguments": '{"command":"pytest"}',
        }
        logger.log(
            _make_entry(
                workflow_id="wf-recovery",
                action="tool_error",
                tool_name=None,
                arguments=None,
                result=None,
                error="Max tool call rounds exceeded",
                round=10,
                **expected,
            )
        )

        log_file = tmp_path / "logs" / "execution-wf-recovery.jsonl"
        persisted = json.loads(log_file.read_text())
        for field, value in expected.items():
            assert persisted[field] == value

    def test_disk_write_appends(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.log(_make_entry(workflow_id="wf-append", round=1))
        logger.log(_make_entry(workflow_id="wf-append", round=2))

        log_file = tmp_path / "logs" / "execution-wf-append.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_result_truncation(self, tmp_path):
        long_result = "x" * 5000
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.log(_make_entry(workflow_id="wf-trunc", result=long_result))

        log_file = tmp_path / "logs" / "execution-wf-trunc.jsonl"
        data = json.loads(log_file.read_text().strip())
        assert len(data["result"]) <= 2000

    def test_restart_hydrates_jsonl_and_enriches_all_execution_ids(self, tmp_path):
        first = ExecutionLogger(workspace_dir=str(tmp_path))
        first.log(_make_entry(workflow_id="execution-123"))

        restarted = ExecutionLogger(
            workspace_dir=str(tmp_path),
            run_contexts={"execution-123": ("run-123", "workflow-123")},
        )

        entries = restarted.get_entries(
            workflow_id="workflow-123",
            run_id="run-123",
            execution_id="execution-123",
        )
        assert len(entries) == 1
        assert entries[0].workflow_id == "workflow-123"
        assert entries[0].run_id == "run-123"
        assert entries[0].execution_id == "execution-123"

    def test_new_entries_persist_all_execution_ids(self, tmp_path):
        logger = ExecutionLogger(workspace_dir=str(tmp_path))
        logger.set_run_context("execution-456", "run-456", "workflow-456")

        logger.log(_make_entry(workflow_id="execution-456"))

        persisted = json.loads(
            (tmp_path / "logs" / "execution-execution-456.jsonl").read_text()
        )
        assert persisted["workflow_id"] == "workflow-456"
        assert persisted["run_id"] == "run-456"
        assert persisted["execution_id"] == "execution-456"
