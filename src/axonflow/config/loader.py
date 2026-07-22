"""YAML 配置文件加载器"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import TypeVar

import structlog
import yaml
from pydantic import BaseModel

from axonflow.config.models import AgentConfig, AxonFlowConfig, PersonaConfig, WorkflowConfig

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


def _load_yaml(path: Path) -> dict:
    """加载单个 YAML 文件"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    return data


def load_global_config(path: Path | str = "config/axonflow.yaml") -> AxonFlowConfig:
    """加载全局配置"""
    path = Path(path)
    if not path.exists():
        logger.warning(
            "config.global_config_not_found",
            path=str(path.resolve()),
            cwd=str(Path.cwd()),
            hint="Using default config (model=gpt-4o). "
            "Make sure to run from the project root or pass the correct config_dir.",
        )
        return AxonFlowConfig()
    data = _load_yaml(path)
    return AxonFlowConfig(**data)


def _read_text_file(path: Path) -> str | None:
    """读取文本文件内容，文件不存在时返回 None"""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_agent_config(path: Path | str) -> AgentConfig:
    """加载单个智能体配置"""
    data = _load_yaml(Path(path))
    # 支持顶层 key 为 "agent" 的格式
    if "agent" in data:
        data = data["agent"]
    return AgentConfig(**data)


def load_agent_config_from_dir(directory: Path) -> AgentConfig:
    """从目录格式加载智能体配置

    目录结构：
        agent_name/
        ├── config.yaml   # 智能体核心配置（必需）
        ├── soul.md       # 价值观与行为准则（可选）
        ├── user.md       # 用户/协作者档案（可选）
        └── workflow.md   # 工作流程指南（可选）
    """
    config_path = directory / "config.yaml"
    data = _load_yaml(config_path)
    if "agent" in data:
        data = data["agent"]
    config = AgentConfig(**data)

    # 读取 persona markdown 文件并注入
    soul = _read_text_file(directory / "soul.md")
    user = _read_text_file(directory / "user.md")
    workflow = _read_text_file(directory / "workflow.md")

    if soul is not None or user is not None or workflow is not None:
        config.persona = PersonaConfig(
            soul=soul if soul is not None else config.persona.soul,
            user=user if user is not None else config.persona.user,
            workflow=workflow if workflow is not None else config.persona.workflow,
        )

    return config


def load_all_agent_configs(directory: Path | str = "config/agents") -> list[AgentConfig]:
    """加载目录下所有智能体配置

    支持两种格式：
    1. 单文件格式：agents/reviewer.yaml
    2. 目录格式：  agents/reviewer/config.yaml  (+ persona md 文件)

    按名称排序以保证确定性顺序。
    """
    directory = Path(directory)
    if not directory.exists():
        return []

    configs: list[AgentConfig] = []

    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and (entry / "config.yaml").exists():
            # 目录格式：含 config.yaml 的子目录
            configs.append(load_agent_config_from_dir(entry))
        elif entry.is_file() and entry.suffix in (".yaml", ".yml"):
            # 单文件格式（向后兼容）
            configs.append(load_agent_config(entry))

    return configs


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 中的值优先"""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_workflow_config(path: Path | str) -> WorkflowConfig:
    """加载单个工作流配置

    支持 extends 字段：指定基础工作流 ID，当前配置会深度合并到基础配置之上。
    """
    path = Path(path)
    data = _load_yaml(path)
    if "workflow" in data:
        data = data["workflow"]

    # 处理工作流继承
    if "extends" in data:
        base_id = data["extends"]
        # 在同目录下查找基础工作流文件
        parent_dir = path.parent
        base_path: Path | None = None
        for ext in (".yaml", ".yml"):
            candidate = parent_dir / f"{base_id}{ext}"
            if candidate.exists():
                base_path = candidate
                break

        if base_path is not None:
            base_data = _load_yaml(base_path)
            if "workflow" in base_data:
                base_data = base_data["workflow"]
            # 移除 extends 字段，避免递归 / 传给模型
            override = {k: v for k, v in data.items() if k != "extends"}
            data = _deep_merge(base_data, override)

        # 确保最终数据中不含 extends
        data.pop("extends", None)

    return WorkflowConfig(**data)


def load_all_workflow_configs(
    directory: Path | str = "config/workflows",
) -> list[WorkflowConfig]:
    """加载目录下所有工作流配置"""
    directory = Path(directory)
    if not directory.exists():
        return []
    configs = []
    for yaml_file in sorted(directory.glob("*.yaml")):
        configs.append(load_workflow_config(yaml_file))
    for yml_file in sorted(directory.glob("*.yml")):
        configs.append(load_workflow_config(yml_file))
    return configs


# ============================================================
# Skill 加载
# ============================================================


def load_skill_content(skills_dir: Path, skill_names: list[str]) -> str:
    """加载指定 skill 的内容，拼接返回

    支持两种格式:
    - 目录格式: skills_dir/{name}/SKILL.md (+ scripts/ 子目录)
    - 单文件格式: skills_dir/{name}.md

    目录格式优先于单文件格式。
    """
    skills_dir = Path(skills_dir)
    if not skills_dir.exists():
        logger.info("skill.skills_dir_not_found", path=str(skills_dir))
        return ""

    sections: list[str] = []
    for name in skill_names:
        # 优先查找目录格式
        skill_dir = skills_dir / name
        if skill_dir.is_dir():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                content = skill_md.read_text(encoding="utf-8")
                content = _resolve_script_refs(content, skill_dir / "scripts")
                sections.append(
                    f"Skill package: {name}\n"
                    f"Package root: {skill_dir.resolve()}\n"
                    f"Entrypoint: {skill_md.resolve()}\n\n{content}"
                )
            else:
                logger.warning("skill.missing_skill_md", skill=name)
            continue

        # 回退到单文件格式
        skill_file = skills_dir / f"{name}.md"
        if skill_file.exists():
            sections.append(skill_file.read_text(encoding="utf-8"))
        else:
            logger.warning("skill.not_found", skill=name)

    return "\n\n---\n\n".join(sections)


def _resolve_script_refs(content: str, scripts_dir: Path) -> str:
    """将 @script:xxx 标记替换为绝对路径的 shell_exec 指引"""

    def _replacer(m: re.Match) -> str:
        script_name = m.group(1)
        relative = Path(script_name)
        scripts_root = scripts_dir.resolve()
        candidate = scripts_dir / relative
        script_path = candidate.resolve()
        if (
            not relative.is_absolute()
            and ".." not in relative.parts
            and script_path.is_relative_to(scripts_root)
            and script_path.is_file()
            and not candidate.is_symlink()
        ):
            interpreters = {
                ".js": "node",
                ".mjs": "node",
                ".py": "python",
                ".sh": "bash",
                ".zsh": "zsh",
            }
            resolved_path = shlex.quote(str(script_path.resolve()))
            interpreter = interpreters.get(script_path.suffix.lower())
            command = f"{interpreter} {resolved_path}" if interpreter else resolved_path
            return f"使用 shell_exec 工具执行 {command}"
        logger.warning("skill.script_not_found", script=script_name)
        return m.group(0)  # 保留原始文本

    return re.sub(r"@script:(\S+)", _replacer, content)
