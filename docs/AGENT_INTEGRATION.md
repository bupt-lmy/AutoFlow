# 复杂 Agent 接入、发现与故障替换

> 更新：2026-07-16。本文描述当前已经实现的接入契约。AxonFlow 借鉴 AIP/ADP 的任务、产物与能力发现思想，但当前实现是本地 `aip-lite/0.1` 与 `ADP-lite`，不等同于完整 ACPs/AIP-PUB 网络协议实现。

## 接入方式

复杂 Agent 可以通过四种方式加入注册表，四者都能成为固定工作流节点或动态发现候选者：

| 方式 | 适用场景 | Agent 需要提供的内容 |
|---|---|---|
| 基础 Agent | 主要依赖 LLM、Skill 和内置工具完成任务 | YAML 中的角色、模型、工具、Skill 与能力标签 |
| Codex Agent | 需要直接读取、修改并测试本地代码仓库 | 已登录的 Codex CLI、受限工作目录、沙箱和超时配置 |
| 自定义 Agent | 需要状态机、长任务、CI 质量门或专用 SDK | 继承 `BaseAgent` 的 Python 类，以及可导入的 `class_path` |
| Remote Agent | 已经独立部署的服务或第三方 Agent | 同步 HTTP endpoint、鉴权配置和约定的 JSON 响应 |

一个可发现 Agent 至少应准确声明以下内容：

```yaml
id: agent-security-reviewer
name: "安全评审 Agent"
role: "检查 Python 代码中的安全缺陷，给出证据和修复建议。"
agent_type: custom
class_path: "my_agents.security.SecurityReviewAgent"
tags: [trusted, security, internal]
tools: [file_read, text_search]
skills: [code-review]
retry_limit: 2
parameters:
  severity_threshold: high
```

- `id` 必须在注册表中唯一。
- `role`、`name`、`tags`、`tools`、`skills` 共同形成本地能力描述；描述越具体，发现排序越可靠。
- `tools` 和 `skills` 是硬约束：发现请求中要求的项必须全部由候选 Agent 声明。
- `parameters` 由自定义实现解释，不参与默认发现评分。
- 需要驱动条件路由时，Agent 应返回结构化 `status`、`content`、`error`、`evidence` 或产物信息，不能只在自然语言中描述失败。

自定义类的构造函数应兼容 `BaseAgent`，主要实现：

```python
from axonflow.core.agent import BaseAgent


class SecurityReviewAgent(BaseAgent):
    async def handle_message(self, message):
        # 执行专用逻辑、轮询长任务或调用内部 SDK
        return {
            "status": "success",
            "content": "review complete",
            "evidence": {"report": "workspace/security-report.json"},
        }
```

Remote Agent 配置示例：

```yaml
id: agent-remote-evaluator
name: "远程评测服务"
role: "运行外部评测并返回结构化状态。"
agent_type: remote
tags: [evaluation, remote]
parameters:
  remote:
    endpoint: "https://agent.example.com/tasks"
    method: POST
    timeout: 600
    api_key_env: EVALUATOR_API_KEY
    auth_header: Authorization
    auth_scheme: Bearer
```

服务收到：

```json
{
  "task": {"task": "...", "_protocol": {}},
  "workflow_id": "...",
  "agent_id": "..."
}
```

服务应同步返回 JSON。HTTP 错误、连接异常或返回的 `status: error` 会被视为失败；纯文本响应会被包装为成功结果。长任务目前应由远端自行轮询完成后再响应，或在自定义 Agent 中实现轮询。

## Codex 编码 Agent

仓库内置 `agent_type: codex`，通过本机已安装且已登录的 Codex CLI 接受链路上传来的编码任务。示例配置位于 `config/agents/codex-coder.yaml`，启动后会显示为 **Codex 编码 Agent**，可直接拖入 Flat 或 Supervisor 工作流。

```yaml
agent:
  id: agent-codex-coder
  name: "Codex 编码 Agent"
  role: "根据上游需求修改仓库、运行测试并返回结构化结果。"
  agent_type: codex
  tags: [coding, repository, local-runner, codex]
  retry_limit: 1
  memory: {enabled: false}
  parameters:
    codex:
      command: codex
      working_directory: .
      allowed_working_directories: [.]
      allow_dynamic_working_directory: false
      sandbox: workspace-write
      timeout_seconds: 1800
      health_check: exec
      health_timeout_seconds: 60
      ephemeral: true
      skip_git_repo_check: false
```

执行前应先在运行 AxonFlow 的同一系统账户中确认：

```bash
codex --version
codex login status
```

### 输入与 Prompt 组装

`CodexAgent` 不调用 AxonFlow 的普通 `LLMGateway`。每次收到 `Message` 后，它将以下内容组装为 `codex exec` 的标准输入：

1. Agent YAML 中声明的 `role`。
2. 固定执行约束：仅在工作目录内操作、保留无关改动、不提交/推送、不等待交互输入、必须运行适当测试。
3. 完整 AxonFlow 信封：`sender`、消息类型、Workflow/Step/Session/Task ID、业务 `payload`、`_protocol` 和消息 `context`。
4. 结构化输出要求：状态、摘要、改动文件、测试命令及剩余问题。

CLI 使用参数数组和 stdin 启动，不经过 Shell 拼接；默认命令形态为：

```text
codex exec --json --color never --sandbox workspace-write --cd <repo> \
  --ephemeral --output-schema <schema> --output-last-message <result> -
```

Codex 的 JSONL 事件用于提取 `thread_id` 和 Token 用量，最终结构化结果会作为下游节点收到的 payload：

```json
{
  "status": "success",
  "outcome_status": "success",
  "content": "实现摘要",
  "files_changed": ["src/example.py", "tests/test_example.py"],
  "tests": [{"command": "pytest -q", "status": "passed", "output": "2 passed"}],
  "notes": [],
  "codex": {
    "thread_id": "...",
    "working_directory": "/repo",
    "return_code": 0,
    "sandbox": "workspace-write",
    "usage": {}
  },
  "artifacts": [{"type": "file", "uri": "src/example.py"}]
}
```

`blocked` 会映射为 AxonFlow `error`，从而触发固定节点的故障替换或 Supervisor 干预；`partial` 保持成功传输，但在 `outcome_status` 中保留部分完成语义，供下游测试/评测 Agent 或 Supervisor 判断。

### 工作目录与安全边界

- 默认禁止上游消息覆盖 `working_directory`，避免 Prompt 把 Codex 引导到任意目录。
- 如需一个 Agent 服务多个仓库，必须同时启用 `allow_dynamic_working_directory` 并配置 `allowed_working_directories`；请求路径解析后必须位于允许根目录下。
- UI 只提供 `read-only` 和 `workspace-write`。配置文件虽然能表达 `danger-full-access`，生产环境不应使用。
- 默认 `retry_limit: 1`，防止同一编码任务在失败后自动重复修改仓库。
- 默认禁止提交、推送、发布和外部联系；这些动作需要另设职责明确、权限独立的后续 Agent。

### Ready 状态

默认 `health_check: exec` 会定期启动一次 `read-only`、`ephemeral` 的真实 Codex 请求，只有 CLI、登录、模型服务和返回事件均可用时才标记 Ready。也可以选择成本较低但保证较弱的 `auth`（只检查 `codex login status`）或 `binary`（只检查版本命令）。

新建 Agent 页面支持直接选择 **Codex coding Agent**，填写仓库绝对路径、可选模型/Profile、沙箱、超时与健康检查方式。模型/Profile 留空时沿用该系统账户的 Codex 配置。

## 动态占位 Agent

在新建或编辑工作流时，从左侧 Agent 列表拖入紫色 **Dynamic Agent**：

1. 填写节点标签和必填的 `Capability description`。
2. 可选择必需的 Skills、Tools 及单候选超时时间。
3. 节点不保存具体 `agent_id`；执行开始后才在当前注册表中发现候选者。
4. 被选 Agent 使用该工作流节点的稳定身份完成本次步骤，因此原有连线、join 和终止条件无需改写。

对应的运行配置为：

```yaml
workflow:
  id: dynamic-review
  name: "动态评审"
  agents: [dynamic-review--review-slot]
  agent_instances:
    - id: dynamic-review--review-slot
      node_id: review-slot
      template_id: null
      name: "运行时评审 Agent"
      discovery:
        description: "检查 Python 代码的安全性并给出可操作报告"
        required_skills: [code-review]
        required_tools: [file_read, text_search]
        tags: [trusted]
        exclude_agents: []
        max_candidates: 5
        min_score: 0.05
        timeout_seconds: 300
        fallback_on_error: true
        fallback_on_timeout: true
  flow:
    mode: flat
    entry: dynamic-review--review-slot
    terminate_on:
      - {agent: dynamic-review--review-slot, status: success}
```

`ADP-lite` 先执行 Tools、Skills、Tags、排除列表等过滤，再对 Agent 的名称、角色、标签、工具和 Skills 做本地词项相关性评分。当前发现范围仅是本进程已注册 Agent；尚未查询外部 ADP 目录，也没有能力证明、签名或网络级信任协商。

运行时会排除最近健康检查已标记为 `unhealthy` 的候选者。`running` 只代表 Agent 正在监听消息，只有模型/远程端点探活成功后的 `healthy` 才代表 Ready。

## 固定 Agent 的故障替换

固定节点可勾选 **Discover a replacement on error or timeout** 并填写替代能力。执行顺序为：

1. 固定 Agent 始终作为首选候选者。
2. 若它返回错误、抛出异常或超过单候选超时，则记录失败原因。
3. 在排除首选 Agent 后执行能力发现，按排名逐个尝试其他候选者。
4. 成功结果附带 `discovery.selected_agent_id`、匹配分数和此前失败尝试；全部失败则返回结构化错误。

YAML 中对应 `fallback_discovery`：

```yaml
agent_instances:
  - id: resilient-flow--coder
    node_id: coder
    template_id: agent-primary-coder
    name: "Coder"
    fallback_discovery:
      description: "实现并修复 Python 应用代码"
      required_tools: [file_read, file_write]
      timeout_seconds: 180
      fallback_on_error: true
      fallback_on_timeout: true
```

发现替代者不会修改持久化工作流，也不会永久替换 Agent 模板；它只改变当前步骤当前运行所使用的具体执行者。

## AIP-lite 交互契约与 Prompt 组装

内部 `Message` 仍是统一传输信封，但已增加：

- `protocol_version: aip-lite/0.1`
- `session_id`：一次工作流协作会话
- `task_id`：一次节点任务
- `TaskCommand` / `TaskStatus` / `TaskResult`
- `DataItem` 与 `Product`，用于表达文本、文件、结构化数据和最终产物

编排器派发时会在 payload 中加入 `_protocol`。动态发现执行时继续补充请求能力、实际选中的 Agent、当前尝试次数和此前失败记录。`PromptBuilder` 将这些字段放入系统提示，要求 Agent 保留可供下游使用的证据、产物和失败原因。

这解决了“只是自由文本传递”的一部分问题，但业务 payload 仍允许扩展字典。`aip-lite/0.1` 当前是 AxonFlow 内部兼容层，没有声称可直接与完整 AIP-PUB/ACPs 节点互操作；未来可以把本地发现服务替换为外部 ADP Provider，同时保留工作流节点模型。

## 验证范围

仓库测试覆盖以下关键行为：

- AIP-lite 数据项和命令校验。
- Message 协议字段序列化、反序列化与回复继承。
- 动态节点和固定故障替换策略的平台模型往返转换。
- Tools、Skills、Tags 过滤与发现排序。
- 首选 Agent 报错后的替代者发现。
- Agent 无响应超时后的替代者发现。
- 发现上下文注入 Prompt，以及最终 `TaskResult` 产物。

这些是本地、可重复的自动化验证；真实远端 Agent、生产模型和外部 ADP 服务仍需要在目标部署环境做契约与故障演练。
