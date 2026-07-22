"""AxonFlow 引擎主入口 — 组装所有模块并启动系统"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from axonflow.agents.discovered import DiscoveredAgent
from axonflow.config.loader import (
    load_all_agent_configs,
    load_all_workflow_configs,
    load_global_config,
)
from axonflow.config.models import AgentConfig, AxonFlowConfig, ModelConfig, Route, WorkflowConfig
from axonflow.core.agent import AgentHealthState, AgentRegistry, create_agent
from axonflow.core.orchestrator_factory import create_orchestrator
from axonflow.core.scheduler import Scheduler
from axonflow.core.workflow import WorkflowResult
from axonflow.llm.gateway import LLMGateway
from axonflow.memory.local import InMemoryStore
from axonflow.messaging.base import MessageBus
from axonflow.messaging.memory_bus import InMemoryMessageBus
from axonflow.observability.execution_log import ExecutionLogger
from axonflow.observability.logger import setup_logging
from axonflow.platform.models import PlatformWorkflow
from axonflow.platform.store import PlatformStore
from axonflow.tools.archive_ops import ArchiveOpsTool
from axonflow.tools.base import Tool, ToolRegistry
from axonflow.tools.directory_tree import DirectoryTreeTool
from axonflow.tools.env_vars import EnvVarsTool
from axonflow.tools.file_ops import FileReadTool, FileWriteTool
from axonflow.tools.file_patch import FilePatchTool
from axonflow.tools.generated_video import GeneratedVideoFinalizeTool
from axonflow.tools.git_ops import GitOpsTool
from axonflow.tools.http_request import HttpRequestTool
from axonflow.tools.json_query import JsonQueryTool
from axonflow.tools.media_compose import MediaComposeTool
from axonflow.tools.media_probe import MediaProbeTool
from axonflow.tools.media_quality import MediaQualityCheckTool
from axonflow.tools.media_register import MediaRegisterTool
from axonflow.tools.media_render import MediaRenderTool
from axonflow.tools.minimax_media import (
    MiniMaxImageGenerateTool,
    MiniMaxMusicGenerateTool,
    MiniMaxSpeechGenerateTool,
    MiniMaxVideoGenerateTool,
)
from axonflow.tools.process_manager import ProcessManagerTool
from axonflow.tools.python_eval import PythonEvalTool
from axonflow.tools.shell_exec import ShellExecTool
from axonflow.tools.storyboard_video import StoryboardMotionRenderTool
from axonflow.tools.subtitle_create import SubtitleCreateTool
from axonflow.tools.text_search import TextSearchTool
from axonflow.tools.video_edit import (
    HardSubtitleBurnTool,
    HighlightRenderTool,
    VideoIngestTool,
    VideoSceneDetectTool,
    VideoTranscribeTool,
)
from axonflow.tools.video_features import VideoSceneFeatureTool
from axonflow.tools.web_scrape import WebScrapeTool
from axonflow.tools.web_search import WebSearchTool

logger = structlog.get_logger()


class AxonFlowEngine:
    """AxonFlow 引擎

    职责:
    1. 加载配置
    2. 初始化各模块（消息总线、LLM 网关、工具注册中心、Agent 注册中心）
    3. 启动所有 Agent 的消息监听
    4. 提供工作流执行入口
    5. 管理生命周期
    """

    def __init__(
        self,
        config_dir: str = "config",
        config: AxonFlowConfig | None = None,
        platform_store: PlatformStore | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._config = config
        self._platform_store = platform_store
        self._message_bus: MessageBus | None = None
        self._llm_gateway: LLMGateway | None = None
        self._tool_registry: ToolRegistry | None = None
        self._agent_registry: AgentRegistry | None = None
        self._scheduler: Scheduler | None = None
        self._memory_store: InMemoryStore | None = None
        self._execution_logger: ExecutionLogger | None = None
        self._agent_tasks: list[asyncio.Task] = []
        self._running = False
        self._initialized = False

    @property
    def config(self) -> AxonFlowConfig:
        if self._config is None:
            raise RuntimeError("Engine not initialized. Call initialize() first.")
        return self._config

    @property
    def agent_registry(self) -> AgentRegistry:
        if self._agent_registry is None:
            raise RuntimeError("Engine not initialized.")
        return self._agent_registry

    @property
    def message_bus(self) -> MessageBus:
        if self._message_bus is None:
            raise RuntimeError("Engine not initialized.")
        return self._message_bus

    async def initialize(self) -> None:
        """初始化引擎 — 加载配置并组装模块"""
        if self._initialized:
            return

        # 1. 加载全局配置
        if self._config is None:
            global_config_path = self._config_dir / "axonflow.yaml"
            self._config = load_global_config(global_config_path)

        # 2. 配置日志
        setup_logging(
            level=self.config.log_level,
            fmt=self.config.log_format,
        )

        logger.info("engine.initializing")

        # 3. 初始化消息总线
        self._message_bus = await self._create_message_bus()

        # 4. 初始化 LLM 网关
        self._llm_gateway = LLMGateway(
            default_model=self.config.default_model,
            token_budget=self.config.token_budget,
            credential_resolver=(
                self._platform_store.resolve_credential if self._platform_store else None
            ),
            span_store=self._platform_store,
        )

        # 5. 注册内置工具
        self._tool_registry = ToolRegistry()
        self._register_builtin_tools()

        # 5.1 加载外部工具插件
        self._load_plugin_tools()

        # 5.5 初始化共享记忆存储
        self._memory_store = InMemoryStore()

        # 5.6 初始化执行日志
        self._execution_logger = ExecutionLogger(
            workspace_dir=self.config.workspace_dir,
            run_contexts=(
                self._platform_store.list_execution_contexts() if self._platform_store else None
            ),
        )

        # 6. 加载并注册 Agent
        self._agent_registry = AgentRegistry()
        await self._load_agents()

        # 7. 初始化调度器
        self._scheduler = Scheduler()
        self._scheduler.set_run_callback(self._scheduled_run)
        self._load_cron_jobs()

        self._initialized = True
        logger.info("engine.initialized")

    async def start_workflow_trace(self, run_id: str, workflow_id: str, input_data: str) -> None:
        if self._llm_gateway is not None:
            await self._llm_gateway.start_workflow_trace(run_id, workflow_id, input_data)

    async def finish_workflow_trace(
        self,
        run_id: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if self._llm_gateway is not None:
            await self._llm_gateway.finish_workflow_trace(run_id, result=result, error=error)

    async def _create_message_bus(self) -> MessageBus:
        """创建消息总线（尝试 Redis，失败则降级到内存）"""
        try:
            import redis.asyncio as aioredis

            from axonflow.messaging.redis_bus import RedisMessageBus

            bus = RedisMessageBus(self.config.redis.url)
            await bus.start()
            # 实际检测 Redis 连通性
            r = aioredis.from_url(self.config.redis.url)
            await r.ping()
            await r.aclose()
            logger.info("engine.message_bus", type="redis")
            return bus
        except Exception as e:
            logger.warning(
                "engine.redis_unavailable",
                error=str(e),
                fallback="in_memory",
            )
            bus = InMemoryMessageBus()
            logger.info("engine.message_bus", type="in_memory")
            return bus

    def _register_builtin_tools(self) -> None:
        """注册内置工具"""
        assert self._tool_registry is not None
        # 原有工具
        self._tool_registry.register(ShellExecTool())
        self._tool_registry.register(FileReadTool())
        self._tool_registry.register(FileWriteTool())
        self._tool_registry.register(GitOpsTool())
        self._tool_registry.register(HttpRequestTool())
        # 新增工具
        self._tool_registry.register(WebSearchTool())
        self._tool_registry.register(WebScrapeTool())
        self._tool_registry.register(TextSearchTool())
        self._tool_registry.register(PythonEvalTool())
        self._tool_registry.register(JsonQueryTool())
        self._tool_registry.register(DirectoryTreeTool())
        self._tool_registry.register(FilePatchTool())
        self._tool_registry.register(EnvVarsTool())
        self._tool_registry.register(ArchiveOpsTool())
        self._tool_registry.register(ProcessManagerTool())
        self._tool_registry.register(MediaProbeTool())
        self._tool_registry.register(MediaRenderTool())
        workspace_dir = Path(self.config.workspace_dir)
        if not workspace_dir.is_absolute():
            workspace_dir = self._config_dir.parent / workspace_dir
        self._tool_registry.register(
            MediaComposeTool(output_dir=workspace_dir / "media" / "composed")
        )
        self._tool_registry.register(MediaQualityCheckTool())
        self._tool_registry.register(MediaRegisterTool(self._platform_store))
        self._tool_registry.register(
            SubtitleCreateTool(output_dir=workspace_dir / "media" / "subtitles")
        )
        self._tool_registry.register(
            VideoIngestTool(output_dir=workspace_dir / "media" / "imports")
        )
        self._tool_registry.register(
            VideoSceneDetectTool(output_dir=workspace_dir / "media" / "keyframes")
        )
        self._tool_registry.register(
            VideoSceneFeatureTool(output_dir=workspace_dir / "media" / "scene-features")
        )
        self._tool_registry.register(
            HighlightRenderTool(output_dir=workspace_dir / "media" / "highlights")
        )
        self._tool_registry.register(
            VideoTranscribeTool(
                model_path=workspace_dir / "models" / "ggml-small.bin",
                output_dir=workspace_dir / "media" / "transcripts",
            )
        )
        self._tool_registry.register(
            HardSubtitleBurnTool(output_dir=workspace_dir / "media" / "final")
        )
        self._tool_registry.register(
            GeneratedVideoFinalizeTool(output_dir=workspace_dir / "media" / "generated-final")
        )
        self._tool_registry.register(
            StoryboardMotionRenderTool(output_dir=workspace_dir / "media" / "storyboards")
        )
        self._tool_registry.register(
            MiniMaxImageGenerateTool(
                output_dir=workspace_dir / "media" / "generated",
                credential_resolver=(
                    self._platform_store.resolve_credential if self._platform_store else None
                ),
            )
        )
        self._tool_registry.register(
            MiniMaxSpeechGenerateTool(
                output_dir=workspace_dir / "media" / "generated",
                credential_resolver=(
                    self._platform_store.resolve_credential if self._platform_store else None
                ),
            )
        )
        self._tool_registry.register(
            MiniMaxMusicGenerateTool(
                output_dir=workspace_dir / "media" / "generated",
                credential_resolver=(
                    self._platform_store.resolve_credential if self._platform_store else None
                ),
            )
        )
        self._tool_registry.register(
            MiniMaxVideoGenerateTool(
                output_dir=workspace_dir / "media" / "generated",
                credential_resolver=(
                    self._platform_store.resolve_credential if self._platform_store else None
                ),
            )
        )

    def _load_plugin_tools(self) -> None:
        """加载外部工具插件"""
        assert self._tool_registry is not None

        for plugin_cfg in self.config.plugins.tools:
            try:
                module_path, class_name = plugin_cfg.class_path.rsplit(".", 1)
                module = importlib.import_module(module_path)
                tool_cls = getattr(module, class_name)

                if not (isinstance(tool_cls, type) and issubclass(tool_cls, Tool)):
                    logger.warning(
                        "engine.plugin_tool_invalid",
                        class_path=plugin_cfg.class_path,
                        error="Not a subclass of Tool",
                    )
                    continue

                # Instantiate — pass config if tool accepts it
                try:
                    tool = tool_cls(**plugin_cfg.config)
                except TypeError:
                    tool = tool_cls()

                self._tool_registry.register(tool)
                logger.info(
                    "engine.plugin_tool_loaded",
                    class_path=plugin_cfg.class_path,
                    tool_name=tool.name,
                )
            except Exception as e:
                logger.error(
                    "engine.plugin_tool_failed",
                    class_path=plugin_cfg.class_path,
                    error=str(e),
                )

    async def _load_agents(self) -> None:
        """从配置目录加载所有 Agent"""
        assert self._agent_registry is not None
        assert self._message_bus is not None
        assert self._llm_gateway is not None
        assert self._tool_registry is not None

        agents_dir = self._config_dir / "agents"
        agent_configs = load_all_agent_configs(agents_dir)

        for cfg in agent_configs:
            await self.add_agent(cfg, start_immediately=False)

        logger.info(
            "engine.agents_loaded",
            count=len(agent_configs),
        )

    async def add_agent(self, config: AgentConfig, start_immediately: bool = True) -> None:
        """Register an Agent created from the UI without requiring an engine restart."""
        assert self._agent_registry is not None
        assert self._message_bus is not None
        assert self._llm_gateway is not None
        assert self._tool_registry is not None
        agent = create_agent(
            config=config,
            message_bus=self._message_bus,
            llm_gateway=self._llm_gateway,
            tool_registry=self._tool_registry,
            memory_store=self._memory_store,
            execution_logger=self._execution_logger,
            skills_dir=self._config_dir / "skills",
        )
        self._agent_registry.register(agent)
        if start_immediately and self._running:
            self._agent_tasks.append(asyncio.create_task(agent.start()))
            if self.config.agent_health.enabled:
                await agent.check_health(self.config.agent_health.timeout_seconds)

    def _load_cron_jobs(self) -> None:
        """从工作流配置中加载 Cron 任务"""
        assert self._scheduler is not None
        workflows_dir = self._config_dir / "workflows"
        workflow_configs = load_all_workflow_configs(workflows_dir)

        for wf in workflow_configs:
            if wf.trigger.type == "cron" and wf.trigger.cron:
                self._scheduler.add_job(
                    workflow_id=wf.id,
                    cron_expr=wf.trigger.cron,
                    input_data=wf.trigger.input,
                    timezone=wf.trigger.timezone,
                )

    def sync_workflow_schedule(self, workflow: WorkflowConfig) -> None:
        """Apply a saved workflow trigger to the live scheduler immediately."""
        if self._scheduler is None:
            return
        if workflow.trigger.type == "cron" and workflow.trigger.cron:
            self._scheduler.upsert_job(
                workflow_id=workflow.id,
                cron_expr=workflow.trigger.cron,
                input_data=workflow.trigger.input,
                timezone=workflow.trigger.timezone,
            )
        else:
            self._scheduler.remove_job(workflow.id)

    async def start(self) -> None:
        """启动引擎 — 启动所有 Agent 和调度器"""
        if not self._initialized:
            await self.initialize()

        self._running = True
        logger.info("engine.starting")

        # 启动所有 Agent 的消息监听
        assert self._agent_registry is not None
        for agent in self._agent_registry.list_agents():
            task = asyncio.create_task(agent.start())
            self._agent_tasks.append(task)

        if self.config.agent_health.enabled:
            await self.check_agent_health()
            health_task = asyncio.create_task(self._health_monitor())
            self._agent_tasks.append(health_task)

        # 启动调度器
        assert self._scheduler is not None
        scheduler_task = asyncio.create_task(self._scheduler.start())
        self._agent_tasks.append(scheduler_task)

        logger.info(
            "engine.started",
            agents=len(self._agent_registry.list_agents()),
        )

    async def check_agent_health(self, agent_id: str | None = None) -> dict[str, dict]:
        """Probe one or all registered Agents concurrently."""
        if self._agent_registry is None:
            return {}
        if agent_id is not None:
            agent = self._agent_registry.get(agent_id)
            if agent is None:
                raise ValueError(f"Agent not found: {agent_id}")
            agents = [agent]
        else:
            agents = self._agent_registry.list_agents()
        if not agents:
            return {}
        results = await asyncio.gather(
            *(agent.check_health(self.config.agent_health.timeout_seconds) for agent in agents)
        )
        return {agent.id: result for agent, result in zip(agents, results, strict=True)}

    async def _health_monitor(self) -> None:
        """Periodically refresh actual Agent endpoint/model readiness."""
        interval = self.config.agent_health.interval_seconds
        logger.info("agent_health.monitor_started", interval_seconds=interval)
        try:
            while self._running:
                await asyncio.sleep(interval)
                if self._running:
                    await self.check_agent_health()
        except asyncio.CancelledError:
            logger.info("agent_health.monitor_stopped")
            raise

    async def stop(self) -> None:
        """停止引擎"""
        self._running = False
        logger.info("engine.stopping")

        # 停止所有 Agent
        if self._agent_registry:
            for agent in self._agent_registry.list_agents():
                await agent.stop()

        # 停止调度器
        if self._scheduler:
            await self._scheduler.stop()

        # 取消所有异步任务
        for task in self._agent_tasks:
            task.cancel()
        if self._agent_tasks:
            await asyncio.gather(*self._agent_tasks, return_exceptions=True)
        self._agent_tasks.clear()

        # 关闭消息总线
        if self._message_bus:
            await self._message_bus.stop()

        logger.info("engine.stopped")

    async def run_workflow(
        self,
        workflow_id: str,
        input_data: str,
        event_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        run_id: str | None = None,
    ) -> WorkflowResult:
        """执行指定工作流"""
        workflows_dir = self._config_dir / "workflows"
        workflow_configs = load_all_workflow_configs(workflows_dir)

        wf_config = None
        for wf in workflow_configs:
            if wf.id == workflow_id:
                wf_config = wf
                break

        if wf_config is None:
            raise ValueError(f"Workflow not found: {workflow_id}")

        assert self._agent_registry is not None
        assert self._message_bus is not None

        execution_config = self._scope_agent_instances(wf_config)
        execution_registry, execution_agents = self._create_execution_agents(execution_config)
        execution_tasks = [asyncio.create_task(agent.start()) for agent in execution_agents]

        orchestrator = create_orchestrator(
            config=execution_config,
            agent_registry=execution_registry,
            message_bus=self._message_bus,
            llm_gateway=self._llm_gateway,
            event_callback=event_callback,
            run_id=run_id,
        )
        try:
            return await orchestrator.execute(input_data)
        finally:
            for agent in execution_agents:
                await agent.stop()
            for task in execution_tasks:
                task.cancel()
            if execution_tasks:
                await asyncio.gather(*execution_tasks, return_exceptions=True)

    def _scope_agent_instances(self, config: WorkflowConfig) -> WorkflowConfig:
        """Give workflow entities a unique message identity for each execution."""
        if not config.agent_instances:
            return config

        scoped = config.model_copy(deep=True)
        namespace = f"run-{uuid.uuid4().hex[:12]}"
        identifiers = {
            instance.id: f"{namespace}--{instance.id}" for instance in scoped.agent_instances
        }
        scoped.agent_instances = [
            instance.model_copy(update={"id": identifiers[instance.id]})
            for instance in scoped.agent_instances
        ]
        scoped.agents = [identifiers.get(agent_id, agent_id) for agent_id in scoped.agents]
        scoped.flow.entry = identifiers.get(scoped.flow.entry, scoped.flow.entry)
        scoped.flow.routes = {
            identifiers.get(source, source): [
                Route(
                    target=identifiers.get(route.target, route.target),
                    condition=route.condition,
                    payload_mapping=route.payload_mapping,
                )
                for route in routes
            ]
            for source, routes in scoped.flow.routes.items()
        }
        scoped.flow.terminate_on = [
            {
                **condition,
                "agent": identifiers.get(condition.get("agent"), condition.get("agent")),
            }
            for condition in scoped.flow.terminate_on
        ]
        scoped.flow.join = {
            identifiers.get(target, target): join.model_copy(
                update={
                    "wait_for": [identifiers.get(agent_id, agent_id) for agent_id in join.wait_for]
                }
            )
            for target, join in scoped.flow.join.items()
        }
        if scoped.flow.supervisor:
            supervisor_id = scoped.flow.supervisor.agent_id
            scoped.flow.supervisor = scoped.flow.supervisor.model_copy(
                update={"agent_id": identifiers.get(supervisor_id, supervisor_id)}
            )
        overrides = scoped.context.get("agent_role_overrides")
        if isinstance(overrides, dict):
            scoped.context["agent_role_overrides"] = {
                identifiers.get(agent_id, agent_id): value for agent_id, value in overrides.items()
            }
        return scoped

    def _create_execution_agents(self, config: WorkflowConfig) -> tuple[AgentRegistry, list]:
        """Instantiate workflow entities from their reusable Agent templates."""
        assert self._agent_registry is not None
        assert self._message_bus is not None
        assert self._llm_gateway is not None
        assert self._tool_registry is not None
        if not config.agent_instances:
            return self._agent_registry, []

        registry = AgentRegistry()
        execution_agents = []
        all_candidate_configs = [
            agent.config.model_copy(deep=True)
            for agent in self._agent_registry.list_agents()
            if agent.health_state != AgentHealthState.UNHEALTHY
        ]
        for instance in config.agent_instances:
            template = (
                self._agent_registry.get(instance.template_id) if instance.template_id else None
            )
            if instance.template_id and template is None:
                raise ValueError(f"Agent template not found: {instance.template_id}")

            entity_config = (
                template.config.model_copy(deep=True)
                if template is not None
                else AgentConfig(
                    id=instance.id,
                    name=instance.name,
                    role=(instance.discovery.description if instance.discovery else ""),
                    model=self.config.default_model.model_copy(deep=True),
                    retry_limit=1,
                )
            )
            entity_config.id = instance.id
            entity_config.name = instance.name
            if instance.model_profile_id:
                if self._platform_store is None:
                    raise ValueError("Model profile support requires a platform store")
                profile = self._platform_store.get_model_profile(instance.model_profile_id)
                if profile is None:
                    raise ValueError(f"Model profile not found: {instance.model_profile_id}")
                entity_config.model = ModelConfig.model_validate(profile["config"])
                entity_config.parameters["model_profile_id"] = instance.model_profile_id
            discovery_policy = instance.discovery or instance.fallback_discovery
            if discovery_policy is not None:
                candidate_configs = [item.model_copy(deep=True) for item in all_candidate_configs]
                if template is not None:
                    preferred_config = entity_config.model_copy(deep=True)
                    preferred_config.id = template.id
                    candidate_configs = [
                        preferred_config if item.id == template.id else item
                        for item in candidate_configs
                    ]
                entity_config.retry_limit = 1
                entity = DiscoveredAgent(
                    config=entity_config,
                    message_bus=self._message_bus,
                    llm_gateway=self._llm_gateway,
                    tool_registry=self._tool_registry,
                    memory_store=self._memory_store,
                    execution_logger=self._execution_logger,
                    skills_dir=self._config_dir / "skills",
                    candidate_configs=candidate_configs,
                    discovery=discovery_policy,
                    preferred_template_id=(
                        instance.template_id if instance.fallback_discovery else None
                    ),
                )
            else:
                entity = create_agent(
                    config=entity_config,
                    message_bus=self._message_bus,
                    llm_gateway=self._llm_gateway,
                    tool_registry=self._tool_registry,
                    memory_store=self._memory_store,
                    execution_logger=self._execution_logger,
                    skills_dir=self._config_dir / "skills",
                )
            registry.register(entity)
            execution_agents.append(entity)
        return registry, execution_agents

    async def _scheduled_run(self, workflow_id: str, input_data: str) -> None:
        """调度器回调 — 执行工作流"""
        run_id = f"scheduled-{uuid.uuid4().hex[:12]}"
        workflow: PlatformWorkflow | None = None
        if self._platform_store is not None:
            workflow = self._platform_store.get_workflow(workflow_id)
            if workflow is None:
                configs = load_all_workflow_configs(self._config_dir / "workflows")
                config = next((item for item in configs if item.id == workflow_id), None)
                if config is not None:
                    workflow = PlatformWorkflow.from_workflow_config(config)
            if workflow is not None:
                self._platform_store.create_run(run_id, workflow, input_data)

        async def record_event(event_type: str, data: dict[str, Any]) -> None:
            if self._platform_store is None or workflow is None:
                return
            timestamp = datetime.now(UTC).isoformat()
            agent_id = data.get("agent_id") or data.get("supervisor_agent_id")
            node_id = workflow.node_id_for_agent(agent_id) if isinstance(agent_id, str) else None
            if node_id is not None:
                data["node_id"] = node_id
            self._platform_store.record_event(run_id, event_type, data, timestamp)
            if node_id is None or not isinstance(agent_id, str):
                return
            if event_type == "node.task_assigned":
                self._platform_store.update_node_run(run_id, node_id, agent_id, "queued")
            elif event_type == "node.task_started":
                self._platform_store.update_node_run(run_id, node_id, agent_id, "running")
            elif event_type == "node.result_ready":
                self._platform_store.update_node_run(
                    run_id,
                    node_id,
                    agent_id,
                    "completed",
                    output=data.get("payload"),
                )
            elif event_type == "node.error":
                self._platform_store.update_node_run(
                    run_id,
                    node_id,
                    agent_id,
                    "error",
                    output=data.get("payload"),
                    error=data.get("error"),
                )
            elif event_type == "supervisor.review_started":
                self._platform_store.update_node_run(run_id, node_id, agent_id, "reviewing")
            elif event_type == "supervisor.decision_ready":
                self._platform_store.update_node_run(run_id, node_id, agent_id, "completed")

        trace_result: dict[str, Any] | None = None
        trace_error: str | None = None
        try:
            await self.start_workflow_trace(run_id, workflow_id, input_data)
            result = await self.run_workflow(
                workflow_id,
                input_data,
                event_callback=record_event,
            )
            trace_result = result.to_dict()
            if self._platform_store is not None and workflow is not None:
                self._platform_store.complete_run(run_id, result.status, trace_result)
            logger.info(
                "engine.scheduled_workflow_completed",
                workflow_id=workflow_id,
                run_id=run_id,
                status=result.status,
            )
        except Exception as e:
            trace_error = str(e)
            if self._platform_store is not None and workflow is not None:
                self._platform_store.complete_run(run_id, "error", {"error": trace_error})
            logger.error(
                "engine.scheduled_workflow_failed",
                workflow_id=workflow_id,
                run_id=run_id,
                error=trace_error,
            )
        finally:
            await self.finish_workflow_trace(run_id, result=trace_result, error=trace_error)

    def status(self) -> dict:
        """获取系统状态"""
        return {
            "running": self._running,
            "agents": (self._agent_registry.get_states() if self._agent_registry else {}),
            "agent_health": (self._agent_registry.get_health() if self._agent_registry else {}),
            "tools": (self._tool_registry.list_tools() if self._tool_registry else []),
            "token_usage": (self._llm_gateway.token_tracker.summary() if self._llm_gateway else {}),
        }
