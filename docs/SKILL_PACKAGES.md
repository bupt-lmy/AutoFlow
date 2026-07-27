# Skill 目录包与本地导入

AxonFlow 的 Skill 是一个可移植目录包，不是单个描述字段。每个目录必须在根目录提供 `SKILL.md`，其余文件由 Skill 自己按需组织。

```text
release-check/
├── SKILL.md                 # 必需：能力、适用时机和操作流程
├── scripts/                 # 可选：确定性脚本和自动化程序
│   └── verify.sh
├── references/              # 可选：规范、模板说明和领域知识
│   └── release-policy.md
├── assets/                  # 可选：图片、模板、样例等二进制/文本资产
│   └── report-template.docx
└── examples/                # 可选：Skill 自定义的其他目录
    └── expected-output.json
```

## SKILL.md

入口文件必须是 UTF-8 文本且不能为空。建议使用 YAML frontmatter 声明名称和能力摘要：

```markdown
---
name: Release Check
description: Validate build artifacts and release evidence before publishing.
---

# Workflow

1. Read `references/release-policy.md`.
2. Run @script:verify.sh.
3. Report failed gates and evidence.
```

`name` 和 `description` 会显示在 Skill 管理列表。没有 frontmatter 时，平台使用第一个 Markdown 标题和第一个正文段落生成展示信息。

`@script:<relative-path>` 会被运行时解析为 `scripts/` 下的绝对脚本位置。路径必须保持在当前 Skill 的 `scripts/` 内；绝对路径、`..` 和符号链接越界不会解析。

浏览器不能提供本地文件的可执行权限位，因此目录导入不依赖或复制该权限。运行时会根据常见脚本扩展名生成明确的解释器命令，例如 `.sh` 使用 `bash`、`.py` 使用 `python`，导入后的脚本无需额外执行 `chmod`。

## 从本地文件夹导入

在 **Skills → Import Folder** 中选择一个本地目录：

1. 浏览器读取所选目录中的文件和相对路径。
2. 根目录必须包含 `SKILL.md`，大小写会规范化为该名称。
3. 平台保留所有目录层级、文本文件和二进制资产。
4. Skill ID 默认由本地文件夹名生成，可在导入前编辑。
5. 相同 ID 默认返回冲突；只有显式勾选替换时才整体替换原包。

当前单个包限制为 500 个文件、总计 20 MB；单文件不超过 5 MB，`SKILL.md` 不超过 100 KB。`.git`、`__pycache__`、路径穿越、绝对路径、重复路径和非法 Base64 会被拒绝。浏览器端自动忽略 `.DS_Store` 与 `__MACOSX` 元数据。

## 管理与编辑

点击 **Manage** 可以：

- 查看完整包目录树、组件、文件数量和容量。
- 编辑 `SKILL.md`、scripts、references 及其他 UTF-8 文本文件。
- 新建包内文本文件。
- 删除除 `SKILL.md` 以外的文件，并自动清理空目录。
- 查看二进制资产的路径和大小；二进制内容会保留，但不在文本编辑器中打开。
- 用另一个本地文件夹整体替换当前 Skill 包。

## Agent 如何使用 Skill 包

Agent 配置的 `skills` 字段仍然使用 Skill ID：

```yaml
agent:
  id: agent-reviewer
  skills: [release-check]
```

普通 Agent 在组装 System Prompt 时读取 `SKILL.md`，注入 Skill ID、包根目录和入口路径，并解析安全的 `@script:` 引用。Skill 可以在入口中指示 Agent 按需读取 `references/` 或使用 `assets/`，避免把整个目录无条件塞入 Prompt。

Codex Agent 也使用同一加载器；绑定的 Skill 会出现在 Codex 任务 Prompt 的 `Assigned Skill packages` 部分。因此目录型 Skill 可以同时用于普通模型 Agent、动态发现候选者和 Codex 编码 Agent。

导入或编辑 Skill 文件后不需要重启 AxonFlow；Agent 在下一次任务开始组装 Prompt 时重新读取当前文件内容。

## API

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/skills` | 列出 Skill 包、能力摘要、组件和文件清单 |
| `POST` | `/api/skills/import` | 导入完整目录包 |
| `POST` | `/api/skills/{id}` | 创建仅含 `SKILL.md` 的新包 |
| `GET` | `/api/skills/{id}/files/{path}` | 读取包内文件元数据和文本内容 |
| `PUT` | `/api/skills/{id}/files/{path}` | 新建或更新包内 UTF-8 文本文件 |
| `DELETE` | `/api/skills/{id}/files/{path}` | 删除非入口文件 |
| `DELETE` | `/api/skills/{id}` | 删除完整 Skill 包 |
