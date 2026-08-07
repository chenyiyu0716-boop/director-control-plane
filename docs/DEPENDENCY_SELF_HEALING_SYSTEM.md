# Dependency Self-Healing System（依赖自愈系统）

> 文档类型：冻结需求 / Frozen Requirement
> 状态：FROZEN（需求已冻结，待排期）
> 提出日期：2026-08-07
> 归属：Director Control Plane（基础设施层）
> 优先级：P2
> 接入阶段：v0.1（可观测：Monitor + Mapping）→ v0.2（自动恢复：Recovery）
> 关联文档：`docs/DIRECTOR_CONTROL_PLANE_V0.1.md`
> Backlog 编号：TASK-STABILITY-001

## 0. 一句话定义

依赖自愈系统是一套**基础设施层能力**，让任意 AI Agent 在依赖服务（知识库、数据库、Backend、外部 API）异常时，能够**自动发现、自动定位影响范围、自动恢复、自动记录事件**——而不是由业务 Agent 自己处理，也不是由每个 Agent 各自实现一套。

> 命名说明：不叫「KESU 修复」。当前以 KESU 挂载失败为首发案例，但本质是通用依赖稳定性问题。Julius、财务看板、未来 IP 知识库都会遇到同一种「依赖晚挂载 / 异常退出 / 静默降级」问题。这是可复用的基础设施资产。

## 1. 问题背景

当前系统包含：Frontend、Backend、Knowledge Base Service（KESU）、Docker Compose 部署环境。Backend 的核心能力依赖 KESU 提供知识库检索服务。

架构存在一个稳定性风险：**如果 KESU 在 backend 启动后未完成挂载、异常退出或晚恢复，backend 不会主动重新建立依赖关系，而是进入静默降级状态。**

### 1.1 首发案例（KESU silent degradation）

根因（已定位，2026-08-07）：backend `main.py` 在启动时以 `try/except` 包裹 `init_knowledge_base_dir()`，异常被吞掉；`KNOWLEDGE_BASE_DIR` 模块常量退化为示例目录（`samples/knowledge_base_sample`）。当 KESU 在 backend 之后才挂载好时，backend 进程内 KB 路径已固定为示例目录，且不会重绑，导致知识库相关能力静默失效。

表现：

- backend 容器状态正常（healthy）
- API 可以访问
- 业务请求可以提交
- 但知识库相关能力返回空结果或 unavailable
- 用户侧表现：
  - 某 IP 大纲生成为空
  - 脚本生成质量异常下降
  - Agent 像「失忆」

> 该案例说明：问题不是 backend bug，而是**依赖生命周期未被纳入统一运维管理**。手动 `docker restart backend` 可临时恢复（强制 backend 在 KESU 已挂载后重新初始化），但这是人工运维动作，不可规模化。

## 2. 当前处理方式（临时 / Manual Workaround）

发现知识库异常时：

1. 检查 backend 日志：

```bash
docker logs backend --tail 200 | grep '\[kb\]'
```

（`[kb]` 为 backend `main.py` 启动期 KB 初始化日志前缀；出现 `knowledge base dir unavailable at startup` 即代表 KB 未正确挂载。）

2. 如发现 KB unavailable，执行：

```bash
docker restart backend
```

重新建立 backend 与 KESU 的连接。

> 这是人工运维动作，仅用于当前生产止血，**不属于系统设计**。

## 3. 当前阶段决策

状态：**PENDING**

暂不修改业务代码。

原因：当前项目仍处于 Agent 能力迭代阶段、知识库结构调整阶段、控制面设计阶段。

当前优先级：

1. 保证生产可用（手动 restart 已能止血）
2. 降低人工维护成本（未来由 Control Plane 接管，而非 human）
3. 建立统一运维体系（Control Plane）

**不在业务 Agent 内加入临时自愈逻辑。**

## 4. 长期目标

该问题不应该由编导 Agent 或单个业务 Agent 解决。应纳入独立 Control Plane（Agent 运维总控系统），建立可复用的 AI Agent Infrastructure Management Layer。

管理对象：

- Agent 服务
- Backend
- Frontend
- Knowledge Base
- 数据库
- 外部依赖服务

## 5. 运维系统需要负责的能力

### 5.1 Dependency Health Monitor（依赖健康监测）

持续检测 Agent 系统依赖状态。

监控项：

- Container health
- Service availability
- API response
- Knowledge Base availability
- Database connection

示例：

```
Agent A
 |
 | depends on
 |
Knowledge Base A

状态：healthy / degraded / unavailable
```

> 与现有 Control Plane 的关系：此能力由 v0.1 **Observer Agent** 承载（见 `DIRECTOR_CONTROL_PLANE_V0.1.md` §4.2）。Observer 已规划检查 frontend/backend/worker 存活、Docker 容器状态、PostgreSQL 连接、API 状态码与耗时、日志错误率。需在其上补充「KB availability」专项检查——关键是识别 **silent degradation**（容器 healthy 但 KB 实际不可用），而非仅看容器状态。

### 5.2 Dependency Mapping（依赖关系映射）

维护依赖关系图：

```
Director Agent
depends_on:
  - backend
  - knowledge-base-director (KESU)
  - postgres
  - llm-provider
```

当依赖异常时，可定位影响范围（blast radius）。

> 与现有 Control Plane 的关系：依赖映射是 Observer / Control Plane 的元数据，应作为 `Project` 实体（见 v0.1 §6.1）的 `dependencies` 字段或独立 `DependencyEdge` 模型沉淀，供 Monitor 与 Recovery 共用。

### 5.3 Automatic Recovery Workflow（自动恢复工作流）

未来支持：

```
检测：Knowledge Base unavailable
  ↓
Step 1: 确认服务状态
  ↓
Step 2: 检查日志
  ↓
Step 3: 判断是否需要恢复
  ↓
Step 4: 执行 restart service（受控、可回滚）
  ↓
Step 5: 验证恢复
  ↓
Step 6: 记录事件
```

> 与现有 Control Plane 的关系：自动恢复属于 **v0.2** 范围（见 v0.1 §11：v0.2 入口条件包含「有限自动修复」）。v0.1 明确「不重启、不修复、不改配置」（§4.2 职责边界、§2.2 明确不包含）。即：v0.1 只做「发现 + 定位 + 告警 + 建议」，v0.2 才做「执行恢复」。

## 6. 推荐技术方案

### 6.1 第一阶段：Docker Compose 原生能力（治标 / 启动顺序）

增加 `healthcheck` 与 `depends_on: condition: service_healthy`，保证启动顺序：

```
KESU healthy
  ↓
Backend start
  ↓
Frontend start
```

目标：消除「backend 先于 KESU 启动导致静默降级」的启动期窗口。

> 注意：Docker Compose `depends_on` 只能解决**启动顺序**，不能解决运行期 KESU 异常退出 / 晚恢复后的重连。运行期自愈必须靠 Control Plane。

### 6.2 第二阶段：Control Plane 接管（治本 / 运行期自愈）

不依赖 docker compose。由 Control Plane 负责：

- 服务状态采集
- 异常判断（含 silent degradation 识别）
- 自动恢复（受控、可回滚）
- 事件记录
- 版本关联

## 7. 不推荐方案（Anti-patterns）

### ❌ Backend 内部无限 retry

- 业务代码侵入基础设施逻辑
- 增加复杂度
- 不利于未来替换知识库实现
- 与「控制面与业务面分离」原则冲突（v0.1 §1 核心原则 1）

### ❌ 编导 Agent 自己处理

- 知识库属于基础设施
- 多个 Agent 都可能依赖：编导 Agent、Julius、财务 Agent、未来其他 IP Agent
- 应统一管理，避免每个 Agent 各自造一套自愈

## 8. 验收标准（Acceptance）

模拟：KESU 异常关闭。

系统应当能够：

- [ ] 检测异常（Dependency Health Monitor 识别 KB unavailable，含 silent degradation）
- [ ] 输出影响范围（Dependency Mapping 给出受影响的 Agent / 服务清单）
- [ ] 执行恢复动作（Automatic Recovery Workflow：受控 restart，可回滚）
- [ ] 恢复 Agent 正常能力（验证 KB 检索恢复、大纲生成非空）
- [ ] 保存事件日志（AuditEvent：actor=system、action=recover、object、before/after、request_id）

## 9. 设计原则

1. 业务 Agent 不负责基础设施维护。
2. 基础设施问题统一进入 Control Plane。
3. 所有自动修复必须：可追踪、可回滚、有日志。
4. 当前阶段优先稳定运行，不提前过度工程化（v0.1 observe-only）。

## 10. Backlog 登记

- 编号：TASK-STABILITY-001
- 名称：Dependency Self-Healing System（依赖自愈系统）
- 优先级：P2
- 类型：Infrastructure / Stability
- 目标：任意 AI Agent 依赖异常时自动发现 / 定位 / 恢复 / 记录
- 分解：
  - TASK-STABILITY-001-A（v0.1）：Observer 增加 KB availability 专项检查 + Dependency Mapping 元数据模型
  - TASK-STABILITY-001-B（v0.2）：Automatic Recovery Workflow（受控 restart + 验证 + 审计）
- 关联首发案例：KESU silent degradation（2026-08-07 根因已定位）
