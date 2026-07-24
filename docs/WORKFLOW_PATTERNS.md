# 工作流模式与局部 ReAct

> 状态：基于 2026-07-16 的代码实现。本文描述的是当前可运行语义，不是未来设计提案。

## 结论

AxonFlow 同时具备两层协作循环：

1. **Agent 内部的局部 ReAct 循环**：`BaseAgent.handle_message()` 会反复调用 LLM、执行其 tool calls、把工具结果回填给 LLM，直至模型给出文本结果或达到 10 轮上限。
2. **工作流层的 Agent 协作循环**：`flat` 编排器按路由分发每个 Agent 的结果，因此可以用条件路由回到上游 Agent；`supervisor` 编排器则由 Supervisor 在每批结果后选择下一步、重试或改派。

它不是“所有模型自由互相聊天”的群聊架构。常规执行路径是“编排器派发 → Agent 执行 → Agent 回传编排器 → 编排器路由”。`BaseAgent.send_request()` 允许自定义 Agent 按 `can_request` 白名单主动发消息，但基础 LLM Agent 没有内置的“向另一个 Agent 发请求”工具，不能仅通过配置让模型自主群聊。

通信的**传输信封**是统一的 JSON `Message`：包含 ID、发送方、接收方、`workflow_id`、`step_id`、`parent_message_id`、消息类型、优先级、TTL、上下文与 `payload`。现在还带有 `aip-lite/0.1` 版本、Session/Task ID，并提供结构化任务状态、数据项和产物模型。业务 `payload` 仍允许扩展字典，所以当前是“版本化任务协作元数据 + 可扩展业务载荷”，不是完整 AIP-PUB/ACPs 网络协议实现。

## 动态节点不是预先固定的链条

画布支持两类节点：

- 固定 Agent：保存具体模板；可选配置错误/超时后的动态替代策略。
- Dynamic Agent：只保存能力描述和 Tools/Skills/Tags 约束，在实际执行时从已注册 Agent 中选择执行者。

两者在图中都有稳定的工作流节点身份。动态发现改变的是“本次由谁执行”，不会改写路由，所以可以直接放入链、分支、回路和 join 中。具体配置与接入要求见 [复杂 Agent 接入、发现与故障替换](AGENT_INTEGRATION.md)。

## Flat：确定性图编排

`flow.mode: flat`（默认）由静态 `routes` 驱动，支持以下图结构：

| 模式 | 配置方式 | 当前语义 |
|---|---|---|
| 链式 | 每个节点指向一个下游节点 | 按结果逐步执行 |
| 条件分支 | 为路由声明 `condition` | 可同时匹配多条路由 |
| 扇出 | 同一节点配置多个无条件路由 | 下游任务先后派发，均会运行 |
| 汇聚 | 为目标节点设置 `join.wait_for` | 等待 `all` 或 `any` 上游结果后派发 |
| 回路 | 下游路由返回已执行节点 | 受 `max_iterations` 与 `timeout` 约束 |

在 `flat` 模式中，路由条件读取 Agent 响应 payload 的字段；`status` 是最常用字段，支持 `eq`、`neq`、`contains`、`gt`、`lt`。

条件判断发生在编排器中，不通过 Prompt，也不交给下游 Agent 再判断。上游 Agent 负责产生结构化 payload；编排器读取所配置的字段并确定是否派发。画布可为每条边配置字段、操作符和值，也可指定要传输的顶层 payload 字段，并把其中一个字段提升为下游 `task`。未配置字段筛选时传输完整业务 payload，AIP-lite 的 `_protocol` 链路元数据始终保留。

## 7×24 运行：定时与连续托管

工作流创建和编辑页面可将触发方式设为 `Scheduled (Cron)`，并配置 Cron 表达式、IANA 时区和每次运行的固定输入。保存后，运行中的调度器会立即新增或更新任务；切回 `Manual only` 会移除任务。配置同时写入 YAML 和平台存储，进程重启后会重新加载。定时运行会进入工作流 Run History；同一工作流的上一次定时执行未结束时，不会重叠启动下一次。

需要“一轮结束后立即继续下一轮”时，可启用工作流的连续托管模式。它会重复执行完整工作流，每轮都产生独立 Run History 和事件记录，并在以下任一条件满足时结束：

- 达到 `max_cycles`；
- 最终结果中的字段满足 `stop_condition`；
- 某轮异常且 `stop_on_error: true`；
- 用户在详情页手动停止。

```yaml
workflow:
  hosting:
    enabled: true
    input: 持续检查并处理待办任务
    max_cycles: 100
    interval_seconds: 5
    stop_on_error: true
    stop_condition:
      field: output.done
      operator: eq
      value: true
```

托管状态持久化在 SQLite 中。AxonFlow 正常重启后会恢复此前处于运行中的托管任务；已收到停止请求的任务不会恢复。当前 Cron 与连续托管都是单进程实现，尚未提供多实例分布式锁。生产环境仍应使用 systemd、Docker restart policy 或 Kubernetes 保证 AxonFlow 服务存活，并持久化 `workspace` 与 `config` 目录。

## Supervisor：动态调度

`flow.mode: supervisor` 使用 `flow.supervisor.agent_id` 指定协调节点。除职责、能力、规划和
失败介入外，还可以配置 `acceptance_criteria`、终止候选/证据门禁、单 Agent 尝试预算、
相同决策限制与复核历史长度。

1. 可选的初始规划要求 Supervisor 返回带 `order` 和 `depends_on` 的步骤；同一 `order` 的步骤会一起派发。
2. 每批 Agent 返回后，Supervisor 会取得完整 payload、步骤 ID、消息类型和近期历史；静态 `routes` 只作为建议，Supervisor 可以接受、覆盖、返工、改派或结束。
3. 如果某步返回 `status: error` 且 `intervention_on_failure: true`，Supervisor 可选择重试、改派、跳过或终止。
4. `status: success` 仅表示 Agent 调用成功，不直接等于任务正确。Supervisor 必须逐项判断验收标准；若开启 `require_evidence`，Agent 还必须在 `evidence_fields` 指定的 payload 字段中返回结构化证据。
5. 配置的终止条件只是完成候选。开启 `require_terminal_candidate` 后，未命中终止条件的 `done=true` 会被拒绝。
6. 发现问题时，Supervisor 派发局部修复并在之后重新验证；`max_attempts_per_agent` 和 `max_repeated_decisions` 会在无进展时将运行标记为 `blocked`，避免无限返工。
7. 最终输出包含 `supervision_report`，记录每个 Agent 的执行次数、监督决策、结构化证据、未解决问题和完成门禁违规。

推荐让测试、审核、发布等自定义 Agent 返回机器可读证据，例如：

```json
{
  "status": "success",
  "content": "回归测试通过",
  "test_results": {"passed": 42, "failed": 0},
  "artifacts": [{"path": "dist/app.tar.gz", "sha256": "..."}]
}
```

若工作流仍使用只返回 `content` 的 BaseAgent，应暂时关闭 `require_evidence`；其余验收门和
重试预算仍然有效。

`depends_on` 当前仅作为规划输出的描述字段；实际依赖控制应使用 `order`、静态路由或 `join` 明确表达。

## 推荐：需求—开发—测试—评测闭环

对于“需求 Agent 给出需求后，编码、测试、评测不断交互，直到形成最终结果”的场景，推荐采用**确定性的质量门回路 + 局部 ReAct**：

```text
需求分析 ─→ 编码 ─→ 测试 ──通过──→ 评测 ──通过──→ 交付
                   │                 │
                   └──失败──────────→ 编码
                                     └──失败──→ 编码
```

每个节点内部都可自行调用文件、Shell、Git、搜索等工具；工作流只负责交接、路由和终止。这样既避免多模型自由对话造成的死循环，也保留了针对局部问题的自主探索能力。

示意配置如下。`agent-requirements`、`agent-evaluator` 和 `agent-delivery` 是需要先注册的 Agent：

```yaml
workflow:
  id: quality-loop
  name: "需求到交付的质量闭环"
  agents:
    - agent-requirements
    - agent-coder
    - agent-tester
    - agent-evaluator
    - agent-delivery
  flow:
    mode: flat
    entry: agent-requirements
    max_iterations: 24
    timeout: 7200
    routes:
      agent-requirements:
        - target: agent-coder
      agent-coder:
        - target: agent-tester
      agent-tester:
        - target: agent-evaluator
          condition: {field: status, operator: eq, value: success}
        - target: agent-coder
          condition: {field: status, operator: eq, value: error}
      agent-evaluator:
        - target: agent-delivery
          condition: {field: status, operator: eq, value: success}
        - target: agent-coder
          condition: {field: status, operator: eq, value: error}
    terminate_on:
      - {agent: agent-delivery, status: success}
```

### 质量门必须返回结构化状态

上例的测试/评测节点必须根据真实结果返回下列 payload 之一：

```json
{"status": "success", "content": "测试通过；覆盖率 92%", "evidence": {"command": "pytest", "exit_code": 0}}
```

```json
{"status": "error", "content": "3 个测试失败", "feedback": "修复空输入处理", "evidence": {"command": "pytest", "exit_code": 1}}
```

这是当前实现的关键约束：通用 `BaseAgent` 的普通文本完成会被封装为 `status: success`。因此它可做局部工具 ReAct，却**不能可靠地把自然语言中的“失败”转为路由状态**。可选实现方式：

- 编写继承 `BaseAgent` 的 `QualityGateAgent`，解析测试工具结果并返回上述 JSON；通过 Agent 的 `class_path` 注册。
- 使用 `agent_type: remote` 对接已有 CI/评测服务；服务直接返回结构化 JSON，并由 `RemoteAgent` 原样保留其 `status`。
- 在自定义 Agent 中调用 `send_request()` 实现更细粒度的反馈，但要限制 `can_request`、迭代次数和超时。

仅修改 `tester.yaml` 中的角色提示，不足以保证状态分支正确。

## 并行评测后汇聚

可将安全、性能、代码质量等评测并行运行，再交给最终质量门汇聚：

```yaml
flow:
  entry: agent-coder
  routes:
    agent-coder:
      - target: agent-test
      - target: agent-security-review
      - target: agent-quality-review
  join:
    agent-evaluator:
      wait_for: [agent-test, agent-security-review, agent-quality-review]
      strategy: all
```

汇聚时，`agent-evaluator` 会收到以各上游 Agent ID 为键的合并 payload。仍应给该 Agent 提供结构化通过/失败输出，才能稳定地进入交付或返工分支。

## 防止无效循环

- 为每个闭环设置足够但有限的 `max_iterations`，并设置 `timeout`。
- 把失败证据、修复目标和产物路径放进 payload/共享上下文，而不是只传递“请重试”。
- 使用明确的交付终止条件；没有 `terminate_on` 的 `flat` 流程通常会运行到迭代上限。
- 将发布、破坏性 Shell 命令和外部写操作放到最后一个独立 Agent，并在生产环境启用适当的沙箱/权限限制。
