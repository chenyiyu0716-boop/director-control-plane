# Director Control Plane v0.1 开发方案

> 文档状态：Draft for implementation  
> 版本：v0.1  
> 目标周期：1 周完成最小可用闭环  
> 首个接入对象：Director Agent（编导 Agent）

## 1. 项目定位

Director Control Plane 是独立于业务 Agent 的运维控制层，用于统一管理多个 AI Agent 项目的知识资产、运行状态和版本信息。

v0.1 的目标不是打造“超级 Agent”，而是把 Director Agent 从需要人工持续维护的项目，逐步变成可被系统管理、可审计、可交接的生产资产。

核心原则：

1. **控制面与业务面分离**：本项目独立部署、独立存储，不侵入业务 Agent 的核心逻辑。
2. **先可观测，后自动化**：v0.1 负责发现、整理、报告和生成建议，不自动修复。
3. **人保留最终决策权**：知识入库、合并、发布、删除及生产配置变更必须人工确认。
4. **所有动作可追踪**：输入、分析结果、审批、输出和失败记录都具有时间、来源和操作者信息。
5. **适配多个项目**：Director Agent 是首个接入对象，数据模型不与单一业务强绑定。

## 2. v0.1 范围

### 2.1 包含

- Knowledge Agent：知识采集、解析、分类、质量检查、生成入库建议及维护文档草案。
- Observer Agent：检查服务、API、容器、数据库、日志和定时任务状态，生成健康报告。
- Release Agent：检查 Git 分支、提交、工作区和发布记录，生成版本报告及 Release Notes 草案。
- Review Queue：统一承载待人工确认的知识变更和高风险建议。
- 飞书通知：发送日报、异常提醒、审批入口和审批结果。
- 审计记录：保存每次运行、检查、建议、审批和发布草案的记录。

### 2.2 明确不包含

v0.1 禁止执行：

- 自动修改业务代码、生产知识库或生产配置；
- 自动合并分支、创建正式发布或自动部署；
- 自动删除、覆盖或批量迁移知识；
- 自动重启服务、修复数据库或清理日志；
- 引入复杂的多 Agent 自主编排和自我迭代机制。

### 2.3 成功闭环

```text
数据源 / 系统 / Git
        ↓
三个专职 Agent 只读检查与分析
        ↓
报告 / 建议 / Review Queue
        ↓
飞书通知与人工确认
        ↓
受控执行或人工处理
        ↓
审计记录
```

## 3. 总体架构

```text
                         Feishu
               日报 / 告警 / 审批 / 结果
                            ↕
┌──────────────── Director Control Plane ────────────────┐
│                                                       │
│  Scheduler / API → Orchestrator → Run & Audit Store   │
│                          │                            │
│              ┌───────────┼───────────┐                │
│              ↓           ↓           ↓                │
│        Knowledge     Observer      Release             │
│          Agent         Agent        Agent              │
│              │           │           │                │
│              └────── Review Queue ───┘                │
└──────────────────────────┬────────────────────────────┘
                           │ 只读优先 / 最小权限
          ┌────────────────┼────────────────┐
          ↓                ↓                ↓
     Knowledge Base   Runtime Systems   Git Repositories
       & Inboxes      API/DB/Logs       & Release Data
```

### 3.1 组件说明

- **API**：接收飞书回调、手工触发和查询请求。
- **Scheduler**：触发日报、周期巡检和知识同步。
- **Orchestrator**：创建运行记录、调用指定 Agent、收集结果并分发通知；不做复杂自治决策。
- **Agents**：每个 Agent 只负责单一领域，并通过适配器访问外部系统。
- **Review Queue**：保存需要人工确认的候选变更。
- **Store**：v0.1 可使用 PostgreSQL；本地开发允许 SQLite。
- **Adapters**：封装 Git、文件系统、HTTP、Docker、数据库和飞书，便于替换与测试。

## 4. 三个 Agent 职责

### 4.1 Knowledge Agent

**目标**：把散落文件转成有来源、有分类、有版本、可审核的知识资产。

输入：Markdown、纯文本、PDF、brief、脚本、爆款案例、复盘、Skill、Prompt；v0.1 对无法可靠解析的格式只登记并提示人工处理。

处理流程：

```text
发现新素材 → 解析 → 去重 → 分类/标签 → 摘要
→ 冲突/过时/缺失检查 → 生成候选变更 → Review Queue
→ 人工批准 → 由受控执行器入库 → 记录审计
```

职责：

- 计算内容指纹，识别完全重复和疑似重复；
- 生成标题、摘要、类型、标签、关联 IP、来源和有效期建议；
- 检查规则冲突、过时内容、缺少来源及缺少必要元数据；
- 生成知识新增、更新或弃用建议，不直接写生产库；
- 基于已批准的生产知识生成以下维护文档草案：
  - `README.md`
  - `KNOWLEDGE_MAP.md`
  - `SKILL_INDEX.md`
  - `CHANGELOG.md`

输出：候选知识条目、质量问题、变更 diff、置信度、审核原因和维护文档草案。

### 4.2 Observer Agent

**目标**：用统一报告回答“系统现在是否正常、哪里异常、需要谁处理”。

检查范围：

- frontend、backend、worker 的存活与关键健康接口；
- Docker 容器状态和重启次数；
- PostgreSQL 连接、基础只读查询及容量阈值；
- API 状态码和响应耗时；
- 日志中的错误率、重复异常和最近失败；
- 定时任务最近运行时间及结果；
- Git 仓库可访问性和部署版本标识。

职责边界：只读检查、聚合、分级、提出建议；不重启、不修复、不改配置。

状态分级：

- `healthy`：核心检查全部通过；
- `warning`：有退化或非核心失败，但服务仍可用；
- `critical`：核心服务不可用、数据源不可访问或任务连续失败；
- `unknown`：权限、网络或配置不足，无法得出可靠结论。

输出：结构化检查结果、总体状态、证据、建议动作、负责人建议和飞书日报/告警。

### 4.3 Release Agent

**目标**：让版本状态透明、变更可回顾、发布信息可标准化。

职责：

- 读取当前分支、默认分支、远端同步状态和工作区状态；
- 汇总指定时间段或版本区间内的提交；
- 识别未提交变更、长期未合并分支和缺失版本标签；
- 按功能、修复、文档、运维和风险分类生成 Release Notes 草案；
- 对发布前检查给出通过、警告或阻断建议；
- 保存报告并通知负责人。

职责边界：不提交、不推送、不建/合并 PR、不打标签、不发布。

## 5. 建议目录结构

```text
director-control-plane/
├── README.md
├── pyproject.toml
├── .env.example
├── docs/
│   ├── DIRECTOR_CONTROL_PLANE_V0.1.md
│   ├── OPERATIONS.md
│   └── SECURITY.md
├── src/control_plane/
│   ├── api/
│   │   ├── routes_health.py
│   │   ├── routes_runs.py
│   │   └── routes_feishu.py
│   ├── agents/
│   │   ├── knowledge.py
│   │   ├── observer.py
│   │   └── release.py
│   ├── adapters/
│   │   ├── filesystem.py
│   │   ├── git.py
│   │   ├── http.py
│   │   ├── docker.py
│   │   ├── database.py
│   │   └── feishu.py
│   ├── domain/
│   │   ├── models.py
│   │   └── enums.py
│   ├── services/
│   │   ├── orchestrator.py
│   │   ├── review_queue.py
│   │   ├── notifier.py
│   │   └── audit.py
│   ├── storage/
│   │   ├── repository.py
│   │   └── migrations/
│   ├── config.py
│   └── main.py
├── prompts/
│   ├── knowledge/
│   ├── observer/
│   └── release/
├── templates/
│   ├── daily_report.md
│   ├── incident_alert.md
│   └── release_notes.md
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
    ├── run_daily_check.sh
    └── seed_dev_data.sh
```

## 6. 核心数据结构

所有主键建议使用 UUID，时间统一存 UTC，展示时转换为 Asia/Shanghai。

### 6.1 Project

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 项目标识 |
| name | string | 项目名称 |
| kind | enum | `business_agent` / `control_plane` |
| repo_url | string? | 仓库地址 |
| knowledge_root | string? | 知识库根路径或连接标识 |
| owner | string | 负责人 |
| config | JSON | 非敏感接入配置 |
| enabled | boolean | 是否启用巡检 |

### 6.2 AgentRun

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 单次运行标识 |
| project_id | UUID | 关联项目 |
| agent_type | enum | `knowledge` / `observer` / `release` |
| trigger | enum | `schedule` / `manual` / `webhook` |
| status | enum | `queued` / `running` / `succeeded` / `failed` / `partial` |
| started_at / finished_at | datetime? | 起止时间 |
| input_ref | string? | 输入引用，不存敏感正文 |
| summary | text? | 运行摘要 |
| error | JSON? | 脱敏错误信息 |

### 6.3 Finding

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 发现项标识 |
| run_id | UUID | 来源运行 |
| category | string | 重复、冲突、服务异常等 |
| severity | enum | `info` / `warning` / `critical` |
| title / detail | text | 标题与说明 |
| evidence | JSON | 可验证证据，需脱敏 |
| recommendation | text? | 建议动作 |
| fingerprint | string | 去重指纹 |

### 6.4 KnowledgeCandidate

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 候选条目标识 |
| run_id / project_id | UUID | 来源运行与项目 |
| source_uri | string | 来源位置 |
| content_hash | string | 内容指纹 |
| title / summary | text | 标题与摘要 |
| knowledge_type | string | script、brief、skill、prompt 等 |
| tags / related_ips | array | 标签与关联 IP |
| proposed_action | enum | `create` / `update` / `deprecate` / `ignore` |
| target_uri | string? | 建议目标位置 |
| confidence | decimal | 0 到 1 |
| diff | text? | 与现有内容的差异 |

### 6.5 ReviewItem

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 审核项标识 |
| project_id | UUID | 关联项目 |
| item_type | string | knowledge change 等 |
| payload_ref | string | 指向候选数据 |
| status | enum | `pending` / `approved` / `rejected` / `expired` / `executed` |
| reviewer / reviewed_at | string?/datetime? | 审核人与时间 |
| comment | text? | 审核意见 |
| expires_at | datetime? | 超时时间 |

### 6.6 CheckResult 与 ReleaseReport

- `CheckResult`：`run_id`、`component`、`check_name`、`status`、`latency_ms`、`observed_at`、`evidence`。
- `ReleaseReport`：`run_id`、`repo_ref`、`branch`、`base_ref`、`head_ref`、`commit_count`、`dirty`、`risk_items`、`notes_draft`。

### 6.7 AuditEvent

记录 `actor`、`action`、`object_type`、`object_id`、`before`、`after`、`request_id`、`created_at`。密钥、Token、完整环境变量和隐私数据不得进入审计内容。

## 7. 飞书交互设计

### 7.1 交互入口

- **定时推送**：每天固定时间发送 Control Plane 日报。
- **异常告警**：首次出现 critical 时立即发送；相同指纹在冷却窗口内合并，恢复时发送恢复通知。
- **审核卡片**：Knowledge Candidate 进入 Review Queue 后发送交互卡片。
- **手工命令**：v0.1 支持 `/cp status`、`/cp run observer`、`/cp reviews`；必须校验发送人白名单。

### 7.2 日报卡片

```text
Director Control Plane Daily · 2026-08-07

总体：⚠️ Warning
Knowledge：待审核 3，冲突 1
Observer：6/7 正常，worker 最近一次任务失败
Release：main 干净，7 个新提交，Release Notes 已生成

[查看详情] [查看待审核]
```

### 7.3 审核卡片

卡片必须展示：来源、摘要、目标位置、变更类型、diff、风险提示、置信度和过期时间。

按钮：

- `批准`：仅把状态改为 approved；受控执行器再次校验版本和权限后才可写入。
- `拒绝`：要求填写原因。
- `查看详情`：打开内部详情页或返回完整报告。

审批回调要求：校验签名、防重放、幂等处理、校验审核人权限，并写入 AuditEvent。若源内容自送审后发生变化，审批失效并要求重新送审。

### 7.4 告警策略

- critical 立即通知，warning 汇总进日报；
- 同一 `project + check + fingerprint` 在 30 分钟内不重复轰炸；
- 连续 2 次成功后标记恢复；
- 通知失败进入重试队列，最多 3 次，仍失败则保留失败记录供查询。

## 8. 第一周开发任务

### Day 1：骨架与边界

- 初始化独立仓库、目录、配置加载和本地启动方式；
- 建立 Project、AgentRun、Finding、ReviewItem、AuditEvent 模型及迁移；
- 准备 `.env.example`，确认密钥不入库；
- 写清只读权限和禁止动作。

**当日产出**：API 可启动，`/health` 正常，数据库可迁移，能创建并查询 AgentRun。

### Day 2：Observer Agent 最小闭环

- 实现 HTTP、Docker、数据库和日志检查接口；
- 实现状态聚合、超时、脱敏和错误隔离；
- 使用 fixture 覆盖 healthy、warning、critical、unknown。

**当日产出**：一条命令生成结构化系统健康报告，不执行任何修复。

### Day 3：Knowledge Agent 最小闭环

- 扫描指定 inbox，解析 Markdown/TXT，登记 PDF；
- 实现 hash 去重、元数据建议和基本冲突提示；
- 创建 KnowledgeCandidate 和 ReviewItem；
- 生成 `KNOWLEDGE_MAP.md` 与 `CHANGELOG.md` 草案。

**当日产出**：新素材可进入 Review Queue，生产知识库没有被直接修改。

### Day 4：Release Agent 与飞书

- 实现 Git 只读状态、提交区间汇总和 Release Notes 草案；
- 实现飞书日报、异常告警和审核卡片；
- 实现签名校验、白名单、幂等审批回调和审计记录。

**当日产出**：三个 Agent 的结果可汇总为一张日报；审核动作可被可靠记录。

### Day 5：集成、演练与文档

- 配置定时任务和失败重试；
- 完成端到端演练：定时触发、发现异常、发送日报、知识审批、审计查询；
- 补齐测试、运行手册、安全说明和已知限制；
- 记录 v0.2 候选项，但不扩展 v0.1 范围。

**当日产出**：在测试环境连续运行并通过验收清单。

## 9. 验收标准

### 9.1 功能验收

- [ ] 可配置至少 1 个项目并独立启停三类检查。
- [ ] 三个 Agent 均能生成带唯一 run ID 的结构化报告。
- [ ] Knowledge Agent 能识别完全重复内容，并将新增/更新建议放入 Review Queue。
- [ ] 未经批准时，生产知识库文件的内容与校验和保持不变。
- [ ] Observer Agent 能正确区分 healthy、warning、critical、unknown，单项检查失败不会导致整次运行丢失。
- [ ] Release Agent 能读取分支、工作区和提交区间，生成包含风险项的 Release Notes 草案。
- [ ] 飞书可收到日报、critical 告警及知识审核卡片；重复回调不会重复执行。
- [ ] 每次运行、发现、审批和执行结果都能通过 run ID 追溯。

### 9.2 安全与边界验收

- [ ] 默认使用只读凭据；生产写入能力与分析进程隔离。
- [ ] v0.1 不存在自动 merge、deploy、restart、delete 或生产配置修改路径。
- [ ] 日志、报告、通知和审计记录中不出现 Token、密码及完整环境变量。
- [ ] 飞书回调完成签名、时效、权限和幂等校验。
- [ ] 所有外部调用有明确超时；失败可重试但不会无限重试。

### 9.3 质量与运行验收

- [ ] 核心领域逻辑单元测试通过，关键适配器具备集成测试。
- [ ] 使用固定 fixture 可重复演示三种 Agent 的成功和失败路径。
- [ ] 测试环境连续 24 小时运行，无未捕获异常，定时任务无重复执行。
- [ ] 日报从触发到送达不超过 5 分钟；critical 告警在检查完成后 1 分钟内发出。
- [ ] `README.md`、`OPERATIONS.md` 和 `.env.example` 足以让新维护者在 30 分钟内完成本地启动。

## 10. 交付物

v0.1 完成时应包含：

1. 可运行的 Control Plane 服务和数据库迁移；
2. Knowledge、Observer、Release 三个只读优先 Agent；
3. Review Queue 与审计记录；
4. 飞书日报、告警和知识审核卡片；
5. 测试 fixture、自动化测试和端到端演练记录；
6. README、运行手册、安全说明及本开发方案。

## 11. v0.2 入口条件

仅当 v0.1 连续稳定运行、误报率可接受、审计链完整且人工审批流程顺畅后，再评估有限自动修复、自动建 PR 或更多业务 Agent 接入。任何自动动作都应逐项授权，并提供预演、回滚和熔断机制。

## 12. 冻结需求 Backlog（Frozen Requirements）

以下需求已冻结，作为 Control Plane 后续阶段（v0.2 及以后）的待排期输入。每个需求有独立规格文档，本表仅作索引。

| 编号 | 名称 | 优先级 | 类型 | 目标阶段 | 规格文档 |
|---|---|---|---|---|---|
| TASK-STABILITY-001 | Dependency Self-Healing System（依赖自愈系统） | P2 | Infrastructure / Stability | v0.1（Monitor + Mapping）→ v0.2（Recovery） | [DEPENDENCY_SELF_HEALING_SYSTEM.md](./DEPENDENCY_SELF_HEALING_SYSTEM.md) |

设计约束（适用于所有冻结需求）：

- 业务 Agent 不负责基础设施维护；基础设施问题统一进入 Control Plane。
- v0.1 仅可观测（发现 / 定位 / 告警 / 建议），不自动修复；自动恢复进入 v0.2，且须逐项授权、可预演、可回滚、有熔断。
- 所有自动动作必须可追踪、可回滚、有日志。
