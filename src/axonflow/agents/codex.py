"""Codex CLI-backed coding Agent."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from axonflow.config.loader import load_skill_content
from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["success", "partial", "blocked"]},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["passed", "failed", "not_run"],
                    },
                    "output": {"type": "string"},
                },
                "required": ["command", "status", "output"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "files_changed", "tests", "notes"],
    "additionalProperties": False,
}

_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
_HEALTH_MODES = {"exec", "auth", "binary"}


class CodexAgent(BaseAgent):
    """Run an AxonFlow task with the locally authenticated Codex CLI.

    The adapter deliberately invokes the executable without a shell. The configured working
    directory is the only dynamic execution boundary by default; a message may select another
    directory only when ``allow_dynamic_working_directory`` is enabled and the resolved path is
    below an explicitly configured allowed root.
    """

    async def handle_message(self, message: Message) -> dict[str, Any]:
        settings = self._settings()
        try:
            command = self._resolve_command(settings)
            working_directory = self._resolve_working_directory(settings, message)
        except (OSError, RuntimeError, ValueError) as exc:
            return {"status": "error", "error": str(exc)}

        prompt = self._build_prompt(message, working_directory, settings)
        timeout_seconds = self._positive_float(settings, "timeout_seconds", 1800)

        with tempfile.TemporaryDirectory(prefix="axonflow-codex-") as temporary_directory:
            temporary_path = Path(temporary_directory)
            schema_path = temporary_path / "output-schema.json"
            output_path = temporary_path / "last-message.json"
            schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")

            arguments = self._exec_arguments(
                command=command,
                working_directory=working_directory,
                settings=settings,
                sandbox=self._sandbox(settings),
                schema_path=schema_path,
                output_path=output_path,
            )
            try:
                return_code, stdout, stderr = await self._run_process(
                    arguments,
                    prompt,
                    timeout_seconds,
                )
            except RuntimeError as exc:
                return {"status": "error", "error": str(exc)}

            events = self._parse_events(stdout)
            final_text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            outcome = self._parse_outcome(final_text, events)

        if return_code != 0:
            error = self._process_error(stderr, events, return_code)
            self._log_codex_execution(message, working_directory, error=error)
            return {
                "status": "error",
                "error": error,
                "content": outcome.get("summary", ""),
                "codex": self._metadata(events, working_directory, return_code, settings),
            }

        outcome_status = str(outcome.get("status", "success"))
        summary = str(outcome.get("summary") or events.get("last_message") or "Codex completed")
        files_changed = self._string_list(outcome.get("files_changed"))
        tests = outcome.get("tests") if isinstance(outcome.get("tests"), list) else []
        notes = self._string_list(outcome.get("notes"))
        result: dict[str, Any] = {
            "status": "error" if outcome_status == "blocked" else "success",
            "outcome_status": outcome_status,
            "content": summary,
            "files_changed": files_changed,
            "tests": tests,
            "notes": notes,
            "codex": self._metadata(events, working_directory, return_code, settings),
            "artifacts": [
                {
                    "type": "file",
                    "name": path,
                    "uri": path,
                    "metadata": {"working_directory": str(working_directory)},
                }
                for path in files_changed
            ],
        }
        if outcome_status == "blocked":
            result["error"] = summary
        self._log_codex_execution(message, working_directory, result=summary)
        return result

    async def _health_probe(self) -> None:
        settings = self._settings()
        command = self._resolve_command(settings)
        working_directory = self._resolve_working_directory(settings)
        mode = str(settings.get("health_check", "exec"))
        if mode not in _HEALTH_MODES:
            raise RuntimeError(f"Unsupported Codex health_check mode: {mode}")
        timeout = self._positive_float(settings, "health_timeout_seconds", 60)

        if mode == "binary":
            arguments = [command, "--version"]
            prompt = ""
        elif mode == "auth":
            arguments = [command, "login", "status"]
            prompt = ""
        else:
            arguments = self._exec_arguments(
                command=command,
                working_directory=working_directory,
                settings=settings,
                sandbox="read-only",
            )
            prompt = "Reply with OK only. Do not inspect or modify files and do not run commands."

        return_code, stdout, stderr = await self._run_process(arguments, prompt, timeout)
        if return_code != 0:
            raise RuntimeError(self._process_error(stderr, self._parse_events(stdout), return_code))
        if mode == "exec" and not self._parse_events(stdout).get("last_message"):
            raise RuntimeError("Codex health probe returned no Agent message")

    def _settings(self) -> dict[str, Any]:
        settings = self.config.parameters.get("codex", {})
        if not isinstance(settings, dict):
            raise RuntimeError("Codex Agent requires parameters.codex to be an object")
        return settings

    @staticmethod
    def _resolve_command(settings: dict[str, Any]) -> str:
        configured = str(settings.get("command", "codex")).strip()
        if not configured or "\x00" in configured:
            raise RuntimeError("Codex command cannot be blank")
        if os.path.isabs(configured):
            path = Path(configured)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise RuntimeError(f"Codex executable is unavailable: {configured}")
            return str(path)
        if Path(configured).name != configured:
            raise RuntimeError("Relative Codex command paths are not allowed")
        resolved = shutil.which(configured)
        if resolved is None:
            raise RuntimeError(f"Codex executable is not on PATH: {configured}")
        return resolved

    def _resolve_working_directory(
        self,
        settings: dict[str, Any],
        message: Message | None = None,
    ) -> Path:
        configured = settings.get("working_directory", ".")
        if not isinstance(configured, str) or not configured.strip():
            raise RuntimeError("Codex working_directory must be a non-empty path")
        default_directory = Path(configured).expanduser().resolve()
        selected = default_directory

        requested = message.payload.get("working_directory") if message is not None else None
        if isinstance(requested, str) and requested.strip():
            if not settings.get("allow_dynamic_working_directory", False):
                raise RuntimeError(
                    "This Codex Agent does not allow a message-selected working directory"
                )
            selected = Path(requested).expanduser().resolve()

        roots_raw = settings.get("allowed_working_directories", [str(default_directory)])
        if not isinstance(roots_raw, list) or not roots_raw:
            raise RuntimeError("Codex allowed_working_directories must contain at least one path")
        roots = [Path(str(root)).expanduser().resolve() for root in roots_raw]
        if not any(selected == root or selected.is_relative_to(root) for root in roots):
            raise RuntimeError(
                f"Codex working directory is outside the configured allowed roots: {selected}"
            )
        if not selected.is_dir():
            raise RuntimeError(f"Codex working directory does not exist: {selected}")
        return selected

    def _build_prompt(
        self,
        message: Message,
        working_directory: Path,
        settings: dict[str, Any],
    ) -> str:
        envelope = {
            "sender": message.sender,
            "message_type": message.type.value,
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "session_id": message.session_id,
            "task_id": message.task_id,
            "payload": message.payload,
            "context": message.context,
        }
        serialized = json.dumps(envelope, ensure_ascii=False, indent=2, default=str)
        max_chars = int(settings.get("max_prompt_chars", 200_000))
        if max_chars < 1000:
            raise RuntimeError("Codex max_prompt_chars must be at least 1000")
        if len(serialized) > max_chars:
            serialized = serialized[:max_chars] + "\n...[input truncated by configured limit]"
        role = self.config.role.strip() or "Implement and verify coding tasks in the repository."
        skill_content = ""
        if self.config.skills and self._skills_dir:
            skill_content = load_skill_content(self._skills_dir, self.config.skills)
        skill_section = (
            f"\nAssigned Skill packages:\n{skill_content}\n"
            if skill_content
            else ""
        )
        return f"""You are the Codex coding Agent inside an AxonFlow workflow.

Agent responsibility:
{role}
{skill_section}

Execution contract:
- Work only inside: {working_directory}
- Treat the AxonFlow envelope below as the complete upstream task and evidence.
- Inspect the repository, implement the requested change, and run proportionate tests.
- Preserve unrelated user changes. Do not create commits, push, publish, or contact people.
- Do not wait for interactive input. If essential information or permission is missing,
  report blocked.
- Your final response must match the JSON output schema supplied by the runner.
- List repository-relative changed files and every test command actually run.

AxonFlow envelope:
{serialized}
"""

    def _exec_arguments(
        self,
        *,
        command: str,
        working_directory: Path,
        settings: dict[str, Any],
        sandbox: str,
        schema_path: Path | None = None,
        output_path: Path | None = None,
    ) -> list[str]:
        arguments = [
            command,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--cd",
            str(working_directory),
        ]
        if settings.get("ephemeral", True):
            arguments.append("--ephemeral")
        model = settings.get("model")
        if isinstance(model, str) and model.strip():
            arguments.extend(["--model", model.strip()])
        profile = settings.get("profile")
        if isinstance(profile, str) and profile.strip():
            arguments.extend(["--profile", profile.strip()])
        if settings.get("skip_git_repo_check", False):
            arguments.append("--skip-git-repo-check")
        if schema_path is not None:
            arguments.extend(["--output-schema", str(schema_path)])
        if output_path is not None:
            arguments.extend(["--output-last-message", str(output_path)])
        arguments.append("-")
        return arguments

    @staticmethod
    async def _run_process(
        arguments: list[str],
        prompt: str,
        timeout_seconds: float,
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RuntimeError(f"Codex execution timed out after {timeout_seconds:g}s") from exc
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _parse_events(stdout: str) -> dict[str, Any]:
        parsed: dict[str, Any] = {"thread_id": None, "last_message": "", "usage": None}
        errors: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                parsed["thread_id"] = event.get("thread_id")
            elif event_type == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parsed["last_message"] = text
            elif event_type == "turn.completed":
                parsed["usage"] = event.get("usage")
            elif event_type in {"error", "turn.failed"}:
                error = event.get("message") or event.get("error")
                if error:
                    errors.append(str(error))
        parsed["errors"] = errors
        return parsed

    @staticmethod
    def _parse_outcome(final_text: str, events: dict[str, Any]) -> dict[str, Any]:
        candidate = final_text.strip() or str(events.get("last_message") or "").strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return {
                "status": "success",
                "summary": candidate,
                "files_changed": [],
                "tests": [],
                "notes": ["Codex returned an unstructured final response."],
            }
        if not isinstance(parsed, dict):
            return {"status": "success", "summary": str(parsed)}
        return parsed

    @staticmethod
    def _process_error(stderr: str, events: dict[str, Any], return_code: int) -> str:
        details = [*events.get("errors", [])]
        if stderr.strip():
            details.append(stderr.strip()[-4000:])
        suffix = ": " + " | ".join(details) if details else ""
        return f"Codex exited with code {return_code}{suffix}"

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def _positive_float(settings: dict[str, Any], key: str, default: float) -> float:
        value = float(settings.get(key, default))
        if value <= 0:
            raise RuntimeError(f"Codex {key} must be greater than zero")
        return value

    @staticmethod
    def _sandbox(settings: dict[str, Any]) -> str:
        sandbox = str(settings.get("sandbox", "workspace-write"))
        if sandbox not in _SANDBOX_MODES:
            raise RuntimeError(f"Unsupported Codex sandbox mode: {sandbox}")
        return sandbox

    @staticmethod
    def _metadata(
        events: dict[str, Any],
        working_directory: Path,
        return_code: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "thread_id": events.get("thread_id"),
            "working_directory": str(working_directory),
            "return_code": return_code,
            "sandbox": settings.get("sandbox", "workspace-write"),
            "model": settings.get("model"),
            "profile": settings.get("profile"),
            "usage": events.get("usage"),
        }

    def _log_codex_execution(
        self,
        message: Message,
        working_directory: Path,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        self._log_execution(
            workflow_id=message.workflow_id,
            action="codex_exec",
            tool_name="codex",
            arguments={"working_directory": str(working_directory)},
            round_num=1,
            result=result,
            error=error,
        )
