# AxonFlow 技术设计

> 版本：v0.1.0 Alpha · 更新：2026-07-16 · 范围：当前实现

## 架构概览

```text
                         ┌───────────────────────────┐
                         │ CLI / React 管理台         │
                         └─────────────┬─────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │ FastAPI + WebSocket        │
                         │ 平台数据（SQLite）          │
                         └─────────────┬─────────────┘
                                       │
┌────────────────────────▼─────────────────────────────────────────────┐
│ AxonFlowEngine                                                        │
│  Config loader · AgentRegistry · ToolRegistry · Scheduler             │
│                                                                         │
│  ┌──────────────────┐        ┌─────────────────────────────────────┐ │
│  │ Orchestrator     │        │ Agent Runtime                       │ │
│  │ Flat / Supervisor├──消息──► Base / Remote / DiscoveredAgent      │ │
│  └────────┬─────────┘        │ LLM → Tool Calls → Observation loop │ │
│           │                  └───────┬─────────────────────┬───────┘ │
│  ┌────────▼─────────┐                │                     │         │
│  │ MessageBus       │          ┌─────▼─────┐         ┌────▼──────┐  │
│  │ Redis / Memory   │          │ LLMGateway│         │ ToolRegistry│ │
│  └──────────────────┘          │ LiteLLM   │         └───────────┘  │
│                                └───────────┘                         │
└───────────────────────────────────────────────────────────────────────┘
```

## 运行时

### 引擎与基础设施

`AxonFlowEngine` 负责加载 `config/`、创建消息总线、LLM 网关、工具注册表、共享内存、执行日志和 Agent 注册表，并启动 Agent 消息监听与 Cron 调度。

- 优先使用 `RedisMessageBus`；Redis 连接失败时自动改用 `InMemoryMessageBus`。
- `PlatformStore` 以 SQLite 保存画布元数据、运行记录、事件、模型配置和加密凭据。
- `ExecutionLogger` 记录工具调用；API 将其和编排事件通过 WebSocket 推送给订阅运行。
- LLM Gateway 通过 LiteLLM 调用模型，记录 Token，并可对接 LangSmith。

### Agent

`BaseAgent` 收到任务后执行以下过程：

```text
任务消息 + Persona + 工作流上下文 + 记忆 + Skill + 工具 Schema
                           │
                           ▼
                         LLM 调用
                    ┌──────┴──────┐
              有 tool_calls      文本完成
                    │               │
                    ▼               ▼
               执行工具并回填     TASK_RESPONSE
                    │
                    └──最多 10 轮──┘
```

Agent 支持 Agent/Workflow/Global 三种记忆作用域，保存最近任务和结果。目录式 Agent 配置可将 `soul.md`、`user.md`、`workflow.md` 注入 Prompt；Skill 内容从 `config/skills/` 读取。`RemoteAgent` 将工作流任务转发至 HTTP 服务并使用服务返回的 JSON 作为结果。

### Agent 有界并发

每个 Agent 使用 `max_concurrent` 控制同时执行的任务数，默认值为 1，允许在详情页在线调整。消息循环只在存在空闲执行槽时从消息总线领取下一条任务，因此不会因突发流量无限创建协程。不同 `workflow_id` 的任务可以并行执行；来自同一工作流的消息通过工作流锁保持串行，避免共享 `WorkflowContext` 的读写竞争。运行状态公开当前执行数、等待调度数和活跃工作流 ID。

代码修改、同一路径文件写入及其他具有共享可变资源的 Agent 应保持 `max_concurrent: 1`。无共享状态的模型调用或只读分析 Agent 可以根据模型配额与机器容量提高并发上限。

### Agent 健康状态

Agent 的消息循环状态与可用性分别维护：

- `Activity`：`idle/running/working/error/stopped`，只描述进程内监听和任务执行状态。
- `Health`：`unknown/checking/healthy/unhealthy`；只有真实模型或远程端点探测成功的 Agent 才是 Ready。

引擎启动时并行发送一次最小 `PING` 命令，之后按 `agent_health.interval_seconds` 定期复检，并记录最后检查时间、成功时间、延迟和错误。Remote Agent 可配置独立 `health_endpoint`，否则向任务 endpoint 发送 `health_check/ping`。可调用 `POST /api/agents/{agent_id}/health-check` 复检单个 Agent，或通过 Dashboard 的刷新按钮调用 `POST /api/agents/health-check` 并发复检全部已注册 Agent。

已知为 `unhealthy` 的 Agent 不参与 Dynamic Agent 和故障替换候选排序。自定义的非 LLM Agent 可覆盖 `_health_probe()` 实现自己的探活契约。

### 消息与上下文

编排器使用 `TASK_REQUEST` 向 Agent 发送任务。Agent 用原消息的 `reply()` 回复，成功时为 `TASK_RESPONSE`，错误时为 `ERROR`。每个工作流创建独立的 `WorkflowContext`，包含原始输入、共享状态、消息历史和迭代计数；上下文会在派发前注入所有参与 Agent。

常规路由经由编排器发生，而非 Agent 彼此直接对话。自定义 Agent 可调用 `send_request()`，但目标必须位于该 Agent 的 `can_request` 白名单。

`Message` 信封统一序列化为 JSON，并包含发送方/接收方、工作流与步骤 ID、父消息 ID、消息类型、优先级、TTL、上下文和字典型 `payload`。`aip-lite/0.1` 增加了 Session/Task ID、TaskCommand/Status/Result、DataItem 和 Product。编排器在 `_protocol` 中传递当前任务；发现包装器再写入选中 Agent、尝试次数和失败记录，Prompt 构建器会将其注入系统提示。

业务 payload 仍是可扩展字典。因此路由所依赖的 `status`、`feedback`、`evidence` 等字段需要由自定义/Remote Agent 或应用约定稳定地产生；AIP-lite 当前也不是完整 AIP-PUB/ACPs 的网络互操作实现。

### 动态发现运行时

`DiscoveredAgent` 是工作流级包装器，向编排器暴露稳定的节点 ID。它使用 `LocalDiscoveryService` 对已注册 `AgentManifest` 执行硬约束过滤和词项排序，在运行时创建选中的具体 Agent：

```text
工作流节点 ID
    │
    ├─ Dynamic Agent ──ADP-lite 排名──候选 1──错误/超时──候选 2──成功
    │
    └─ 固定 Agent ──首选模板──错误/超时──ADP-lite 替代者
```

候选执行结果仍以节点 ID 返回，所以 Flat/Supervisor 路由、join、终止条件和运行记录不需要知道具体模板。当前 Provider 只搜索本地注册表；未来可替换为外部目录 Provider。

## 编排器

### FlatOrchestrator

默认的 `flat` 模式读取 `flow.entry` 和 `flow.routes`：

1. 入口 Agent 接收初始任务。
2. 编排器接收结果后，检查 `terminate_on`。
3. 所有匹配的路由都被派发，因此一个节点可扇出到多个下游。
4. 配置了 `join` 的节点等待其 `wait_for` 列表按 `all` 或 `any` 满足后获得合并 payload。
5. 一条路由可回指上游，形成受 `max_iterations`、`timeout` 限制的回路。

路由条件直接读取响应 payload 字段，支持 `eq`、`neq`、`contains`、`gt`、`lt`。判断由编排器确定性执行，不经过 Prompt。上游 Agent 仍必须实际产生相应结构化字段，例如只有结果真的携带 `status: error`，失败分支才会生效。`payload_mapping.include` 可筛选传给下游的顶层业务字段，`payload_mapping.task_field` 可把指定字段提升为下游 `task`；协议元数据不受业务筛选影响。

Cron Trigger 保存表达式、IANA 时区和固定运行输入。工作流 API 在创建/更新后立即向进程内 Scheduler 执行 upsert/remove，服务重启时再从持久化 YAML 加载。调度器维护下一触发时间并禁止同一任务重叠执行；定时运行写入 SQLite Run History。当前不提供停机补跑或多实例分布式锁，因此 7×24 部署仍依赖外部进程守护与持久卷。

连续托管由 `HostedWorkflowManager` 管理。它复用手动运行的统一执行服务，让每一轮都写入完整运行记录、节点状态、WebSocket 事件和 Trace；循环状态写入 `workflow_hosting` 表，并在 API 进程重启时恢复。终止策略支持循环上限、异常状态、手动停止，以及对 `WorkflowResult.to_dict()` 嵌套字段的确定性比较。该管理器同样是单进程实现，不提供多实例选主或分布式锁。

### SupervisorOrchestrator

`supervisor` 模式使用配置的 Supervisor Agent 的模型：

1. 可选全局规划将任务拆为步骤；相同 `order` 的步骤会被并行派发。
2. 一批结果返回后，将每个节点的完整 payload、步骤 ID、消息类型、累计历史、终止候选和静态路由建议交给 Supervisor；静态路由不再绕过审阅。
3. 当收到 `status: error` 且允许干预时，Supervisor 可重试、改派、跳过或终止。
4. Supervisor 的工作流级 `responsibility` 与 `capabilities` 会和所选模板的基础角色一起注入规划、审阅、失败干预和总结 Prompt。
5. 无待处理目标时，Supervisor 对完整步骤结果生成总结；非法或不在当前工作流内的目标会被拒绝。

规划 JSON 中的 `depends_on` 目前没有直接执行约束；要表达真实依赖，应使用 `order`、静态路由或 `join`。

## 平台与 API

FastAPI 路由覆盖系统状态、工作流、Agent、日志、配置、凭据、模型配置、Trace 和 Skill。工作流 API 的特点：

- 首次读取 YAML 工作流时，将其实体化为可视化平台模型。
- 编辑画布后，平台将可运行定义同步写回 `config/workflows/*.yaml`。
- 保存 Cron Trigger 时实时同步运行中调度器；定时运行与手动运行共享历史数据模型。
- 执行时创建 run、异步调用引擎、持久化节点/事件状态，并用 `run_id` 推送 WebSocket 事件。
- 工作流 Agent 实体可从 Agent 模板实例化，并在一次运行中使用唯一消息身份以隔离并发执行。
- Credential 与模型配置均支持创建、编辑和删除；编辑模型配置会同步更新引用它的 Agent YAML 模板及当前运行实例。加密 Credential 编辑时留空新密钥会保留原密文，显式输入时才轮换。
- Provider catalog 同时提供常见模型 ID 建议；模型配置表单会随 Provider 切换建议列表，但允许输入目录外的精确模型 ID，以兼容私有部署和新模型。

## 配置模型

关键 Pydantic 配置类型位于 `src/axonflow/config/models.py`：

- `AgentConfig`：角色、模型、工具、Tags、`can_request`、重试、记忆、Persona、Skill、扩展参数。
- `DiscoveryConfig`：能力描述、Tools/Skills/Tags、排除列表、排序阈值、候选数、超时及错误/超时替换策略。
- `AgentInstanceConfig`：固定模板或动态发现槽，以及固定节点的可选故障替换策略。
- `WorkflowConfig`：参与 Agent、可选工作流实体、触发器、流程和上下文。
- `FlowConfig`：模式、入口、路由、终止条件、join、Supervisor、迭代与超时。
- `ModelConfig`：模型提供商、名称、温度、Token、端点、凭据和 fallback。

## 当前设计边界

| 主题 | 当前行为 | 使用建议 |
|---|---|---|
| 基础 Agent 状态 | 正常文本结果固定为 `success` | 质量门应使用自定义/Remote Agent 返回结构化通过或失败 |
| 直接 Agent 通信 | 仅自定义代码可通过 `send_request()` 主动使用 | 将常规协作放在编排路由，避免无边界群聊 |
| 内存降级 | 仅进程内队列 | 生产环境依赖 Redis，并补充恢复策略 |
| 回路 | 最大迭代与超时终止 | 传递可操作的失败证据并设置交付终止条件 |
| 工具安全 | 工具能力按 Agent 配置授予 | 对 Shell、Git、文件写入和发布动作设置隔离与最小权限 |
| 能力发现 | 只搜索本地注册表，使用确定性词项评分 | 对外部 ADP 增加身份、签名、健康检查和信任策略后再用于跨组织发现 |

关于需求—编码—测试—评测闭环的配置和实现方式，见 [工作流模式与局部 ReAct](WORKFLOW_PATTERNS.md)。
复杂 Agent 的接入契约、动态占位节点和故障替换见 [复杂 Agent 接入、发现与故障替换](AGENT_INTEGRATION.md)。
