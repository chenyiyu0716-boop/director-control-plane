# Feishu Owner Control Channel

TASK-017 为指定 Owner 提供独立于 WorkBuddy 的飞书控制入口。它使用飞书企业自建应用的 SDK 长连接，不暴露公网回调地址。

## 处理边界

1. 卡片动作必须来自 `ownerOpenIds` 白名单。
2. `header.event_id` 是请求幂等键；卡片中的 `nonce` 是防重放键，两者均唯一。
3. 卡片必须携带带时区的 `expires_at`；过期动作落为 rejected，不产生业务副作用。
4. 回调只同步写入脱敏的结构化 inbox，然后立即向飞书确认。业务处理由后台 worker 完成。
5. 不保存完整会话、消息正文、token、cookie、App Secret 或其他未知卡片字段。

## 卡片动作契约

`control_plane.adapters.feishu_cards` 提供决策卡、需求/方向输入卡和预览确认卡。按钮绑定当前对象版本、一次性 nonce 与过期时间；输入卡提交后必须先返回预览确认卡。

共同字段放在卡片按钮的 `value`：

```json
{
  "nonce": "one-time-random-value",
  "expires_at": "2026-08-11T12:00:00+08:00",
  "command": "task_decision"
}
```

### Owner 决策

`task_decision` 还必须携带 `task_id`、`task_version`、`action` 和 `reason`。`action` 只允许 `approve`、`reject`、`request_changes`。只允许从当前版本的 `NEEDS_DECISION` 转移；批准转为 `READY`，其余转为 `BLOCKED`。

### 新需求与方向调整

`requirement_intake` 携带 `project_id`、`kind`、`objective` 和可选 `requested_priority`。系统先保存结构化影响预览，状态为 `PREVIEW_PENDING`。

`confirm_intake` 携带 `intake_id`、`intake_version` 和 `confirm`。确认后状态为 `CONFIRMED`，等待 Planner 生成或调整 DRAFT 任务。该动作不会修改 CLAIMED/RUNNING 任务，也不会直接生成 READY。

## 启用

1. 在飞书开放平台创建企业自建应用，启用机器人，并用长连接订阅 `card.action.trigger`。
2. 从 `config/feishu-control.example.json` 创建被 git 忽略的 `.local.json`，只填写 Owner open_id。
3. 将 App ID 和 App Secret 放入进程环境变量，禁止写入文件或日志。
4. 安装 `requirements-feishu.txt` 后运行 `scripts/run_feishu_control.py`。
5. 先发送测试卡验证白名单、重复提交、过期提交和决策留痕，再启用正式卡片。

当前仓库提供可测试的适配器和运行入口，但不会自动创建飞书应用，也不会代填或提交真实凭据。
