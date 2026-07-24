# AxonFlow 前端

AxonFlow 的管理台基于 React 19、TypeScript、Vite、Ant Design 和 React Flow。它通过后端 REST API 管理配置与运行记录，并通过 WebSocket 展示实时执行事件。

## 开发

```bash
npm install
npm run dev
```

默认开发地址为 `http://localhost:5173`。同时在仓库根目录启动 API：

```bash
python -m uvicorn axonflow.api.app:app --port 8000 --reload
```

前端请求使用同源 `/api` 与 `/ws` 路径；`vite.config.ts` 会在开发时将它们代理到 `http://localhost:8000`。

## 命令

```bash
npm run dev      # Vite 开发服务器
npm run build    # TypeScript 校验并构建到 dist/
npm run lint     # ESLint
npm run preview  # 预览已构建的 dist/
```

构建产物位于 `frontend/dist/`；该目录存在时，FastAPI 应用会将其挂载为根路径静态站点。

## 页面

| 路径 | 功能 |
|---|---|
| `/` | Dashboard：系统、Agent、工具和 Token 概览 |
| `/workflows` | 工作流列表与可视化编辑入口 |
| `/workflows/new` | 新建可运行的工作流 |
| `/workflows/:id` | DAG、YAML 和工作流详情 |
| `/workflows/:id/runs/:runId` | 工作流运行状态与实时事件 |
| `/agents`、`/agents/:id` | Agent、Persona、模型、Codex 与远程 Agent 配置 |
| `/skills` | 目录型 Skill 包导入、文件树与文本资源管理 |
| `/logs` | 工具与 Agent 执行日志 |
| `/observability` | LLM Trace |
| `/settings` | 全局运行配置 |

工作流画布保存后，后端会将可运行定义写回 `config/workflows/*.yaml`，同时在 SQLite 保存画布布局和运行记录。因此 YAML 是 CLI/引擎的运行来源，平台数据负责视觉元数据与历史。

工作流创建/编辑页可选择手动或 Cron 触发，并配置 IANA 时区和定时输入。保存 Cron 后会立即注册到运行中的调度器，切回手动会立即取消；定时执行也会显示在 Run History。编辑器的回路边会使用外侧回线，避免直接穿过节点。

工作流还可启用连续托管模式：每轮完整运行结束后自动开始下一轮，并可设置最大循环次数、轮次间隔、异常停止和基于最终结果字段的终止条件。托管状态会持久化，服务重启后自动恢复尚未结束的任务；每轮仍作为独立运行显示在 Run History。

同一页面可选择 Flat 或 Supervisor 编排模式。Supervisor 模式要求选择画布中的具体 Supervisor 节点，并配置独立职责、能力、初始规划与失败介入策略；运行时 Supervisor 会审阅每批节点的完整结果后控制下一步，画布静态链路仅作为建议。

选中链路后可配置完整 Route Condition（payload 字段、操作符和值）及 Transferred Payload。条件由后端编排器直接计算，不是 Prompt 判断；传输字段为空表示传递完整业务 payload，也可以筛选字段并指定哪个字段作为下游 `task`。

Dashboard 分别展示 Agent 的 Activity 和 Health。`Listening` 只表示消息循环已启动；模型或远程端点探测成功后才显示 `Ready`，失败时显示 `Unavailable`、探测时间、延迟和错误。

Agent 详情页可配置 `max_concurrent`。同一 Agent 可并行处理来自不同工作流的任务，同一工作流内部仍保持串行；并发调整立即作用于正在运行的 Agent，已执行任务不会被中断。涉及代码修改或共享文件写入的 Agent 建议保持并发数为 1。

新建 Agent 时可选择 **Codex coding Agent**，绑定本地代码仓库、沙箱、超时和健康探针。该 Agent 使用本机已经登录的 Codex CLI，能够接收工作流上游的完整消息信封，直接修改并测试仓库，再把改动文件和测试结果结构化传给下游节点。

Settings 中的 Credential 与 Model Profile 支持创建、编辑和删除。编辑加密 Credential 时可留空新密钥以保留原值；编辑 Model Profile 后，后端会同步更新引用它的 Agent 模板和运行实例。`api_key_env` 只接受环境变量名称，不接受密钥值。

Model Profile 的模型字段是可输入的下拉选择：选择 Provider 后显示该供应商的常见模型 ID，也可以直接输入目录之外的自定义模型 ID。

画布左侧的紫色 **Dynamic Agent** 是运行时发现占位节点：它只保存能力描述、必需 Skills/Tools 和超时，不绑定 Agent 模板。固定节点也可在设置中启用错误/超时后的动态替换。完整契约见 [复杂 Agent 接入、发现与故障替换](../docs/AGENT_INTEGRATION.md)。

Skills 页面将 Skill 作为完整目录包管理。每个包必须包含 `SKILL.md`，并可附带 `scripts/`、`references/`、`assets/` 或自定义目录；支持从本地选择文件夹整体导入、保留二进制资产、浏览文件树并编辑包内文本资源。完整说明见 [Skill 目录包与本地导入](../docs/SKILL_PACKAGES.md)。

新建和编辑工作流时，Agent 模板及画布节点会按实时健康状态着色：绿色为 `Ready`，红色为 `Unavailable`，橙色为 `Checking`，灰色为 `Unknown`；编辑器每 10 秒刷新一次状态。左侧搜索框支持按 Agent 名称或描述实时筛选，紫色动态发现占位节点始终保留。
