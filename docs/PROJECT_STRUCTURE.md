# AxonFlow 项目结构

> 更新：2026-07-16。以下为当前仓库的主要目录；运行时缓存、构建产物和历史设计稿不逐项展开。

```text
AxonFlow/
├── config/                         # 运行配置
│   ├── axonflow.yaml                # 全局模型、Redis、日志、安全与 Webhook
│   ├── agents/                      # Agent YAML 与目录式 Persona
│   ├── skills/                      # 托管 Skill（SKILL.md）
│   └── workflows/                   # YAML 工作流运行定义
├── docs/
│   ├── PRD.md                       # 产品范围与近期优先项
│   ├── AGENT_INTEGRATION.md         # 复杂 Agent、发现协议与故障替换
│   ├── TECHNICAL_DESIGN.md          # 当前技术架构
│   ├── WORKFLOW_PATTERNS.md         # 编排模式、局部 ReAct 与质量闭环
│   ├── PROJECT_STRUCTURE.md         # 本文件
│   ├── specs/                       # 历史规格
│   └── superpowers/                 # 历史计划与规格
├── frontend/                        # React + TypeScript 管理台
│   ├── src/api/                     # REST 与 WebSocket 客户端
│   ├── src/components/              # 工作流画布、YAML、实时日志组件
│   ├── src/layouts/                 # 主布局
│   ├── src/pages/                   # Dashboard、工作流、Agent、Skill、日志等
│   └── dist/                        # 前端构建产物（存在时由 API 挂载）
├── src/axonflow/
│   ├── agents/                       # HTTP Remote Agent 与动态发现包装器
│   ├── api/                         # FastAPI 应用、路由、WebSocket
│   ├── cli/                         # Typer CLI
│   ├── config/                      # Pydantic 模型与 YAML/Skill 加载
│   ├── core/                        # Agent、上下文、Flat/Supervisor 编排、调度器
│   ├── discovery/                   # 本地 ADP-lite 能力发现与排序
│   ├── llm/                         # LiteLLM 网关、Prompt、Token/Trace
│   ├── memory/                      # 内存存储接口与实现
│   ├── messaging/                   # Redis 与内存消息总线
│   ├── observability/               # 结构化日志、执行日志、LangSmith
│   ├── platform/                    # SQLite 平台数据、可视化工作流模型、凭据
│   ├── security/                    # 沙箱与密钥相关能力
│   ├── tools/                       # 内置工具与注册表
│   └── engine.py                    # 组装模块、加载配置、运行工作流
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── docker/docker-compose.yml        # Redis 等本地依赖编排
├── workspace/                       # Agent 工作目录与平台 SQLite 数据
├── logs/                            # 运行日志目录
├── pyproject.toml                   # Python 包与工具配置
└── README.md                        # 项目入口
```

## 模块关系

```text
CLI / Web UI
    │
FastAPI routes ─────────────── PlatformStore (SQLite)
    │                                      │
AxonFlowEngine ── Config loader ───────────┘
    ├── AgentRegistry ── Base / Remote / DiscoveredAgent ── LLMGateway / ToolRegistry
    ├── Orchestrator factory ── FlatOrchestrator / SupervisorOrchestrator
    ├── MessageBus ── RedisMessageBus / InMemoryMessageBus
    ├── Scheduler
    └── ExecutionLogger / LangSmith
```

工作流 YAML 是 CLI 与引擎的运行来源。平台层在保存画布时会同步写入该 YAML，并将画布位置、运行记录和事件保存到 `workspace/axonflow.db`。
