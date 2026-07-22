"""执行日志 — 记录 tool 调用、错误、异常"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_MAX_RESULT_LENGTH = 2000

# Callback signature: (entry: ExecutionLogEntry, run_id: str | None) -> None
LogCallback = Callable[["ExecutionLogEntry", str | None], Any]


@dataclass
class ExecutionLogEntry:
    """单条执行日志"""

    timestamp: str  # ISO 8601
    workflow_id: str
    agent_id: str
    action: str  # tool_call | tool_error | llm_error | skill_error
    tool_name: str | None
    arguments: dict | None
    result: str | None
    error: str | None
    round: int
    run_id: str | None = None
    execution_id: str | None = None
    message_id: str | None = None
    task_preview: str | None = None
    rounds_used: int | None = None
    last_tool_name: str | None = None
    last_tool_arguments: str | None = None


class ExecutionLogger:
    """执行日志记录器 — 双写：内存 + JSONL 磁盘 + 可选回调"""

    def __init__(
        self,
        workspace_dir: str = "./workspace",
        run_contexts: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._entries: list[ExecutionLogEntry] = []
        self._workspace_dir = Path(workspace_dir)
        self._callbacks: list[LogCallback] = []
        # Mapping: internal execution_id -> (product run_id, workflow definition id).
        self._run_contexts = dict(run_contexts or {})
        self._load_from_disk()

    def add_callback(self, callback: LogCallback) -> None:
        """注册日志回调（如 WebSocket 广播）"""
        self._callbacks.append(callback)

    def remove_callback(self, callback: LogCallback) -> None:
        """移除日志回调"""
        self._callbacks = [cb for cb in self._callbacks if cb is not callback]

    def set_run_context(self, execution_id: str, run_id: str, workflow_id: str) -> None:
        """Associate an internal execution with its stable workflow and product run IDs."""
        self._run_contexts[execution_id] = (run_id, workflow_id)

    def set_run_id(self, workflow_id: str, run_id: str) -> None:
        """Backward-compatible alias for callers that only know the execution ID."""
        self.set_run_context(workflow_id, run_id, workflow_id)

    def clear_run_id(self, workflow_id: str) -> None:
        """清除 workflow_id 对应的 run_id"""
        self._run_contexts.pop(workflow_id, None)

    def get_run_id(self, workflow_id: str) -> str | None:
        """Return the product-facing run ID associated with an engine execution ID."""
        context = self._run_contexts.get(workflow_id)
        return context[0] if context else None

    def log(self, entry: ExecutionLogEntry) -> None:
        """记录一条执行日志"""
        self._apply_run_context(entry)
        self._entries.append(entry)
        self._write_to_disk(entry)

        # 触发回调
        run_id = entry.run_id
        for callback in self._callbacks:
            try:
                callback(entry, run_id)
            except Exception as e:
                logger.error("execution_log.callback_failed", error=str(e))

    def get_entries(
        self,
        workflow_id: str | None = None,
        run_id: str | None = None,
        execution_id: str | None = None,
        agent_id: str | None = None,
        action: str | None = None,
    ) -> list[ExecutionLogEntry]:
        """按条件过滤查询"""
        results = self._entries
        if workflow_id is not None:
            results = [e for e in results if e.workflow_id == workflow_id]
        if run_id is not None:
            results = [e for e in results if e.run_id == run_id]
        if execution_id is not None:
            results = [e for e in results if e.execution_id == execution_id]
        if agent_id is not None:
            results = [e for e in results if e.agent_id == agent_id]
        if action is not None:
            results = [e for e in results if e.action == action]
        return results

    def _write_to_disk(self, entry: ExecutionLogEntry) -> None:
        """追加写入 JSONL 文件"""
        try:
            log_dir = self._workspace_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"execution-{entry.execution_id or entry.workflow_id}.jsonl"

            data = asdict(entry)
            # 截断过长的 result
            if data.get("result") and len(data["result"]) > _MAX_RESULT_LENGTH:
                data["result"] = data["result"][:_MAX_RESULT_LENGTH]

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("execution_log.write_failed", error=str(e))

    def _apply_run_context(self, entry: ExecutionLogEntry) -> None:
        """Normalize legacy entries and enrich all three execution identifiers."""
        execution_id = entry.execution_id or entry.workflow_id
        entry.execution_id = execution_id
        context = self._run_contexts.get(execution_id)
        if context:
            entry.run_id, entry.workflow_id = context

    def _load_from_disk(self) -> None:
        """Hydrate JSONL history so the Logs API survives process restarts."""
        log_dir = self._workspace_dir / "logs"
        if not log_dir.exists():
            return
        allowed_fields = {field.name for field in fields(ExecutionLogEntry)}
        loaded = 0
        for log_file in sorted(log_dir.glob("execution-*.jsonl")):
            try:
                lines = log_file.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.warning("execution_log.read_failed", path=str(log_file), error=str(exc))
                continue
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    entry = ExecutionLogEntry(
                        **{key: value for key, value in raw.items() if key in allowed_fields}
                    )
                    self._apply_run_context(entry)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "execution_log.entry_skipped",
                        path=str(log_file),
                        line=line_number,
                        error=str(exc),
                    )
                    continue
                self._entries.append(entry)
                loaded += 1
        logger.info("execution_log.hydrated", entries=loaded, directory=str(log_dir))
