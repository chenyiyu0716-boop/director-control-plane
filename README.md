# Director Control Plane

AI Agent 运维总控系统 —— 独立于业务 Agent 的控制面，统一管理多个 Agent 项目的知识资产、运行状态与版本信息。

> 当前阶段：v0.1（可观测，先不自动修复）。设计详见 [`docs/DIRECTOR_CONTROL_PLANE_V0.1.md`](docs/DIRECTOR_CONTROL_PLANE_V0.1.md)。

## 当前可运行闭环

仓库已经具备第一条只读闭环：

- `KnowledgeAgent` 扫描白名单知识目录，计算指纹并创建 Review Queue 候选；不保存源正文，不写生产库。
- `ObserverAgent` 检查项目根目录、Ledger、状态文件和 Git 可读性，单项失败不会丢失整次运行。
- `ReleaseAgent` 汇总分支、提交、工作区状态与 Release Notes 草案；不提交、不推送、不发布。
- SQLite 保存 Project、AgentRun、Finding、CheckResult、KnowledgeCandidate、ReviewItem、ReleaseReport 和 AuditEvent。
- Task Registry 保存任务定义、依赖、版本化状态和完整迁移历史，并可生成确定性的 Markdown 人类视图。
- Decision Policy 使用版本化确定性规则把任务判定为 READY、NEEDS_DECISION 或 BLOCKED；模型建议不能覆盖安全门禁。
- 本地只读 API 提供 `/health`、`/api/projects`、`/api/runs`、`/api/findings`、`/api/reviews`、`/api/checks`、`/api/releases`、`/api/tasks`。

### 本地启动

```bash
cp config/projects.example.json config/projects.local.json
PYTHONPATH=src python3 -m control_plane.main --config config/projects.local.json init-db
PYTHONPATH=src python3 -m control_plane.main --config config/projects.local.json run-all --trigger manual
PYTHONPATH=src python3 -m control_plane.main --config config/projects.local.json task register --file config/task.example.json --actor codex
PYTHONPATH=src python3 -m control_plane.main --config config/projects.local.json task decide TASK-015 --facts config/decision-facts.example.json --expected-version 1 --actor codex
PYTHONPATH=src python3 -m control_plane.main --config config/projects.local.json task render --output var/TASKS.md
PYTHONPATH=src python3 -m control_plane.main --config config/projects.local.json serve
```

服务默认只监听 `127.0.0.1:8765`，没有外网写入入口。

### 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 定位

- 控制面与业务面分离，不侵入业务 Agent 核心逻辑。
- 先可观测，后自动化：v0.1 负责发现 / 整理 / 报告 / 建议；v0.2 再评估有限自动修复。
- 人保留最终决策权；所有动作可追踪、可回滚、有日志。

## 文档

- `docs/DIRECTOR_CONTROL_PLANE_V0.1.md` —— v0.1 开发方案（架构 / 三 Agent / 数据结构 / 验收）。
- `docs/DEPENDENCY_SELF_HEALING_SYSTEM.md` —— 冻结需求：依赖自愈系统（基础设施层，首发案例 KESU 静默降级）。
- `docs/DECISION_POLICY.md` —— TASK-016 确定性决策规则、模型边界与审计证据。

## 冻结需求 Backlog

见主设计文档 §12。当前条目：**TASK-STABILITY-001 Dependency Self-Healing System（P2）**。
