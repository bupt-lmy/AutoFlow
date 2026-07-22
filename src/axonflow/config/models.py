"""Pydantic 配置模型定义"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# LLM 模型配置
# ============================================================


class ModelConfig(BaseModel):
    """LLM 模型配置"""

    provider: str = "openai"
    name: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: float = 60.0
    api_base: str | None = None
    api_key_env: str | None = None  # 环境变量名，如 OPENAI_API_KEY
    credential_id: str | None = None  # 平台加密凭据 ID，优先于 api_key_env
    fallback_models: list[str] = Field(default_factory=list)


# ============================================================
# 记忆配置
# ============================================================


class MemoryConfig(BaseModel):
    """智能体记忆配置"""

    enabled: bool = True
    backend: str = "in_memory"  # in_memory / redis（后续扩展）
    max_records: int = 1000  # 最大记忆条数
    default_ttl: int | None = None  # 默认过期时间（秒），None 不过期
    scopes: list[str] = Field(
        default_factory=lambda: ["agent", "workflow"]
    )  # 该 Agent 可访问的记忆作用域


# ============================================================
# Agent Persona 配置
# ============================================================


class PersonaConfig(BaseModel):
    """Agent 人设配置 — 类似 OpenClaw 的结构化人设文件

    每个 Agent 可通过目录结构定义人设：
    - soul.md: 价值观与行为准则
    - user.md: 用户/协作者档案
    - workflow.md: 工作流程指南

    内容由 loader 从 md 文件中读取并注入。
    """

    soul: str | None = None  # 价值观与行为准则
    user: str | None = None  # 用户/协作者档案
    workflow: str | None = None  # 工作流程指南


# ============================================================
# 智能体配置
# ============================================================


class AgentConfig(BaseModel):
    """智能体配置

    支持通过 agent_type 指定 Agent 实现类，通过 class_path 加载自定义类，
    通过 parameters 传递自定义参数，通过 memory 配置记忆系统，
    通过 persona 定义结构化人设。
    """

    id: str
    name: str
    role: str = ""  # System Prompt / 角色描述（可被 persona 文件替代）
    agent_type: str = "base"  # Agent 类型标识: base / planner / reviewer / custom
    class_path: str | None = None  # 自定义 Agent 类路径，如 "mymodule.MyAgent"
    model: ModelConfig = Field(default_factory=ModelConfig)
    tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)  # 用于能力发现和 Agent 分类
    can_request: list[str] = Field(default_factory=list)
    max_concurrent: int = 1
    retry_limit: int = 3
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    parameters: dict[str, Any] = Field(default_factory=dict)  # 自定义扩展参数
    persona: PersonaConfig = Field(default_factory=PersonaConfig)  # 人设配置
    skills: list[str] = Field(default_factory=list)  # 关联的 skill 名称列表


class DiscoveryConfig(BaseModel):
    """Runtime capability discovery and failover policy for a workflow slot."""

    description: str = Field(min_length=1)
    required_skills: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    exclude_agents: list[str] = Field(default_factory=list)
    max_candidates: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.05, ge=0, le=1)
    timeout_seconds: float = Field(default=300, gt=0)
    fallback_on_error: bool = True
    fallback_on_timeout: bool = True

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        description = value.strip()
        if not description:
            raise ValueError("Discovery description cannot be blank")
        return description


class AgentInstanceConfig(BaseModel):
    """A workflow-scoped fixed Agent or runtime-discovered Agent slot."""

    id: str
    node_id: str
    template_id: str | None = None
    name: str
    model_profile_id: str | None = None
    discovery: DiscoveryConfig | None = None
    fallback_discovery: DiscoveryConfig | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> AgentInstanceConfig:
        if self.template_id is None and self.discovery is None:
            raise ValueError("Agent instance requires template_id or discovery")
        if self.template_id is not None and self.discovery is not None:
            raise ValueError("Agent instance cannot define both template_id and discovery")
        if self.template_id is None and self.model_profile_id is not None:
            raise ValueError("Discovered Agent slots cannot override a template model profile")
        return self


# ============================================================
# 工作流配置
# ============================================================


class TriggerConfig(BaseModel):
    """触发器配置"""

    type: str = "manual"  # manual / cron / event
    cron: str | None = None
    event: str | None = None
    timezone: str = "UTC"
    input: str = ""

    @model_validator(mode="after")
    def validate_trigger(self) -> TriggerConfig:
        if self.type == "cron":
            if not self.cron:
                raise ValueError("Cron triggers require a cron expression")
            if not croniter.is_valid(self.cron):
                raise ValueError("Invalid cron expression")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        return self


class RouteCondition(BaseModel):
    """路由条件"""

    field: str
    operator: str = "eq"  # eq / neq / contains / gt / lt
    value: Any = None

    def evaluate(self, actual: Any) -> bool:
        """评估条件是否满足"""
        ops = {
            "eq": lambda a, b: a == b,
            "neq": lambda a, b: a != b,
            "contains": lambda a, b: b in str(a),
            "gt": lambda a, b: a > b,
            "lt": lambda a, b: a < b,
        }
        op_fn = ops.get(self.operator)
        if op_fn is None:
            return False
        try:
            return op_fn(actual, self.value)
        except (TypeError, ValueError):
            return False


class RoutePayloadMapping(BaseModel):
    """Select the business fields sent across a workflow route."""

    include: list[str] = Field(default_factory=list)
    task_field: str | None = None

    @field_validator("include")
    @classmethod
    def normalize_include(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(field.strip() for field in value if field.strip()))

    @field_validator("task_field")
    @classmethod
    def normalize_task_field(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class Route(BaseModel):
    """路由规则"""

    target: str  # 目标 Agent ID
    condition: RouteCondition | None = None
    payload_mapping: RoutePayloadMapping | None = None


class JoinConfig(BaseModel):
    """Fan-in 汇聚配置

    指定某个 Agent 需要等待多个上游 Agent 全部（或任一）完成后才接收任务。
    """

    wait_for: list[str]  # 需要等待的 Agent ID 列表
    strategy: str = "all"  # all = 全部完成 | any = 任一完成


class SupervisorConfig(BaseModel):
    """Supervisor 模式配置"""

    agent_id: str  # 用作 supervisor 的 Agent ID
    responsibility: str = (
        "Review every Agent result, enforce workflow quality gates, and decide whether to "
        "continue, request rework, reassign, or finish."
    )
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "execution planning",
            "result review",
            "routing control",
            "failure recovery",
        ]
    )
    planning_enabled: bool = True  # 是否在开头做全局规划
    intervention_on_failure: bool = True  # agent 失败时是否自动介入纠偏

    @field_validator("responsibility")
    @classmethod
    def normalize_responsibility(cls, value: str) -> str:
        return value.strip()

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class FlowConfig(BaseModel):
    """工作流流程配置"""

    mode: str = "flat"  # 编排模式: flat / supervisor / 自定义 class_path
    entry: str  # 入口 Agent ID
    max_iterations: int = 10
    timeout: int = 3600  # 秒
    routes: dict[str, list[Route]] = Field(default_factory=dict)
    terminate_on: list[dict[str, Any]] = Field(default_factory=list)
    join: dict[str, JoinConfig] = Field(default_factory=dict)  # fan-in 汇聚点
    supervisor: SupervisorConfig | None = None  # supervisor 模式配置

    @model_validator(mode="after")
    def validate_supervisor_mode(self) -> FlowConfig:
        if self.mode == "supervisor" and self.supervisor is None:
            raise ValueError("Supervisor mode requires a supervisor configuration")
        return self


class WorkflowConfig(BaseModel):
    """工作流顶层配置"""

    id: str
    name: str
    extends: str | None = None  # 继承基础工作流模板 ID
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    agents: list[str] = Field(default_factory=list)
    agent_instances: list[AgentInstanceConfig] = Field(default_factory=list)
    flow: FlowConfig
    context: dict[str, Any] = Field(default_factory=dict)


# ============================================================
# 全局配置
# ============================================================


class RedisConfig(BaseModel):
    """Redis 连接配置"""

    url: str = "redis://localhost:6379"
    max_connections: int = 10


class SandboxConfig(BaseModel):
    """沙箱配置"""

    enabled: bool = False
    command_whitelist: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)


class WebhookEndpoint(BaseModel):
    """Webhook 端点"""

    url: str
    events: list[str] = Field(default_factory=lambda: ["workflow.completed", "workflow.failed"])


class ToolPluginConfig(BaseModel):
    """外部工具插件配置"""

    class_path: str  # 工具类的完整路径，如 "mypackage.tools.SlackTool"
    config: dict[str, Any] = Field(default_factory=dict)  # 传递给工具的配置参数


class PluginsConfig(BaseModel):
    """插件配置"""

    tools: list[ToolPluginConfig] = Field(default_factory=list)


class AgentHealthConfig(BaseModel):
    """Agent endpoint/model health probing policy."""

    enabled: bool = True
    interval_seconds: float = Field(default=300, ge=10)
    timeout_seconds: float = Field(default=15, gt=0, le=300)


class AxonFlowConfig(BaseModel):
    """AxonFlow 全局配置"""

    redis: RedisConfig = Field(default_factory=RedisConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    webhooks: list[WebhookEndpoint] = Field(default_factory=list)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    agent_health: AgentHealthConfig = Field(default_factory=AgentHealthConfig)
    log_level: str = "INFO"
    log_format: str = "json"  # json / console
    workspace_dir: str = "./workspace"
    default_model: ModelConfig = Field(default_factory=ModelConfig)
    token_budget: int | None = None  # 全局 Token 预算上限
