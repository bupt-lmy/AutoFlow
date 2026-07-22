"""Skill 系统测试"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from axonflow.config.loader import _resolve_script_refs, load_skill_content
from axonflow.config.models import AgentConfig, ModelConfig
from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message, MessageType
from axonflow.llm.gateway import LLMResponse
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.tools.base import ToolRegistry


class TestAgentConfigSkills:
    def test_skills_default_empty(self):
        config = AgentConfig(id="a", name="A")
        assert config.skills == []

    def test_skills_from_yaml_data(self):
        config = AgentConfig(
            id="a",
            name="A",
            skills=["code-review", "tdd"],
        )
        assert config.skills == ["code-review", "tdd"]


class TestResolveScriptRefs:
    def test_replaces_existing_script(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "lint.sh").write_text("#!/bin/bash\necho lint")

        content = "Run: @script:lint.sh {file}"
        result = _resolve_script_refs(content, scripts_dir)
        assert "@script:" not in result
        assert "shell_exec" in result
        assert "bash " in result
        assert str((scripts_dir / "lint.sh").resolve()) in result

    def test_uses_node_for_module_script(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "check.mjs").write_text("console.log('ok')")

        result = _resolve_script_refs("Run @script:check.mjs", scripts_dir)

        assert "shell_exec" in result
        assert "node " in result

    def test_preserves_missing_script(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()

        content = "Run: @script:missing.sh"
        result = _resolve_script_refs(content, scripts_dir)
        assert "@script:missing.sh" in result

    def test_multiple_refs(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "a.sh").write_text("#!/bin/bash")
        (scripts_dir / "b.sh").write_text("#!/bin/bash")

        content = "First @script:a.sh then @script:b.sh then @script:c.sh"
        result = _resolve_script_refs(content, scripts_dir)
        assert "@script:a.sh" not in result
        assert "@script:b.sh" not in result
        assert "@script:c.sh" in result

    def test_rejects_script_reference_outside_package(self, tmp_path):
        scripts_dir = tmp_path / "skill" / "scripts"
        scripts_dir.mkdir(parents=True)
        (tmp_path / "outside.sh").write_text("echo unsafe")

        result = _resolve_script_refs("Run @script:../../outside.sh", scripts_dir)

        assert result == "Run @script:../../outside.sh"


class TestLoadSkillContent:
    def test_load_directory_format(self, tmp_path):
        skill_dir = tmp_path / "code-review"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Code Review\nDo careful review.")

        result = load_skill_content(tmp_path, ["code-review"])
        assert "Code Review" in result
        assert "careful review" in result
        assert f"Package root: {skill_dir.resolve()}" in result

    def test_load_single_file_format(self, tmp_path):
        (tmp_path / "gap-analysis.md").write_text("# Gap Analysis\nFind the gaps.")

        result = load_skill_content(tmp_path, ["gap-analysis"])
        assert "Gap Analysis" in result

    def test_directory_preferred_over_single_file(self, tmp_path):
        skill_dir = tmp_path / "review"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("Directory version")
        (tmp_path / "review.md").write_text("File version")

        result = load_skill_content(tmp_path, ["review"])
        assert "Directory version" in result
        assert "File version" not in result

    def test_load_with_script_refs(self, tmp_path):
        skill_dir = tmp_path / "lint-skill"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho lint")
        (skill_dir / "SKILL.md").write_text("Execute @script:run.sh to lint.")

        result = load_skill_content(tmp_path, ["lint-skill"])
        assert "@script:" not in result
        assert "shell_exec" in result

    def test_missing_skill_returns_empty(self, tmp_path):
        result = load_skill_content(tmp_path, ["nonexistent"])
        assert result == ""

    def test_missing_skill_md_in_directory(self, tmp_path):
        (tmp_path / "empty-skill").mkdir()
        result = load_skill_content(tmp_path, ["empty-skill"])
        assert result == ""

    def test_multiple_skills_joined(self, tmp_path):
        (tmp_path / "skill-a.md").write_text("Skill A content")
        (tmp_path / "skill-b.md").write_text("Skill B content")

        result = load_skill_content(tmp_path, ["skill-a", "skill-b"])
        assert "Skill A content" in result
        assert "Skill B content" in result
        assert "---" in result

    def test_skills_dir_not_exists(self, tmp_path):
        nonexistent = tmp_path / "no-such-dir"
        result = load_skill_content(nonexistent, ["anything"])
        assert result == ""

class TestAgentSkillIntegration:
    @pytest.mark.asyncio
    async def test_agent_loads_skills_into_prompt(self, tmp_path):
        """Agent 配置了 skills 时，handle_message 应加载 skill 内容到 prompt"""
        skill_dir = tmp_path / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Test Skill\nDo testing stuff.")

        config = AgentConfig(
            id="skill-agent",
            name="Skill Agent",
            role="You are a test agent.",
            model=ModelConfig(provider="openai", name="test-model"),
            skills=["test-skill"],
        )

        bus = InMemoryMessageBus()
        registry = ToolRegistry()
        gateway = MagicMock()
        gateway.chat = AsyncMock(
            return_value=LLMResponse(content="Done.", model="test-model"),
        )

        agent = BaseAgent(
            config=config,
            message_bus=bus,
            llm_gateway=gateway,
            tool_registry=registry,
            skills_dir=tmp_path / "skills",
        )

        msg = Message(
            sender="user",
            receiver="skill-agent",
            type=MessageType.TASK_REQUEST,
            payload={"task": "test"},
            workflow_id="wf-skill",
        )

        await agent.handle_message(msg)

        call_args = gateway.chat.call_args
        messages = call_args[1]["messages"]
        system_content = messages[0]["content"]
        assert "Test Skill" in system_content
        assert "testing stuff" in system_content
