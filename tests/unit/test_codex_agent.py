"""Codex CLI Agent adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axonflow.agents.codex import CodexAgent
from axonflow.api.routes import agents as agent_routes
from axonflow.config.loader import load_agent_config
from axonflow.config.models import AgentConfig, ModelConfig
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMGateway
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry


def _agent(
    tmp_path: Path,
    *,
    skills: list[str] | None = None,
    skills_dir: Path | None = None,
    **codex_overrides,
) -> CodexAgent:
    codex = {
        "command": "codex",
        "working_directory": str(tmp_path),
        "allowed_working_directories": [str(tmp_path)],
        "sandbox": "workspace-write",
        "timeout_seconds": 30,
        "health_check": "exec",
        **codex_overrides,
    }
    return CodexAgent(
        config=AgentConfig(
            id="agent-codex",
            name="Codex",
            role="Implement Python changes and run tests.",
            agent_type="codex",
            model=ModelConfig(provider="codex", name="configured-default"),
            skills=skills or [],
            parameters={"codex": codex},
        ),
        message_bus=InMemoryMessageBus(),
        llm_gateway=LLMGateway(),
        tool_registry=ToolRegistry(),
        skills_dir=skills_dir,
    )


def _message(**payload) -> Message:
    return Message(
        sender="requirements",
        receiver="agent-codex",
        type=MessageType.TASK_REQUEST,
        workflow_id="flow-1",
        step_id="step-coding",
        session_id="session-1",
        task_id="task-1",
        payload={
            "task": "Add a health endpoint",
            "_protocol": {"version": "aip-lite/0.1"},
            **payload,
        },
    )


async def test_handle_message_passes_envelope_via_stdin_and_returns_structured_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = _agent(tmp_path)
    calls: list[tuple[list[str], str, float]] = []

    monkeypatch.setattr(agent, "_resolve_command", lambda settings: "/usr/local/bin/codex")

    async def fake_run(arguments: list[str], prompt: str, timeout: float):
        calls.append((arguments, prompt, timeout))
        output_path = Path(arguments[arguments.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "status": "success",
                    "summary": "Implemented the endpoint.",
                    "files_changed": ["src/app.py", "tests/test_app.py"],
                    "tests": [
                        {"command": "pytest -q", "status": "passed", "output": "2 passed"}
                    ],
                    "notes": [],
                }
            ),
            encoding="utf-8",
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
            ]
        )
        return 0, stdout, ""

    monkeypatch.setattr(agent, "_run_process", fake_run)

    result = await agent.handle_message(_message())

    assert result["status"] == "success"
    assert result["content"] == "Implemented the endpoint."
    assert result["files_changed"] == ["src/app.py", "tests/test_app.py"]
    assert result["codex"]["thread_id"] == "thread-123"
    assert result["artifacts"][0]["uri"] == "src/app.py"
    arguments, prompt, timeout = calls[0]
    assert arguments[-1] == "-"
    assert "--output-schema" in arguments
    assert "--dangerously-bypass-approvals-and-sandbox" not in arguments
    assert "Add a health endpoint" in prompt
    assert '"workflow_id": "flow-1"' in prompt
    assert "Implement Python changes and run tests." in prompt
    assert timeout == 30


async def test_health_probe_runs_a_real_read_only_codex_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = _agent(tmp_path)
    calls: list[tuple[list[str], str, float]] = []
    monkeypatch.setattr(agent, "_resolve_command", lambda settings: "/usr/local/bin/codex")

    async def fake_run(arguments: list[str], prompt: str, timeout: float):
        calls.append((arguments, prompt, timeout))
        return (
            0,
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
            "",
        )

    monkeypatch.setattr(agent, "_run_process", fake_run)

    await agent._health_probe()

    arguments, prompt, timeout = calls[0]
    assert arguments[arguments.index("--sandbox") + 1] == "read-only"
    assert "Do not inspect or modify files" in prompt
    assert timeout == 60


async def test_message_selected_working_directory_is_denied_by_default(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    result = await agent.handle_message(_message(working_directory=str(tmp_path / "another")))

    assert result["status"] == "error"
    assert "does not allow a message-selected working directory" in result["error"]


def test_dynamic_working_directory_must_stay_below_an_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    agent = _agent(
        allowed,
        allow_dynamic_working_directory=True,
        allowed_working_directories=[str(allowed)],
    )

    with pytest.raises(RuntimeError, match="outside the configured allowed roots"):
        agent._resolve_working_directory(
            agent._settings(),
            _message(working_directory=str(outside)),
        )


def test_parse_codex_jsonl_events() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            "not-json",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 7}}),
        ]
    )

    parsed = CodexAgent._parse_events(stdout)

    assert parsed["thread_id"] == "thread-1"
    assert parsed["last_message"] == "final"
    assert parsed["usage"] == {"output_tokens": 7}


def test_codex_prompt_includes_assigned_skill_package(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "review-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Review Workflow\nFollow the review checklist.")
    agent = _agent(
        tmp_path,
        skills=["review-workflow"],
        skills_dir=skills_dir,
    )

    prompt = agent._build_prompt(_message(), tmp_path.resolve(), agent._settings())

    assert "Assigned Skill packages" in prompt
    assert "Review Workflow" in prompt
    assert f"Package root: {skill_dir.resolve()}" in prompt


async def test_create_codex_agent_persists_local_runner_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    (config_dir / "agents").mkdir(parents=True)
    repository = tmp_path / "repository"
    repository.mkdir()

    class StubEngine:
        def __init__(self) -> None:
            self.added: list[AgentConfig] = []

        async def add_agent(self, config: AgentConfig) -> None:
            self.added.append(config)

    engine = StubEngine()
    monkeypatch.setattr(agent_routes, "get_config_dir", lambda: config_dir)
    monkeypatch.setattr(agent_routes, "get_engine", lambda: engine)

    response = await agent_routes.create_agent(
        agent_routes.AgentCreateRequest(
            id="codex-worker",
            name="Codex Worker",
            role="Implement upstream coding requests.",
            agent_type="codex",
            codex_working_directory=str(repository),
            codex_sandbox="workspace-write",
            codex_health_check="exec",
        )
    )

    stored = load_agent_config(config_dir / "agents" / "codex-worker.yaml")
    assert response["agent_type"] == "codex"
    assert stored.agent_type == "codex"
    assert stored.parameters["codex"]["working_directory"] == str(repository.resolve())
    assert stored.parameters["codex"]["allowed_working_directories"] == [
        str(repository.resolve())
    ]
    assert stored.model.provider == "codex"
    assert engine.added[0].id == "codex-worker"
