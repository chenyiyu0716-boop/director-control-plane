# 独立钟（2026-08-19 登记）

目标：系统永远知道三个 agent 是否活着。不是三个窗口永远开着。

```
launchd（钟，自持）
    → status probe → HOURLY_STATUS 落盘
    → Chief 判断（WARNING，不修复）
```

## 角色（2026-08-19 12:04 锁定）

| 层 | 承担 | 不承担 |
|---|---|---|
| **钟** | launchd 自持；每小时观察、落盘 | 执行产品工作 |
| **手** | 优先 Cursor 本机 agent（非交互拉起，Clock Proof **之后**才验） | 聊天窗口当 daemon |
| **WB** | 人工审批 / 临时发射 / 产品运营入口（看稿、专项评审、人工介入） | 每小时检查、每天巡检、持续治理；不进控制链 |

Agent Runner 候选（未验、未启用）：

```
launchd → cursor-agent --cwd <repo> --prompt HOURLY_LOOP.md
```

若本机非交互拉起失败，才保留 `launchd → WB 发射 → Cursor 执行` 作过渡。现在不测、不安装、不拉起。

Julius 仓内 `julius-workbuddy` 仍可当 **J3 产品执行器**（draft_build），那是运营入口，不是小时巡检 daemon。hy3 / `dispatch next` 仍冻。

## Clock Proof Phase（当前）

P0 钟：12:10 / 12:40 / 13:10 看 on_time、exit、log 缺口。  
P1 手：今日窗口保持开着（控制变量）。下一轮才测手消失 → stale。  
P2 Chief：stale 只 WARNING，不重启、不改配置、不提交。

12:10 成功标准：clock `on_time=True`；Director `status_age < 60min`；`able_to_read=True`。

通过后再进 **Agent Runner Phase**。

指针：`/Users/pojian/chief-clock/`  
LaunchAgent：`gui/501/com.pojian.chief-clock`
