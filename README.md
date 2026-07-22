# AxonFlow

**基于多智能体的自治工作流引擎。** AxonFlow 通过 YAML 或 Web 画布定义多 Agent 协作流程，并提供模型网关、工具调用、实时执行观察和配置管理能力。

> 当前状态：Alpha（2026-07-16）。后端测试已验证通过；请在接入生产凭据、文件系统或发布工具前完成权限与安全评估。

## 当前能力

- **工作流编排**：`flat` 模式支持顺序、条件分支、扇出、汇聚（fan-in）和有上限的回路；`supervisor` 模式由模型规划并在每轮结果后决定下一步。
- **局部 ReAct**：每个基础 Agent 可在一次任务内执行“LLM → 工具调用 → 工具结果 → LLM”的循环，最多 10 轮。
- **Agent 管理**：支持 YAML Agent、目录式 Persona（`soul.md` / `user.md` / `workflow.md`）、工作流级 Agent 实体、模型配置复用、托管 Skills，以及 HTTP Remote Agent。
- **动态发现与替换**：工作流可放置不绑定模板的 Dynamic Agent，运行时按能力、Tools、Skills、Tags 发现执行者；固定 Agent 出错或超时时可按策略发现替代者。
- **AIP-lite 协议**：任务消息带版本、Session/Task ID，任务结果可表达状态、数据项和产物；发现选择与失败尝试会注入 Agent Prompt。
- **模型与工具**：LiteLLM 统一接入模型、备用模型；内置 24 个文件、Shell、Git、搜索、抓取、Python、JSON、归档、进程与媒体类工具，并可加载插件工具。
- **平台界面**：React + Ant Design 管理台提供工作流 DAG 创建/编辑、Agent 与 Skill 管理、YAML 编辑、运行记录、执行日志和 LLM Trace 页面；运行事件通过 WebSocket 推送。
- **运行保障**：启动时和运行期间定期探测 Agent 模型/远程端点，区分消息循环 Activity 与真实 Ready 状态；Redis 不可用时降级为内存消息总线；Web 可配置 Cron、时区和定时输入，调度变更即时生效且运行进入历史记录。

## 编排模式与边界

工作流不是只能链式执行。`flat` 路由可以形成如下闭环：

```text
需求 → 需求分析 → 编码 → 测试 ──通过──→ 评测/交付
                         │
                         └──未通过──→ 编码
```

其中“未通过”必须是实际的结构化结果（例如 `{"status": "error", "content": "..."}`），而不只是模型回复中的一句“测试失败”。当前 `BaseAgent` 对正常文本回复固定返回 `status: success`；因此可靠的质量闭环应使用能返回结构化状态的自定义 Agent 或 `remote` Agent。完整说明、可执行的路由范式和限制见 [工作流模式说明](docs/WORKFLOW_PATTERNS.md)。

## 快速开始

### 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cd frontend
npm install
cd ..
```

配置所用模型的凭据，例如：

```bash
export OPENAI_API_KEY="your-api-key"
```

模型、端点和备用模型在 `config/axonflow.yaml` 或 Agent 配置的 `model` 字段中定义。仓库附带的示例 Agent 使用了特定的兼容端点；在其他环境运行前，请替换 `api_base`、模型名和密钥环境变量。

### 验证与运行

```bash
pytest tests/ -q
axonflow status
axonflow run dev-pipeline --input "实现一个快速排序函数"
```

首个影视剪辑 MVP 工作流会执行“媒体探测 → Timeline 规划 → MP4 渲染”。媒体 Worker
需要安装 FFmpeg，输入和输出必须使用本地绝对路径：

```bash
axonflow run video-edit-mvp --input \
  '{"assets":{"asset-source":"/absolute/path/source.mp4"},"output_path":"/absolute/path/result.mp4","target":{"width":1080,"height":1920,"fps":30}}'
```

影视语义剪辑工作流接受“本地源视频或目标链接 + 内容描述”，执行资源导入、镜头切分、
每镜头多帧采样、运动/画面变化/音频冲击特征提取、MiniMax-M3 多模态语义分析、
综合精彩度评分、动作峰值区间精修、帧级精确拼接、本地 Whisper 对白转写、硬字幕烧录、
技术质检和资产登记。无台词镜头不会因缺少对白而被降权；链接只能用于用户有权下载和
处理的资源：

```bash
# 一键生成无版权合成样片并跑通默认工作流（MiniMax 和 Whisper 均为可选增强）
./examples/semantic-video-edit/run-demo.sh
```

完整的新用户说明、依赖降级行为和 Web 平台入口见
[影视语义剪辑默认示例](examples/semantic-video-edit/README.md)。工作流及其 12 个 Agent 配置
均位于 `config/`，克隆源码后会自动出现在平台工作流列表中。

```bash
axonflow run semantic-video-edit --input \
  '{"source":"/absolute/path/source.mp4","description":"找出节奏紧张的追逐和对抗片段，排除静态空镜","target_duration_seconds":30,"hard_subtitles":true}'
```

长视频会先从运动高分、对白高密度和全片时间覆盖三个维度筛出有限的语义分析候选，
再对全部镜头保留确定性特征评分。工作流使用 `ffmpeg-full`/libass 保留源视频动作和原声，
通过 `trim`/`atrim` 精确到目标帧边界，并将 SRT 永久烧录到画面；
URL 导入依赖 `yt-dlp`。自动转写可选依赖 `whisper-cli` 和
`workspace/models/ggml-small.bin`；缺失时工作流继续完成纯视觉选片，并生成基于选片原因的
旁路 SRT。输出写入 `workspace/media/final/`，通过技术质检后才登记。

文本生成视频使用独立的 `text-to-video-generation` 工作流，不读取或剪切已有视频。默认后端
不需要 MiniMax 视频生成额度，执行“可选开放资源搜索 → M3 连续动作分镜规划 → image-01
生成四张关键帧 → FFmpeg 推拉摇移与交叉淡化 → 永久 AI 虚构标识 → H.264/AAC 标准化
→ 质检 → 资产登记”：

```bash
axonflow run text-to-video-generation --input \
  '{"description":"明显虚构的讽刺喜剧 AI 场景：一名公众人物罚点球射失后抱头痛哭","generation_backend":"storyboard","collect_resources":false}'
```

默认 `storyboard` 后端输出的是“多镜头动态分镜视频”：画面有逐帧运镜和镜头转场，但人物肢体
不会像原生视频模型那样逐帧连续运动。涉及公众人物的虚构场景必须保留
`AI GENERATED - FICTIONAL` 画面标识。以后获得 Hailuo 视频额度后，可显式传入
`"generation_backend":"hailuo"`；该后端支持 6 或 10 秒，10 秒只支持 768P，并使用独立 Credits。

旧版 MiniMax 素材合成工作流会执行“创意策划 → 图片/旁白/配乐并行生成 → 素材汇总 → SRT 字幕
→ FFmpeg 成片合成 → 技术质检 → 正式资产登记”。它使用 MiniMax-M3、`image-01`、
`speech-2.8-hd` 和 `music-2.6`，需要先在平台凭据库配置 MiniMax Key，或设置
`MINIMAX_API_KEY`：

```bash
axonflow run video-asset-generation-minimax --input \
  "制作一条关于未来城市清晨苏醒的温暖电影感短片"
```

生成文件分别保存到 `workspace/media/generated/`、`workspace/media/subtitles/` 和
`workspace/media/composed/`。成片固定输出 H.264/AAC、1080p、30 fps、48 kHz Stereo，
内含可开关中文字幕轨；质检会完整解码文件，通过后才写入媒体资产库。配乐生成通常
明显慢于图片和语音，工作流会等待三个并行分支全部成功后再继续。

Web API 已提供本地素材和异步渲染任务入口：

```text
POST /api/assets/upload?name=source.mp4&kind=video   # 请求体为原始文件字节
GET  /api/assets/{asset_id}/content                 # 下载或预览工作区资产
POST /api/render-jobs                               # 提交 Timeline 和 output_name
GET  /api/render-jobs/{job_id}                      # 查询 queued/running/completed 状态
POST /api/render-jobs/{job_id}/cancel               # 取消活动渲染任务
```

上传内容会流式保存到 `workspace/media/assets/`，渲染结果保存到
`workspace/media/renders/`；两者都会记录 SHA-256 并登记到媒体资产库。

运行 Web 管理台：

```bash
# 终端 1：后端
python -m uvicorn axonflow.api.app:app --port 8000 --reload

# 终端 2：前端
cd frontend && npm run dev
```

开发环境访问 `http://localhost:5173`。构建前端后，后端会在 `frontend/dist/` 存在时挂载静态页面：

```bash
cd frontend && npm run build && cd ..
python -m uvicorn axonflow.api.app:app --port 8000
```

## 配置概览

```text
config/
├── axonflow.yaml             # 全局模型、Redis、沙箱、日志、Webhook
├── agents/                   # 单文件或目录式 Agent 配置
│   └── coder/                # config.yaml + 可选 Persona Markdown
├── skills/                   # 托管 Skill（SKILL.md）
└── workflows/                # 可运行的工作流 YAML
```

基础工作流示例：

```yaml
workflow:
  id: dev-pipeline
  name: "软件开发流水线"
  agents: [agent-coder, agent-tester, agent-publisher]
  flow:
    mode: flat
    entry: agent-coder
    max_iterations: 10
    timeout: 3600
    routes:
      agent-coder:
        - target: agent-tester
      agent-tester:
        - target: agent-publisher
          condition: {field: status, operator: eq, value: success}
        - target: agent-coder
          condition: {field: status, operator: eq, value: error}
    terminate_on:
      - {agent: agent-publisher, status: success}
```

## 文档导航

- [工作流模式与局部 ReAct](docs/WORKFLOW_PATTERNS.md)：当前编排语义、闭环范式和落地条件。
- [复杂 Agent 接入、发现与故障替换](docs/AGENT_INTEGRATION.md)：接入契约、动态占位 Agent、ADP-lite 和 AIP-lite。
- [技术设计](docs/TECHNICAL_DESIGN.md)：实际架构、消息流、运行时与已知边界。
- [产品需求与路线](docs/PRD.md)：目标、已完成能力与后续优先项。
- [项目结构](docs/PROJECT_STRUCTURE.md)：当前目录和模块职责。
- [前端说明](frontend/README.md)：前端开发、构建与页面说明。

历史方案保留在 `docs/specs/` 和 `docs/superpowers/`，用于追溯决策，不代表当前实现。
