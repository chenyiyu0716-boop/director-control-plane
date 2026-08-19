# Chief

Chief 是 AI Agent 运维总控系统 —— 独立于业务 Agent 的控制层，统一管理多个 Agent 项目的知识资产、运行状态与版本信息。

> 当前阶段：v0.1（可观测，先不自动修复）。设计详见 [`docs/DIRECTOR_CONTROL_PLANE_V0.1.md`](docs/DIRECTOR_CONTROL_PLANE_V0.1.md)（历史规格文档，文件名与标题沿用旧称，不做改写）。

## 命名与兼容性

对外产品名统一为 **Chief**。以下标识符仅作内部兼容用途，保持不变，不随产品名改写：

| 类别 | 标识符 | 说明 |
| --- | --- | --- |
| Python 包 | `control_plane` | 导入路径与模块名不变 |
| 项目 ID | `control-panel` | 配置、任务与基线登记沿用 |
| 数据库路径 | `var/control-plane.sqlite3` | 运行时数据文件不迁移 |
| 环境变量 | `CONTROL_PLANE_CONFIG`、`CONTROL_PLANE_FEISHU_CONFIG` | 启动契约不变 |
| 历史规格文档 | `docs/DIRECTOR_CONTROL_PLANE_V0.1.md`、`docs/DEPENDENCY_SELF_HEALING_SYSTEM.md` | 冻结文档不改写 |

架构语境中的「控制面 / control plane」仍指分层概念，不作为产品名使用。

Chief 登记并只读观察 Julius 与 Director。它不替代两仓执行，不改两仓产品代码，本期也不启用 hy3 领取任务。

## 当前可运行闭环

仓库已经具备第一条只读闭环：

- `KnowledgeAgent` 扫描白名单知识目录，计算指纹并创建 Review Queue 候选；不保存源正文，不写生产库。
- `ObserverAgent` 检查项目根目录、Ledger、状态文件和 Git 可读性，单项失败不会丢失整次运行。
- `ReleaseAgent` 汇总分支、提交、工作区状态与 Release Notes 草案；不提交、不推送、不发布。
- SQLite 保存 Project、AgentRun、Finding、CheckResult、KnowledgeCandidate、ReviewItem、ReleaseReport 和 AuditEvent。
- Task Registry 保存任务定义、依赖、版本化状态和完整迁移历史，并可生成确定性的 Markdown 人类视图。
- Decision Policy 使用版本化确定性规则把任务判定为 READY、NEEDS_DECISION 或 BLOCKED；模型建议不能覆盖安全门禁。
- 飞书 Owner Control Channel 通过企业自建应用长连接接收结构化决策与方向调整；白名单、过期、nonce 和 event_id 阻止越权与重放。
- Lease Dispatcher 让授权执行器以唯一、可过期租约领取 READY 任务，并在基线漂移时拒绝提交。
- 本地只读 API 提供 `/health`、`/api/projects`、`/api/runs`、`/api/findings`、`/api/reviews`、`/api/checks`、`/api/releases`、`/api/tasks`。

### 本地启动（clone 后第一段）

clone 本仓即可观察两仓登记，不依赖本机 `~/repos/julius` 或 `~/repos/director` 热树。`config/projects.example.json` 指向本仓 `fixtures/demo-julius` 与 `fixtures/demo-director`。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
chmod +x scripts/bootstrap_demo.sh
./scripts/bootstrap_demo.sh
```

`bootstrap_demo.sh` 只在 fixture 目录 `git init`（已 gitignore），写入 `var/demo-control-plane.sqlite3`，然后 `init-db`、`run-all`、列出 `julius` 与 `director-agent`。不写真实两仓，不 register 任务，不 dispatch。

要把观察目标换成真实 checkout：复制 example 为 `config/projects.local.json`，把 `root` 改成绝对路径，并把 `database` 改成 `var/control-plane.sqlite3`。不要把领取执行器任务当作第一段。

服务默认只监听 `127.0.0.1:8765`，没有外网写入入口：

```bash
PYTHONPATH=src python3 -m control_plane.main --config config/projects.example.json serve
```

飞书入口单独运行，真实配置和凭据不入库：

```bash
python3 -m pip install -r requirements-feishu.txt
cp config/feishu-control.example.json config/feishu-control.local.json
FEISHU_APP_ID=... FEISHU_APP_SECRET=... \
CONTROL_PLANE_CONFIG=config/projects.local.json \
CONTROL_PLANE_FEISHU_CONFIG=config/feishu-control.local.json \
PYTHONPATH=src python3 scripts/run_feishu_control.py
```

该入口只订阅新版 `card.action.trigger`。回调先落入结构化 inbox 并立即响应，后台线程再执行；不保存完整飞书会话。需求和方向调整先生成 `PREVIEW_PENDING` 记录，确认后也只进入 Planner review，不直接修改运行中任务或创建 READY 任务。

### 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 定位

- 控制层与业务层分离：Chief 独立部署、独立存储，不侵入业务 Agent 核心逻辑。
- 先可观测，后自动化：v0.1 负责发现 / 整理 / 报告 / 建议；v0.2 再评估有限自动修复。
- 人保留最终决策权；所有动作可追踪、可回滚、有日志。

## 文档

- `docs/DIRECTOR_CONTROL_PLANE_V0.1.md` —— v0.1 开发方案（架构 / 三 Agent / 数据结构 / 验收）。
- `docs/DEPENDENCY_SELF_HEALING_SYSTEM.md` —— 冻结需求：依赖自愈系统（基础设施层，首发案例 KESU 静默降级）。
- `docs/DECISION_POLICY.md` —— TASK-016 确定性决策规则、模型边界与审计证据。
- `docs/FEISHU_CONTROL_CHANNEL.md` —— TASK-017 Owner 控制通道、卡片字段、安全边界和启用步骤。
- `docs/LEASE_DISPATCHER.md` —— TASK-018 租约协议、权限门禁、过期恢复与执行器边界。

## 冻结需求 Backlog

见主设计文档 §12。当前条目：**TASK-STABILITY-001 Dependency Self-Healing System（P2）**。
